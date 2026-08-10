from __future__ import annotations

import copy
import json
import math
import threading
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

    def __init__(self, audit_path: Path | None = None, max_audit_entries: int = 500) -> None:
        self.mock_rosters: dict[str, set[str]] = {}
        self.exposure_limit = 0.0
        self.audit_path = audit_path
        self.max_audit_entries = max_audit_entries
        self._audit_lock = threading.Lock()
        self.decision_log = self._load_decision_log()
        self.state: dict[str, object] = {
            "connected": False,
            "mode": "shadow",
            "can_submit": False,
            "message": "Waiting for an ESPN mock-draft snapshot.",
            "decision_log": self.decision_summary(),
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
            return payload[-self.max_audit_entries :]
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
            "market_rank": candidate.get("market_rank"),
            "market_reach": candidate.get("market_reach"),
            "draft_score": candidate.get("draft_score"),
            "survival_probability": candidate.get("survival_probability"),
            "top_driver": top_driver,
            "components": candidate.get("components", {}),
            "contributions": contributions,
        }

    @staticmethod
    def _basic_player(player: Player) -> dict[str, object]:
        return {
            "espn_id": player.external_ids.get("espn"),
            "name": player.name,
            "team": player.team,
            "position": player.position,
            "projected_points": projected_points(player),
            "market_rank": DraftEngine._market_rank(player),
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
            self._persist_decision_log_locked()
        self.state["decision_log"] = self.decision_summary()

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
            round_number = (overall - 1) // config.teams + 1
            within_round = (overall - 1) % config.teams + 1
            slot = within_round if round_number % 2 else config.teams + 1 - within_round
            if slot == config.user_slot:
                return overall
        return final_pick + 1

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
        available_ids = self._ids(payload, "available_player_ids")
        roster_ids = self._ids(payload, "roster_player_ids")
        if not available_ids:
            raise ValueError("available_player_ids cannot be empty")
        if set(available_ids) & set(roster_ids):
            raise ValueError("a player cannot be both available and on the roster")
        exposure_rates: dict[str, float] = {}
        mock_history_count = 0
        if is_mock:
            self.mock_rosters[draft_id] = set(roster_ids)
            exposure_rates, mock_history_count = self._exposure_rates(draft_id)

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
        decision_pick = (
            overall_pick
            if payload["on_clock"]
            else self._next_user_pick(overall_pick, snapshot_config)
        )
        ranked = engine.rank(
            mapped_available,
            mapped_roster,
            decision_pick,
            self._next_user_pick(decision_pick, snapshot_config),
            len(mapped_available),
            exposure_rates=exposure_rates,
            exposure_limit=self.exposure_limit if is_mock else 0.0,
        ) if decision_pick <= snapshot_config.teams * snapshot_config.roster_size else []
        recommendations = ranked[:5]
        if ranked:
            self._record_decision(
                league_id,
                draft_id,
                is_mock,
                overall_pick,
                decision_pick,
                user_slot,
                payload["on_clock"],
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
            "on_clock": payload["on_clock"],
            "is_mock": is_mock,
            "mock_history_count": mock_history_count,
            "mock_exposure_limit": round(self.exposure_limit, 2),
            "match_rate": round(mapping_rate, 3),
            "catalog_size": len(merged_players),
            "historical_enrichment_rate": round(enrichment_rate, 3),
            "signal_enrichment_rate": round(signal_rate, 3),
            "mapped_roster": len(mapped_roster),
            "roster": [
                {**player.as_dict(), "projected_points": projected_points(player)}
                for player in mapped_roster
            ],
            "recommendations": recommendations,
            "prequeue_espn_player_ids": [item["espn_id"] for item in recommendations],
            "pending_espn_player_id": (
                recommendations[0]["espn_id"] if payload["on_clock"] and recommendations else None
            ),
            "mock_command_ready": bool(payload["on_clock"] and recommendations),
            "decision_log": self.decision_summary(),
            "received_at": datetime.now(timezone.utc).isoformat(),
            "message": "Recommendations are precomputed; the companion may submit only in mock mode.",
        }
        return self.state
