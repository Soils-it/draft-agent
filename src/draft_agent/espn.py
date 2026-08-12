from __future__ import annotations

import copy
import json
import math
import threading
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import LeagueConfig
from .engine import DraftEngine
from .models import Player
from .scoring import projected_points
from .signals import SignalRecord, apply_signals


class EspnDraftBridge:
    """Validate browser-observed ESPN state and calculate a shadow recommendation.

    The bridge deliberately has no browser credentials and no submit-pick method.
    A later, mock-draft-tested companion may poll an explicit command endpoint.
    """

    positions = ("QB", "RB", "WR", "TE", "K", "DST")
    minimum_mapping_rate = 0.8
    minimum_historical_rate = 0.5
    minimum_signal_rate = 0.75
    minimum_catalog_size = 100

    @staticmethod
    def _team_for_pick(overall: int, teams: int) -> int:
        round_number = (overall - 1) // teams + 1
        within_round = (overall - 1) % teams + 1
        return within_round if round_number % 2 else teams + 1 - within_round

    def __init__(self, audit_path: Path | None = None, max_audit_entries: int = 500) -> None:
        self.mock_rosters: dict[str, set[str]] = {}
        self.confirmed_rosters: dict[str, set[str]] = {}
        self.exposure_limit = 0.0
        self.audit_path = audit_path
        self.max_audit_entries = max_audit_entries
        self._audit_lock = threading.Lock()
        self.decision_log = self._load_decision_log()
        for entry in self.decision_log:
            draft_id = str(entry.get("draft_id") or "")
            selected_id = str((entry.get("selected_player") or {}).get("espn_id") or "")
            if draft_id and selected_id and entry.get("status") in {"selected", "submitted"}:
                self.confirmed_rosters.setdefault(draft_id, set()).add(selected_id)
        if self.audit_path is not None and self.audit_path.exists():
            with self._audit_lock:
                self._persist_decision_log_locked()
        self.state: dict[str, object] = {
            "connected": False,
            "mode": "shadow",
            "can_submit": False,
            "message": "Waiting for an ESPN mock-draft snapshot.",
            "readiness": {
                "ready": False,
                "label": "NOT READY",
                "reasons": ["Waiting for a current ESPN draft snapshot."],
                "coverage": {},
                "sources": {},
            },
            "decision_log": self.decision_summary(),
            "mock_exposure_report": self.mock_exposure_summary(),
        }

    def invalidate(self, message: str) -> None:
        """Fail closed until a new browser snapshot is validated."""
        self.state = {
            "connected": False,
            "mode": "shadow",
            "can_submit": False,
            "recommendations": [],
            "prequeue_espn_player_ids": [],
            "pending_espn_player_id": None,
            "mock_command_ready": False,
            "message": message,
            "readiness": {
                "ready": False,
                "label": "NOT READY",
                "reasons": [message],
                "coverage": {},
                "sources": {},
            },
            "decision_log": self.decision_summary(),
            "mock_exposure_report": self.mock_exposure_summary(),
        }

    def _load_decision_log(self) -> list[dict[str, Any]]:
        if self.audit_path is None or not self.audit_path.exists():
            return []
        try:
            if self.audit_path.stat().st_size > 10_000_000:
                return []
            payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                return []
            valid: list[dict[str, Any]] = []
            for item in payload:
                try:
                    teams = int(item.get("teams", 12))
                    decision_pick = int(item["decision_pick"])
                    user_slot = int(item["user_slot"])
                    if teams <= 0 or decision_pick <= 0:
                        continue
                    if self._team_for_pick(decision_pick, teams) != user_slot:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                valid.append(item)
            return valid[-self.max_audit_entries :]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []

    def _persist_decision_log_locked(self) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.audit_path.with_suffix(self.audit_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.decision_log, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.audit_path)

    @staticmethod
    def _compact_candidate(candidate: dict[str, object]) -> dict[str, object]:
        contributions = dict(candidate.get("contributions", {}))
        positive = [item for item in contributions.items() if float(item[1]) > 0]
        top_driver = max(positive, key=lambda item: float(item[1]))[0] if positive else None
        return {
            "status": "eligible",
            "espn_id": candidate.get("espn_id"),
            "name": candidate.get("name"),
            "team": candidate.get("team"),
            "position": candidate.get("position"),
            "projected_points": candidate.get("projected_points"),
            "espn_rank": candidate.get("espn_rank"),
            "consensus_rank": candidate.get("consensus_rank"),
            "market_rank": candidate.get("market_rank"),
            "market_reach": candidate.get("market_reach"),
            "market_disagreement": candidate.get("market_disagreement"),
            "draft_score": candidate.get("draft_score"),
            "survival_probability": candidate.get("survival_probability"),
            "top_driver": top_driver,
            "components": candidate.get("components", {}),
            "contributions": contributions,
        }

    @staticmethod
    def _basic_player(player: Player) -> dict[str, object]:
        consensus_rank = DraftEngine._consensus_rank(player)
        return {
            "espn_id": player.external_ids.get("espn"),
            "name": player.name,
            "team": player.team,
            "position": player.position,
            "projected_points": projected_points(player),
            "espn_rank": round(float(player.adp), 1),
            "consensus_rank": (
                round(consensus_rank, 1) if consensus_rank is not None else None
            ),
            "market_rank": DraftEngine._market_rank(player),
            "market_disagreement": DraftEngine._market_disagreement(player),
        }

    def _decision_rules(
        self,
        engine: DraftEngine,
        roster: list[Player],
        current_pick: int,
    ) -> dict[str, object]:
        round_number = (current_pick - 1) // engine.config.teams + 1
        counts = {
            position: sum(item.position == position for item in roster)
            for position in self.positions
        }
        incumbents: dict[str, object] = {}
        for position in self.positions:
            group = [item for item in roster if item.position == position]
            if not group:
                incumbents[position] = None
                continue
            best = min(group, key=engine._market_rank)
            incumbents[position] = self._basic_player(best)
        return {
            "round": round_number,
            "required_positions": sorted(engine._required_positions(roster, round_number)),
            "market_reach_limit": engine._market_reach_limit(round_number),
            "position_counts": counts,
            "position_caps": dict(engine.config.position_caps),
            "incumbents": incumbents,
        }

    def _blocked_position(
        self,
        position: str,
        available: list[Player],
        rules: dict[str, object],
    ) -> dict[str, object]:
        candidates = [item for item in available if item.position == position]
        if not candidates:
            return {"status": "unavailable", "reason": "No mapped player is available."}
        raw = min(candidates, key=DraftEngine._market_rank)
        counts = dict(rules["position_counts"])
        caps = dict(rules["position_caps"])
        required = list(rules["required_positions"])
        round_number = int(rules["round"])
        if counts[position] >= caps[position]:
            reason = "Position cap reached."
        elif required and position not in required:
            reason = f"Deferred while {', '.join(required)} is required."
        elif position in {"K", "DST"} and round_number < 15:
            reason = "Specialists are reserved for rounds 15-16."
        elif position == "QB" and counts[position] and round_number < 13:
            reason = "Backup QB is blocked before round 13."
        elif position == "TE" and counts[position] and round_number < 13:
            reason = "Backup TE is blocked before round 13."
        else:
            reason = "Blocked by reach, availability, or starter-quality rules."
        return {
            "status": "blocked",
            "reason": reason,
            **self._basic_player(raw),
        }

    def _reconcile_decisions_locked(
        self,
        draft_id: str,
        roster: list[Player],
        overall_pick: int,
    ) -> None:
        roster_by_id = {
            item.external_ids.get("espn", ""): item
            for item in roster
            if item.external_ids.get("espn")
        }
        current_ids = set(roster_by_id)
        for entry in reversed(self.decision_log):
            if entry.get("draft_id") != draft_id or entry.get("status") not in {
                "pending",
                "submitted",
            }:
                continue
            before = set(entry.get("roster_before_ids", []))
            added = current_ids - before
            if added:
                selected_id = sorted(added)[0]
                selected = roster_by_id[selected_id]
                entry["status"] = "selected"
                entry["selected_player"] = self._basic_player(selected)
                entry["matched_recommendation"] = selected_id == (
                    entry.get("recommended_player") or {}
                ).get("espn_id")
                entry["selected_at"] = datetime.now(timezone.utc).isoformat()
            elif overall_pick > int(entry.get("decision_pick", overall_pick)) + 1:
                entry["status"] = "unresolved"
                entry["resolution_note"] = "The draft advanced without a mapped roster addition."
            else:
                continue
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()

    def _record_decision(
        self,
        league_id: str,
        draft_id: str,
        is_mock: bool,
        overall_pick: int,
        decision_pick: int,
        user_slot: int,
        on_clock: bool,
        available: list[Player],
        roster: list[Player],
        ranked: list[dict[str, object]],
        engine: DraftEngine,
    ) -> None:
        if self._team_for_pick(decision_pick, engine.config.teams) != user_slot:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._audit_lock:
            self._reconcile_decisions_locked(draft_id, roster, overall_pick)
            key = f"{draft_id}:{decision_pick}"
            existing = next(
                (
                    item
                    for item in reversed(self.decision_log)
                    if item.get("key") == key
                ),
                None,
            )
            if existing is not None and existing.get("status") in {
                "selected",
                "submitted",
                "unresolved",
            }:
                self._persist_decision_log_locked()
                return
            rules = self._decision_rules(engine, roster, decision_pick)
            top_by_position: dict[str, object] = {}
            for position in self.positions:
                candidate = next((item for item in ranked if item["position"] == position), None)
                top_by_position[position] = (
                    self._compact_candidate(candidate)
                    if candidate is not None
                    else self._blocked_position(position, available, rules)
                )
            record = {
                "key": key,
                "league_id": league_id,
                "draft_id": draft_id,
                "is_mock": is_mock,
                "decision_pick": decision_pick,
                "round": int(rules["round"]),
                "user_slot": user_slot,
                "teams": engine.config.teams,
                "status": "pending",
                "on_clock_seen": bool(on_clock or (existing or {}).get("on_clock_seen")),
                "created_at": existing.get("created_at", now) if existing else now,
                "updated_at": now,
                "roster_before_ids": [
                    item.external_ids["espn"]
                    for item in roster
                    if item.external_ids.get("espn")
                ],
                "roster_before": [self._basic_player(item) for item in roster],
                "rules": rules,
                "recommended_player": self._compact_candidate(ranked[0]) if ranked else None,
                "top_overall": [self._compact_candidate(item) for item in ranked[:5]],
                "top_by_position": top_by_position,
            }
            if existing is None:
                self.decision_log.append(record)
            else:
                existing.clear()
                existing.update(record)
            self.decision_log = self.decision_log[-self.max_audit_entries :]
            self._persist_decision_log_locked()

    def record_pick_result(self, payload: dict[str, Any]) -> None:
        if payload.get("ok") is not True:
            return
        draft_id = str(payload.get("draft_id") or payload.get("league_id") or "").strip()
        player_id = str(payload.get("player_id") or "").strip()
        overall_pick = int(payload.get("overall_pick", 0))
        if not draft_id or not player_id or overall_pick <= 0:
            raise ValueError("pick result requires draft_id, overall_pick, and player_id")
        key = f"{draft_id}:{overall_pick}"
        with self._audit_lock:
            entry = next(
                (
                    item
                    for item in reversed(self.decision_log)
                    if item.get("key") == key
                ),
                None,
            )
            if entry is None:
                raise ValueError("pick result did not match a recorded decision")
            selected = next(
                (
                    item
                    for item in entry.get("top_overall", [])
                    if item.get("espn_id") == player_id
                ),
                {
                    "espn_id": player_id,
                    "name": str(payload.get("name") or "Unknown player"),
                },
            )
            entry["status"] = "submitted"
            entry["selected_player"] = selected
            entry["matched_recommendation"] = player_id == (
                entry.get("recommended_player") or {}
            ).get("espn_id")
            entry["submitted_at"] = datetime.now(timezone.utc).isoformat()
            entry["updated_at"] = entry["submitted_at"]
            self.confirmed_rosters.setdefault(draft_id, set()).add(player_id)
            if entry.get("is_mock"):
                self.mock_rosters.setdefault(draft_id, set()).add(player_id)
            self._persist_decision_log_locked()
        self.state["decision_log"] = self.decision_summary()
        self.state["mock_exposure_report"] = self.mock_exposure_summary()

    def decision_summary(self) -> dict[str, object]:
        with self._audit_lock:
            recent = []
            for entry in reversed(self.decision_log[-12:]):
                positions = {
                    position: value.get("name") or value.get("status")
                    for position, value in entry.get("top_by_position", {}).items()
                }
                recent.append(
                    {
                        "draft_id": entry.get("draft_id"),
                        "decision_pick": entry.get("decision_pick"),
                        "round": entry.get("round"),
                        "status": entry.get("status"),
                        "recommended": (entry.get("recommended_player") or {}).get("name"),
                        "selected": (entry.get("selected_player") or {}).get("name"),
                        "matched_recommendation": entry.get("matched_recommendation"),
                        "top_by_position": positions,
                    }
                )
            return {"total": len(self.decision_log), "recent": recent}

    def mock_exposure_summary(
        self,
        max_drafts: int = 12,
        roster_size: int = 16,
    ) -> dict[str, object]:
        """Summarize repeated selections across recent completed mock drafts.

        This is diagnostic only. It reads the persisted decision audit so the
        report survives a Python restart, while the optional exposure cap keeps
        its existing mock-only, in-memory behavior.
        """
        with self._audit_lock:
            rosters: dict[str, set[str]] = {}
            player_details: dict[str, dict[str, object]] = {}
            last_seen: dict[str, int] = {}
            for index, entry in enumerate(self.decision_log):
                if not entry.get("is_mock") or entry.get("status") not in {
                    "selected",
                    "submitted",
                }:
                    continue
                draft_id = str(entry.get("draft_id") or "")
                selected = entry.get("selected_player") or {}
                if not isinstance(selected, dict):
                    continue
                player_id = str(selected.get("espn_id") or "")
                if not draft_id or not player_id:
                    continue
                rosters.setdefault(draft_id, set()).add(player_id)
                last_seen[draft_id] = index
                player_details[player_id] = {
                    "espn_id": player_id,
                    "name": selected.get("name") or "Unknown player",
                    "position": selected.get("position") or "",
                }

            completed = sorted(
                (
                    (last_seen[draft_id], draft_id, player_ids)
                    for draft_id, player_ids in rosters.items()
                    if len(player_ids) >= roster_size
                ),
                key=lambda item: item[0],
            )[-max_drafts:]
            counts: Counter[str] = Counter()
            for _, _, player_ids in completed:
                counts.update(player_ids)
            draft_count = len(completed)
            players = [
                {
                    **player_details[player_id],
                    "drafts": count,
                    "rate": round(count / draft_count, 3),
                }
                for player_id, count in sorted(
                    counts.items(),
                    key=lambda item: (
                        -item[1],
                        str(player_details[item[0]]["name"]),
                    ),
                )[:20]
            ]
            return {
                "draft_count": draft_count,
                "window_size": max_drafts,
                "players": players,
            }

    def decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= self.max_audit_entries:
            raise ValueError(f"decision limit must be between 1 and {self.max_audit_entries}")
        with self._audit_lock:
            return copy.deepcopy(self.decision_log[-limit:])

    def configure_mock_exposure(self, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError("mock exposure limit must be between 0 and 100")
        self.exposure_limit = percent / 100

    def _exposure_rates(self, current_draft_id: str) -> tuple[dict[str, float], int]:
        history = [
            roster
            for draft_id, roster in self.mock_rosters.items()
            if draft_id != current_draft_id and roster
        ]
        if not history:
            return {}, 0
        player_ids = set().union(*history)
        return (
            {
                player_id: sum(player_id in roster for roster in history) / len(history)
                for player_id in player_ids
            },
            len(history),
        )

    @staticmethod
    def _ids(payload: dict[str, Any], key: str) -> list[str]:
        value = payload.get(key)
        if not isinstance(value, list) or not all(isinstance(item, (str, int)) for item in value):
            raise ValueError(f"{key} must be a list of ESPN player IDs")
        result = [str(item) for item in value]
        if len(result) != len(set(result)):
            raise ValueError(f"{key} contains duplicate IDs")
        return result

    @staticmethod
    def _next_user_pick(current_pick: int, config: LeagueConfig) -> int:
        final_pick = config.teams * config.roster_size
        for overall in range(current_pick + 1, final_pick + 1):
            if EspnDraftBridge._team_for_pick(overall, config.teams) == config.user_slot:
                return overall
        return final_pick + 1

    @staticmethod
    def _expected_prior_user_selections(overall_pick: int, config: LeagueConfig) -> int:
        return sum(
            EspnDraftBridge._team_for_pick(pick, config.teams) == config.user_slot
            for pick in range(1, overall_pick)
        )

    def _readiness(
        self,
        *,
        catalog_size: int,
        mapping_rate: float,
        roster_mapping_rate: float,
        historical_rate: float,
        signal_rate: float,
        roster_reasons: list[str],
        source_status: dict[str, object] | None,
    ) -> dict[str, object]:
        reasons = list(roster_reasons)
        if catalog_size < self.minimum_catalog_size:
            reasons.append(
                f"ESPN catalog has only {catalog_size} players; at least {self.minimum_catalog_size} are required."
            )
        if mapping_rate < self.minimum_mapping_rate:
            reasons.append(
                f"Only {mapping_rate:.0%} of available ESPN players mapped; refresh complete player data."
            )
        if roster_mapping_rate < 1:
            reasons.append(
                f"Only {roster_mapping_rate:.0%} of roster players mapped; wait for a complete snapshot."
            )
        if historical_rate < self.minimum_historical_rate:
            reasons.append(
                f"Historical enrichment is {historical_rate:.0%}; load or restore nflverse data."
            )
        if signal_rate < self.minimum_signal_rate:
            reasons.append(
                f"Current signal enrichment is {signal_rate:.0%}; refresh consensus and injury signals."
            )
        if source_status is not None and not bool(source_status.get("ready")):
            for reason in source_status.get("reasons", []):
                if isinstance(reason, str) and reason not in reasons:
                    reasons.append(reason)
        return {
            "ready": not reasons,
            "label": "READY" if not reasons else "NOT READY",
            "reasons": reasons,
            "coverage": {
                "catalog_players": catalog_size,
                "available_mapping_rate": round(mapping_rate, 3),
                "roster_mapping_rate": round(roster_mapping_rate, 3),
                "historical_enrichment_rate": round(historical_rate, 3),
                "signal_enrichment_rate": round(signal_rate, 3),
            },
            "sources": dict((source_status or {}).get("sources", {})),
        }

    @staticmethod
    def _catalog(
        payload: dict[str, Any], historical: list[Player]
    ) -> tuple[list[Player], float, float]:
        raw_catalog = payload.get("player_catalog")
        if raw_catalog is None:
            return historical, 1.0, sum(bool(player.signals) for player in historical) / max(len(historical), 1)
        if not isinstance(raw_catalog, list) or not raw_catalog:
            raise ValueError("player_catalog must be a non-empty list")

        historical_by_espn = {
            player.external_ids["espn"]: player
            for player in historical
            if player.external_ids.get("espn")
        }
        seen: set[str] = set()
        merged: list[Player] = []
        enriched = 0
        allowed_positions = {"QB", "RB", "WR", "TE", "K", "DST"}
        for item in raw_catalog:
            if not isinstance(item, dict):
                raise ValueError("each player_catalog entry must be an object")
            espn_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            team = str(item.get("team") or "FA").strip().upper()
            position = str(item.get("position") or "").strip().upper().replace("D/ST", "DST")
            if not espn_id or not name:
                raise ValueError("player_catalog entries require id and name")
            if espn_id in seen:
                raise ValueError("player_catalog contains duplicate IDs")
            if position not in allowed_positions:
                raise ValueError(f"unsupported ESPN position: {position or '(blank)'}")
            try:
                rank = float(item["rank"])
                projection = float(item["projected_points"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("player_catalog rank and projected_points must be numbers") from exc
            if not math.isfinite(rank) or rank <= 0:
                raise ValueError("player_catalog rank must be a positive finite number")
            if not math.isfinite(projection) or projection < 0:
                raise ValueError("player_catalog projected_points must be a non-negative finite number")

            seen.add(espn_id)
            baseline = historical_by_espn.get(espn_id)
            if baseline:
                enriched += 1
                merged.append(
                    replace(
                        baseline,
                        name=name,
                        team=team,
                        position=position,
                        adp=rank,
                        status="ESPN CURRENT PROJECTION + HISTORICAL BASELINE",
                        projected_points_override=projection,
                    )
                )
            else:
                merged.append(
                    Player(
                        player_id=f"espn-{espn_id}",
                        name=name,
                        team=team,
                        position=position,
                        adp=rank,
                        upside=0.5,
                        risk=0.3,
                        status="ESPN CURRENT PROJECTION",
                        external_ids={"espn": espn_id},
                        projected_points_override=projection,
                    )
                )
        return merged, enriched / len(merged), sum(bool(player.signals) for player in merged) / len(merged)

    def ingest(
        self,
        payload: dict[str, Any],
        players: list[Player],
        engine: DraftEngine,
        config: LeagueConfig,
        signal_records: list[SignalRecord] | None = None,
        source_status: dict[str, object] | None = None,
    ) -> dict[str, object]:
        league_id = str(payload.get("league_id") or "").strip()
        draft_id = str(payload.get("draft_id") or "").strip()
        if not league_id or not draft_id:
            raise ValueError("league_id and draft_id are required")
        overall_pick = int(payload.get("overall_pick", 0))
        if not 1 <= overall_pick <= config.teams * config.roster_size:
            raise ValueError("overall_pick is outside the configured draft")
        if not isinstance(payload.get("on_clock"), bool):
            raise ValueError("on_clock must be true or false")
        is_mock = payload.get("is_mock", False)
        if not isinstance(is_mock, bool):
            raise ValueError("is_mock must be true or false")
        user_slot = payload.get("user_slot", config.user_slot)
        if user_slot is None:
            user_slot = config.user_slot
        user_slot = int(user_slot)
        if not 1 <= user_slot <= config.teams:
            raise ValueError(f"user_slot must be between 1 and {config.teams}")
        snapshot_config = replace(config, user_slot=user_slot)
        # Treat the page observer as an input, not as the final authority. ESPN's
        # controllingTeam can remain pinned to the user's team between turns.
        # Snake-order ownership prevents an opponent pick from arming automation
        # or creating an impossible decision-audit record.
        on_clock = bool(
            payload["on_clock"]
            and self._team_for_pick(overall_pick, snapshot_config.teams) == user_slot
        )
        available_ids = self._ids(payload, "available_player_ids")
        roster_ids = self._ids(payload, "roster_player_ids")
        if not available_ids:
            raise ValueError("available_player_ids cannot be empty")
        if set(available_ids) & set(roster_ids):
            raise ValueError("a player cannot be both available and on the roster")
        expected_roster_size = self._expected_prior_user_selections(
            overall_pick, snapshot_config
        )
        roster_set = set(roster_ids)
        confirmed = self.confirmed_rosters.get(draft_id, set())
        roster_reasons: list[str] = []
        if len(roster_ids) < expected_roster_size:
            roster_reasons.append(
                f"Roster snapshot has {len(roster_ids)} players but snake order requires at least {expected_roster_size}."
            )
        missing_confirmed = sorted(confirmed - roster_set)
        if missing_confirmed:
            roster_reasons.append(
                f"Roster snapshot lost {len(missing_confirmed)} previously confirmed selection(s)."
            )

        merged_players, enrichment_rate, signal_rate = self._catalog(payload, players)
        if signal_records:
            merged_players, _ = apply_signals(merged_players, signal_records)
            signal_rate = sum(bool(player.signals) for player in merged_players) / len(
                merged_players
            )
        by_espn_id = {
            player.external_ids["espn"]: player
            for player in merged_players
            if player.external_ids.get("espn")
        }
        mapped_available = [by_espn_id[item] for item in available_ids if item in by_espn_id]
        mapped_roster = [by_espn_id[item] for item in roster_ids if item in by_espn_id]
        mapping_rate = len(mapped_available) / len(available_ids)
        if mapping_rate < 0.5:
            raise ValueError(
                "fewer than 50% of ESPN available players mapped to the local data; refresh player data"
            )
        roster_mapping_rate = (
            len(mapped_roster) / len(roster_ids) if roster_ids else 1.0
        )
        readiness = self._readiness(
            catalog_size=len(merged_players),
            mapping_rate=mapping_rate,
            roster_mapping_rate=roster_mapping_rate,
            historical_rate=enrichment_rate,
            signal_rate=signal_rate,
            roster_reasons=roster_reasons,
            source_status=source_status,
        )
        if not roster_reasons:
            self.confirmed_rosters.setdefault(draft_id, set()).update(roster_set)
        exposure_rates: dict[str, float] = {}
        mock_history_count = 0
        if is_mock:
            if not roster_reasons:
                self.mock_rosters[draft_id] = set(roster_ids)
            exposure_rates, mock_history_count = self._exposure_rates(draft_id)
        decision_pick = (
            overall_pick
            if on_clock
            else self._next_user_pick(overall_pick, snapshot_config)
        )
        ranked = (
            engine.rank(
                mapped_available,
                mapped_roster,
                decision_pick,
                self._next_user_pick(decision_pick, snapshot_config),
                len(mapped_available),
                exposure_rates=exposure_rates,
                exposure_limit=self.exposure_limit if is_mock else 0.0,
            )
            if readiness["ready"]
            and decision_pick <= snapshot_config.teams * snapshot_config.roster_size
            else []
        )
        recommendations = ranked[:5]
        if ranked:
            self._record_decision(
                league_id,
                draft_id,
                is_mock,
                overall_pick,
                decision_pick,
                user_slot,
                on_clock,
                mapped_available,
                mapped_roster,
                ranked,
                engine,
            )
        self.state = {
            "connected": True,
            "mode": "shadow",
            "can_submit": False,
            "league_id": league_id,
            "draft_id": draft_id,
            "overall_pick": overall_pick,
            "decision_pick": decision_pick,
            "user_slot": user_slot,
            "on_clock": on_clock,
            "is_mock": is_mock,
            "mock_history_count": mock_history_count,
            "mock_exposure_limit": round(self.exposure_limit, 2),
            "match_rate": round(mapping_rate, 3),
            "catalog_size": len(merged_players),
            "historical_enrichment_rate": round(enrichment_rate, 3),
            "signal_enrichment_rate": round(signal_rate, 3),
            "mapped_roster": len(mapped_roster),
            "expected_roster_size": expected_roster_size,
            "observed_roster_size": len(roster_ids),
            "roster": [
                {**player.as_dict(), "projected_points": projected_points(player)}
                for player in mapped_roster
            ],
            "recommendations": recommendations,
            "prequeue_espn_player_ids": [item["espn_id"] for item in recommendations],
            "pending_espn_player_id": (
                recommendations[0]["espn_id"] if on_clock and recommendations else None
            ),
            "mock_command_ready": bool(on_clock and recommendations),
            "readiness": readiness,
            "decision_log": self.decision_summary(),
            "mock_exposure_report": self.mock_exposure_summary(),
            "received_at": datetime.now(timezone.utc).isoformat(),
            "message": (
                "All roster and data checks passed; mock-only recommendations are armed."
                if readiness["ready"]
                else str(readiness["reasons"][0])
            ),
        }
        return self.state
