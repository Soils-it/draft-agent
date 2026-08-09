from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import LeagueConfig
from .engine import DraftEngine
from .models import Player


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
        available_ids = self._ids(payload, "available_player_ids")
        roster_ids = self._ids(payload, "roster_player_ids")
        if not available_ids:
            raise ValueError("available_player_ids cannot be empty")
        if set(available_ids) & set(roster_ids):
            raise ValueError("a player cannot be both available and on the roster")

        by_espn_id = {
            player.external_ids["espn"]: player
            for player in players
            if player.external_ids.get("espn")
        }
        mapped_available = [by_espn_id[item] for item in available_ids if item in by_espn_id]
        mapped_roster = [by_espn_id[item] for item in roster_ids if item in by_espn_id]
        match_rate = len(mapped_available) / len(available_ids)
        if match_rate < 0.5:
            raise ValueError(
                "fewer than 50% of ESPN available players mapped to the local data; refresh player data"
            )
        recommendations = []
        if payload["on_clock"]:
            recommendations = engine.rank(
                mapped_available,
                mapped_roster,
                overall_pick,
                self._next_user_pick(overall_pick, config),
                5,
            )
        self.state = {
            "connected": True,
            "mode": "shadow",
            "can_submit": False,
            "league_id": league_id,
            "draft_id": draft_id,
            "overall_pick": overall_pick,
            "on_clock": payload["on_clock"],
            "match_rate": round(match_rate, 3),
            "mapped_roster": len(mapped_roster),
            "recommendations": recommendations,
            "pending_espn_player_id": recommendations[0]["espn_id"] if recommendations else None,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "message": "Shadow mode only: no ESPN pick will be submitted.",
        }
        return self.state
