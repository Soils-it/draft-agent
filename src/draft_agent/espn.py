from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .config import LeagueConfig
from .engine import DraftEngine
from .models import Player
from .scoring import projected_points


class EspnDraftBridge:
    """Validate browser-observed ESPN state and calculate a shadow recommendation.

    The bridge deliberately has no browser credentials and no submit-pick method.
    A later, mock-draft-tested companion may poll an explicit command endpoint.
    """

    def __init__(self) -> None:
        self.state: dict[str, object] = {
            "connected": False,
            "mode": "shadow",
            "can_submit": False,
            "message": "Waiting for an ESPN mock-draft snapshot.",
        }

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

        merged_players, enrichment_rate, signal_rate = self._catalog(payload, players)
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
        recommendations = engine.rank(
            mapped_available,
            mapped_roster,
            decision_pick,
            self._next_user_pick(decision_pick, snapshot_config),
            5,
        ) if decision_pick <= snapshot_config.teams * snapshot_config.roster_size else []
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
            "received_at": datetime.now(timezone.utc).isoformat(),
            "message": "Recommendations are precomputed; the companion may submit only in mock mode.",
        }
        return self.state
