from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import LeagueConfig, STANDARD_PPR
from .engine import DraftEngine
from .models import Player
from .scoring import projected_points


SLEEPER_API_ORIGIN = "https://api.sleeper.app"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ValueError("Sleeper API redirects are not allowed")


class SleeperApiClient:
    """Bounded, unauthenticated GET-only client for two fixed Sleeper paths."""

    def __init__(
        self,
        fetcher: Callable[[str], object] | None = None,
        *,
        timeout_seconds: float = 4.0,
    ) -> None:
        self.fetcher = fetcher
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def validate_identifier(value: object, label: str) -> str:
        identifier = str(value or "").strip()
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError(
                f"{label} must be 1-100 letters, numbers, underscores, or hyphens"
            )
        return identifier

    def _get(self, path: str, max_bytes: int) -> object:
        if not path.startswith("/v1/draft/") or "?" in path or "#" in path:
            raise ValueError("invalid Sleeper API path")
        if self.fetcher is not None:
            return copy.deepcopy(self.fetcher(path))
        request = Request(
            SLEEPER_API_ORIGIN + path,
            headers={"Accept": "application/json", "User-Agent": "draft-agent/0.1"},
            method="GET",
        )
        try:
            with build_opener(_RejectRedirects()).open(
                request, timeout=self.timeout_seconds
            ) as response:
                status = response.getcode()
                if status != 200:
                    raise ValueError(f"Sleeper API returned HTTP {status}")
                raw = response.read(max_bytes + 1)
        except HTTPError as exc:
            raise ValueError(f"Sleeper API returned HTTP {exc.code}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ValueError(f"Sleeper API request failed: {exc}") from exc
        if len(raw) > max_bytes:
            raise ValueError("Sleeper API response exceeded the size limit")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Sleeper API returned invalid JSON") from exc

    def draft(self, draft_id: object) -> object:
        identifier = self.validate_identifier(draft_id, "draft_id")
        return self._get(f"/v1/draft/{identifier}", 256_000)

    def picks(self, draft_id: object) -> object:
        identifier = self.validate_identifier(draft_id, "draft_id")
        return self._get(f"/v1/draft/{identifier}/picks", 1_000_000)


class SleeperDraftBridge:
    """Validate public Sleeper draft state and produce recommendations only."""

    teams = 16
    rounds = 16
    maximum_picks = teams * rounds
    minimum_mapping_rate = 0.8
    minimum_mapped_players = 100

    def __init__(self, client: SleeperApiClient | None = None) -> None:
        self.client = client or SleeperApiClient()
        self._pick_histories: dict[str, tuple[tuple[object, ...], ...]] = {}
        self.state = self._empty_state(
            "Waiting for an explicit Sleeper draft synchronization."
        )

    @staticmethod
    def _empty_state(message: str) -> dict[str, object]:
        return {
            "connected": False,
            "provider": "sleeper",
            "mode": "shadow",
            "can_submit": False,
            "recommendations": [],
            "roster": [],
            "message": message,
            "readiness": {
                "ready": False,
                "label": "NOT READY",
                "reasons": [message],
                "coverage": {},
                "sources": {},
            },
        }

    def invalidate(self, message: str) -> None:
        self.state = self._empty_state(message)

    @staticmethod
    def _integer(value: object, label: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be an integer")
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{label} must be an integer")
        return parsed

    @classmethod
    def _team_for_pick(cls, overall: int) -> int:
        round_number = (overall - 1) // cls.teams + 1
        within_round = (overall - 1) % cls.teams + 1
        return within_round if round_number % 2 else cls.teams + 1 - within_round

    @classmethod
    def _next_user_pick(cls, current_pick: int, user_slot: int) -> int | None:
        for overall in range(current_pick, cls.maximum_picks + 1):
            if cls._team_for_pick(overall) == user_slot:
                return overall
        return None

    @classmethod
    def _metadata(
        cls, payload: object, draft_id: str, user_id: str
    ) -> tuple[str, str, int, str, dict[int, str]]:
        if not isinstance(payload, dict):
            raise ValueError("Sleeper draft metadata must be an object")
        if str(payload.get("draft_id") or "") != draft_id:
            raise ValueError("Sleeper draft metadata returned a different draft_id")
        league_id = SleeperApiClient.validate_identifier(
            payload.get("league_id"), "league_id"
        )
        if str(payload.get("type") or "").strip().lower() != "snake":
            raise ValueError("Sleeper draft must use snake order")
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"pre_draft", "drafting", "complete"}:
            raise ValueError("Sleeper draft status must be pre_draft, drafting, or complete")
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("Sleeper draft settings are missing")
        if cls._integer(settings.get("teams"), "Sleeper team count") != cls.teams:
            raise ValueError("Sleeper draft must contain exactly 16 teams")
        if cls._integer(settings.get("rounds"), "Sleeper round count") != cls.rounds:
            raise ValueError("Sleeper draft must contain exactly 16 rounds")
        supported_slots = {
            "slots_qb": 1,
            "slots_rb": 2,
            "slots_wr": 2,
            "slots_te": 1,
            "slots_flex": 1,
            "slots_sflex": 0,
            "slots_k": 1,
            "slots_def": 1,
            "slots_bn": 7,
        }
        for key, expected in supported_slots.items():
            if cls._integer(settings.get(key), f"Sleeper {key}") != expected:
                raise ValueError(
                    "Sleeper roster settings must match the supported normal 1-QB format"
                )
        non_drafted_slots = {"slots_ir", "slots_taxi"}
        for raw_key, raw_value in settings.items():
            key = str(raw_key)
            if (
                key.startswith("slots_")
                and key not in supported_slots
                and key not in non_drafted_slots
                and cls._integer(raw_value, f"Sleeper {key}") != 0
            ):
                raise ValueError(
                    "Sleeper roster settings contain an unsupported drafted position"
                )

        raw_order = payload.get("draft_order")
        if not isinstance(raw_order, dict) or len(raw_order) != cls.teams:
            raise ValueError("Sleeper draft_order must contain all 16 users")
        order: dict[str, int] = {}
        for raw_user, raw_slot in raw_order.items():
            checked_user = SleeperApiClient.validate_identifier(raw_user, "draft user ID")
            order[checked_user] = cls._integer(raw_slot, "draft_order slot")
        if set(order.values()) != set(range(1, cls.teams + 1)):
            raise ValueError("Sleeper draft_order slots must be unique 1 through 16")
        if user_id not in order:
            raise ValueError("Sleeper user_id is not present in draft_order")

        raw_rosters = payload.get("slot_to_roster_id")
        if not isinstance(raw_rosters, dict) or len(raw_rosters) != cls.teams:
            raise ValueError("Sleeper slot_to_roster_id must contain all 16 slots")
        rosters: dict[int, str] = {}
        for raw_slot, raw_roster in raw_rosters.items():
            slot = cls._integer(raw_slot, "slot_to_roster_id slot")
            roster_id = SleeperApiClient.validate_identifier(raw_roster, "roster_id")
            if slot in rosters:
                raise ValueError("Sleeper slot_to_roster_id contains duplicate slots")
            rosters[slot] = roster_id
        if set(rosters) != set(range(1, cls.teams + 1)):
            raise ValueError("Sleeper slot_to_roster_id slots must be 1 through 16")
        if len(set(rosters.values())) != cls.teams:
            raise ValueError("Sleeper roster IDs must be unique")
        user_slot = order[user_id]
        return league_id, status, user_slot, rosters[user_slot], rosters

    @classmethod
    def _picks(
        cls,
        payload: object,
        draft_id: str,
        rosters: dict[int, str],
    ) -> tuple[tuple[tuple[object, ...], ...], list[str]]:
        if not isinstance(payload, list):
            raise ValueError("Sleeper picks must be a list")
        if len(payload) > cls.maximum_picks:
            raise ValueError("Sleeper draft contains more than 256 picks")
        parsed: list[tuple[object, ...]] = []
        pick_numbers: set[int] = set()
        player_ids: set[str] = set()
        for raw in payload:
            if not isinstance(raw, dict):
                raise ValueError("Each Sleeper pick must be an object")
            if str(raw.get("draft_id") or "") != draft_id:
                raise ValueError("A Sleeper pick belongs to a different draft")
            pick_no = cls._integer(raw.get("pick_no"), "pick_no")
            round_number = cls._integer(raw.get("round"), "round")
            draft_slot = cls._integer(raw.get("draft_slot"), "draft_slot")
            roster_id = SleeperApiClient.validate_identifier(
                raw.get("roster_id"), "pick roster_id"
            )
            player_id = SleeperApiClient.validate_identifier(
                raw.get("player_id"), "Sleeper player_id"
            )
            if pick_no in pick_numbers:
                raise ValueError("Sleeper picks contain duplicate pick numbers")
            if player_id in player_ids:
                raise ValueError("Sleeper picks contain duplicate player IDs")
            expected_round = (pick_no - 1) // cls.teams + 1
            expected_slot = cls._team_for_pick(pick_no) if pick_no > 0 else 0
            if not 1 <= pick_no <= cls.maximum_picks:
                raise ValueError("Sleeper pick_no is outside the 256-pick draft")
            if round_number != expected_round or draft_slot != expected_slot:
                raise ValueError("Sleeper pick round or draft_slot conflicts with snake order")
            if rosters.get(draft_slot) != roster_id:
                raise ValueError("Sleeper pick roster_id conflicts with slot_to_roster_id")
            pick_numbers.add(pick_no)
            player_ids.add(player_id)
            parsed.append((pick_no, player_id, round_number, draft_slot, roster_id))
        parsed.sort(key=lambda item: int(item[0]))
        if [item[0] for item in parsed] != list(range(1, len(parsed) + 1)):
            raise ValueError("Sleeper picks must be a contiguous prefix starting at pick 1")
        return tuple(parsed), [str(item[1]) for item in parsed]

    def _synchronize(
        self,
        draft_id: str,
        user_id: str,
        players: list[Player],
        engine: DraftEngine,
        config: LeagueConfig,
    ) -> dict[str, object]:
        if config.teams != self.teams or config.roster_size != self.rounds:
            raise ValueError("Configure a 16-team, 16-round league before syncing Sleeper")
        if config.profile_id != STANDARD_PPR or config.is_superflex:
            raise ValueError("Sleeper sync currently supports only the normal 1-QB profile")
        metadata = self.client.draft(draft_id)
        picks_payload = self.client.picks(draft_id)
        league_id, status, user_slot, user_roster_id, rosters = self._metadata(
            metadata, draft_id, user_id
        )
        if user_slot != config.user_slot:
            raise ValueError(
                f"Sleeper user slot {user_slot} does not match configured draft slot "
                f"{config.user_slot}"
            )
        history, drafted_ids = self._picks(picks_payload, draft_id, rosters)
        if status == "pre_draft" and history:
            raise ValueError("Sleeper pre_draft metadata cannot contain completed picks")
        if status == "complete" and len(history) != self.maximum_picks:
            raise ValueError("Sleeper complete metadata must contain all 256 picks")
        prior = self._pick_histories.get(draft_id, ())
        if len(history) < len(prior):
            raise ValueError("Sleeper pick history regressed; refusing stale draft state")
        if history[: len(prior)] != prior:
            raise ValueError("Sleeper pick history mutated; refusing inconsistent draft state")

        by_sleeper: dict[str, Player] = {}
        for player in players:
            sleeper_id = str(player.external_ids.get("sleeper") or "").strip()
            if not sleeper_id:
                continue
            SleeperApiClient.validate_identifier(sleeper_id, "loaded Sleeper player ID")
            if sleeper_id in by_sleeper:
                raise ValueError(f"Duplicate loaded Sleeper player mapping: {sleeper_id}")
            by_sleeper[sleeper_id] = player
        mapping_rate = len(by_sleeper) / max(len(players), 1)
        if (
            mapping_rate < self.minimum_mapping_rate
            or len(by_sleeper) < self.minimum_mapped_players
        ):
            raise ValueError(
                "Sleeper player mapping coverage is too low; load complete free player data first"
            )
        unknown = [player_id for player_id in drafted_ids if player_id not in by_sleeper]
        if unknown:
            raise ValueError(
                f"Sleeper drafted player {unknown[0]} has no unique direct local mapping"
            )
        drafted = set(drafted_ids)
        available = [player for sleeper_id, player in by_sleeper.items() if sleeper_id not in drafted]
        user_pick_ids = [
            str(item[1]) for item in history if str(item[4]) == user_roster_id
        ]
        roster = [by_sleeper[player_id] for player_id in user_pick_ids]
        current_pick = len(history) + 1
        decision_pick = (
            self._next_user_pick(current_pick, user_slot)
            if current_pick <= self.maximum_picks
            else None
        )
        next_pick = (
            self._next_user_pick(decision_pick + 1, user_slot)
            if decision_pick is not None
            else None
        )
        ranked = (
            engine.rank(
                available,
                roster,
                decision_pick,
                next_pick or self.maximum_picks + 1,
                len(available),
            )
            if decision_pick is not None and available
            else []
        )
        recommendations = ranked[:5]
        received_at = datetime.now(timezone.utc).isoformat()
        new_state: dict[str, object] = {
            "connected": True,
            "provider": "sleeper",
            "mode": "shadow",
            "can_submit": False,
            "draft_id": draft_id,
            "league_id": league_id,
            "status": status,
            "teams": self.teams,
            "rounds": self.rounds,
            "user_slot": user_slot,
            "user_roster_id": user_roster_id,
            "current_pick": current_pick,
            "decision_pick": decision_pick,
            "complete": len(history) == self.maximum_picks,
            "mapping_rate": round(mapping_rate, 3),
            "mapped_players": len(by_sleeper),
            "received_at": received_at,
            "roster": [
                {**player.as_dict(), "projected_points": round(projected_points(player), 1)}
                for player in roster
            ],
            "recommendations": recommendations,
            "readiness": {
                "ready": True,
                "label": "READY",
                "reasons": [],
                "coverage": {
                    "mapping_rate": round(mapping_rate, 3),
                    "mapped_players": len(by_sleeper),
                    "drafted_players": len(drafted),
                },
                "sources": {
                    "sleeper": {
                        "connected": True,
                        "received_at": received_at,
                        "read_only": True,
                    }
                },
            },
            "message": (
                "Draft complete; no recommendation is available."
                if len(history) == self.maximum_picks
                else "Validated public Sleeper draft data; recommendations are read-only."
            ),
        }
        self._pick_histories[draft_id] = history
        self.state = new_state
        return copy.deepcopy(new_state)

    def synchronize(
        self,
        draft_id: object,
        user_id: object,
        players: list[Player],
        engine: DraftEngine,
        config: LeagueConfig,
    ) -> dict[str, object]:
        try:
            checked_draft = SleeperApiClient.validate_identifier(draft_id, "draft_id")
            checked_user = SleeperApiClient.validate_identifier(user_id, "user_id")
            return self._synchronize(
                checked_draft, checked_user, players, engine, config
            )
        except (OSError, TimeoutError, TypeError, ValueError) as exc:
            message = f"Sleeper sync failed: {exc}"
            self.invalidate(message)
            raise ValueError(message) from exc
