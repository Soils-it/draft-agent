from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import (
    LEAGUE_PROFILES,
    LeagueConfig,
    league_config_for_profile,
    league_profiles_payload,
)
from .data import demo_players
from .espn import EspnDraftBridge
from .models import Player
from .providers import (
    CANONICAL_NFL_TEAMS,
    NFLVERSE_SCHEDULES_TIMESTAMP_URL,
    NFLVERSE_SCHEDULES_URL,
    VEGAS_ATTRIBUTION,
    VEGAS_LICENSE,
    VEGAS_MAX_AGE_HOURS,
    NflverseProvider,
    NflverseVegasProvider,
    VegasProviderResult,
    VegasSnapshot,
    apply_vegas_context,
)
from .signals import FreeSignalProvider, SignalRecord, apply_signals
from .session import DraftSession


SESSION = DraftSession(demo_players())
OVERRIDE_SECONDS = 20
SIMULATION_SAMPLES = 200
SIGNAL_MAX_AGE_HOURS = 48
ESPN_SNAPSHOT_MAX_AGE_SECONDS = 10


def _demo_data_source(warning: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "name": "generated demo data",
        "kind": "demo",
        "season": None,
        "cached": False,
    }
    if warning:
        result["restore_warning"] = warning
    return result


DATA_SOURCE: dict[str, object] = _demo_data_source()
SIGNAL_RECORDS: list[SignalRecord] = []
VEGAS_RESULT: VegasProviderResult | None = None
PLAYER_PREFERENCES: dict[str, object] = {
    "prefer": [],
    "fade": [],
    "never": [],
    "mock_exposure_limit": 0,
}
PREFERENCE_PATH = Path(".cache/player_preferences.json")
RUNTIME_SETTINGS_PATH = Path(".cache/draft_settings.json")
DECISION_LOG_PATH = Path(".cache/draft_decisions.json")
ESPN_BRIDGE = EspnDraftBridge(DECISION_LOG_PATH)


def _configure_session(session: DraftSession) -> None:
    session.engine.simulation_samples = SIMULATION_SAMPLES
    session.engine.set_preferences(
        list(PLAYER_PREFERENCES["prefer"]),
        list(PLAYER_PREFERENCES["fade"]),
        list(PLAYER_PREFERENCES["never"]),
    )


def _replace_session(
    players: list[Player] | None = None,
    config: LeagueConfig | None = None,
) -> None:
    """Rebuild the local mock without discarding its active data or strategy."""
    global SESSION
    old = SESSION
    weights = dict(old.engine.weights.__dict__)
    SESSION = DraftSession(
        list(old.players.values()) if players is None else players,
        old.config if config is None else config,
    )
    SESSION.engine.weights.update(weights)
    _configure_session(SESSION)


def _age_seconds(timestamp: object, now: datetime | None = None) -> float | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    age = (current - parsed.astimezone(timezone.utc)).total_seconds()
    return None if age < -300 else max(0.0, age)


def _usable_vegas_snapshot(now: datetime | None = None) -> VegasSnapshot | None:
    if VEGAS_RESULT is None or not VEGAS_RESULT.usable(now):
        return None
    return VEGAS_RESULT.snapshot


def _sync_session_vegas(now: datetime | None = None) -> int:
    """Apply fresh Vegas fields, or remove them without resetting draft state."""
    snapshot = _usable_vegas_snapshot(now)
    enriched, matched = apply_vegas_context(
        list(SESSION.players.values()),
        snapshot,
    )
    SESSION.players = {player.player_id: player for player in enriched}
    if snapshot is None:
        ESPN_BRIDGE.neutralize_vegas_ranking()
    return matched


def _vegas_health(now: datetime | None = None) -> dict[str, object]:
    current = now or datetime.now(timezone.utc)
    result = VEGAS_RESULT
    snapshot = result.snapshot if result else None
    age = _age_seconds(snapshot.fetched_at, current) if snapshot else None
    fresh = bool(result and result.usable(current))
    status = result.status if result else "unavailable"
    if snapshot is not None and not fresh and status in {"cached", "refreshed", "fallback"}:
        status = "stale"
    return {
        "loaded": snapshot is not None,
        "fresh": fresh,
        "usable": fresh,
        "optional": True,
        "status": status,
        "error": result.error if result else None,
        "cached": bool(result.cached) if result else False,
        "fetched_at": snapshot.fetched_at if snapshot else None,
        "age_hours": round(age / 3600, 1) if age is not None else None,
        "max_age_hours": VEGAS_MAX_AGE_HOURS,
        "dataset_timestamp": snapshot.dataset_timestamp if snapshot else None,
        "season": snapshot.season if snapshot else None,
        "coverage": snapshot.coverage if snapshot else 0,
        "required_coverage": len(CANONICAL_NFL_TEAMS),
        "lined_games": snapshot.lined_games if snapshot else 0,
        "matched_players": sum(
            "vegas_games" in player.signals for player in SESSION.players.values()
        ),
        "source_url": snapshot.source_url if snapshot else NFLVERSE_SCHEDULES_URL,
        "timestamp_url": (
            snapshot.timestamp_url if snapshot else NFLVERSE_SCHEDULES_TIMESTAMP_URL
        ),
        "attribution": snapshot.attribution if snapshot else VEGAS_ATTRIBUTION,
        "license": snapshot.license if snapshot else VEGAS_LICENSE,
        "team_totals": {
            team: total.as_dict() for team, total in sorted(snapshot.teams.items())
        }
        if snapshot
        else {},
        "label": "Team-level market context; not player props or a projection replacement.",
    }


def _data_health(now: datetime | None = None) -> dict[str, object]:
    _sync_session_vegas(now)
    players = list(SESSION.players.values())
    historical_players = sum(
        player.player_id.startswith("nflverse-") for player in players
    )
    signaled_players = sum("consensus_rank" in player.signals for player in players)
    mapped_espn_ids = sum(bool(player.external_ids.get("espn")) for player in players)
    historical_claim = DATA_SOURCE.get("kind") == "nflverse"
    historical_loaded = historical_claim and historical_players >= 100
    signal_claim = bool(DATA_SOURCE.get("signals")) and bool(SIGNAL_RECORDS)
    signals_loaded = (
        signal_claim and len(SIGNAL_RECORDS) >= 100 and signaled_players >= 75
    )
    signal_age_seconds = _age_seconds(DATA_SOURCE.get("signals_fetched_at"), now)
    signals_fresh = bool(
        signals_loaded
        and signal_age_seconds is not None
        and signal_age_seconds <= SIGNAL_MAX_AGE_HOURS * 3600
    )
    reasons: list[str] = []
    if not historical_claim:
        reasons.append("Load nflverse historical data; generated demo data is not draft-ready.")
    elif not historical_loaded:
        reasons.append("Historical source metadata does not match the active player pool.")
    if not signal_claim:
        reasons.append("Load current consensus, identity, injury, and trend signals.")
    elif not signals_loaded:
        reasons.append("Cached current-signal coverage is insufficient for live drafting.")
    elif signal_age_seconds is None:
        reasons.append("Current-signal freshness is unknown; refresh complete free data.")
    elif not signals_fresh:
        reasons.append(
            f"Current signals are older than {SIGNAL_MAX_AGE_HOURS} hours; refresh complete free data."
        )
    historical_age = _age_seconds(DATA_SOURCE.get("fetched_at"), now)
    return {
        "ready": not reasons,
        "reasons": reasons,
        "sources": {
            "historical": {
                "loaded": historical_loaded,
                "name": DATA_SOURCE.get("name"),
                "season": DATA_SOURCE.get("season"),
                "cached": bool(DATA_SOURCE.get("cached")),
                "fetched_at": DATA_SOURCE.get("fetched_at"),
                "age_hours": round(historical_age / 3600, 1)
                if historical_age is not None
                else None,
                "players": historical_players,
                "mapped_espn_ids": mapped_espn_ids,
            },
            "signals": {
                "loaded": signals_loaded,
                "fresh": signals_fresh,
                "cached": bool(DATA_SOURCE.get("signals_cached")),
                "fetched_at": DATA_SOURCE.get("signals_fetched_at"),
                "age_hours": round(signal_age_seconds / 3600, 1)
                if signal_age_seconds is not None
                else None,
                "max_age_hours": SIGNAL_MAX_AGE_HOURS,
                "matched_players": signaled_players,
                "records": len(SIGNAL_RECORDS),
            },
            "vegas": _vegas_health(now),
        },
    }


def _espn_state_payload(source_health: dict[str, object]) -> dict[str, object]:
    vegas_health = dict(source_health.get("sources", {})).get("vegas")
    if isinstance(vegas_health, dict) and vegas_health.get("usable") is False:
        ESPN_BRIDGE.neutralize_vegas_ranking()
    state = copy.deepcopy(ESPN_BRIDGE.state)
    readiness = copy.deepcopy(
        state.get(
            "readiness",
            {"ready": False, "label": "NOT READY", "reasons": [], "coverage": {}, "sources": {}},
        )
    )
    reasons = [str(reason) for reason in readiness.get("reasons", [])]
    for reason in source_health.get("reasons", []):
        if isinstance(reason, str) and reason not in reasons:
            reasons.append(reason)
    snapshot_age = _age_seconds(state.get("received_at"))
    snapshot_fresh = bool(
        state.get("connected")
        and snapshot_age is not None
        and snapshot_age <= ESPN_SNAPSHOT_MAX_AGE_SECONDS
    )
    if state.get("connected") and not snapshot_fresh:
        reasons.append(
            f"ESPN snapshot is older than {ESPN_SNAPSHOT_MAX_AGE_SECONDS} seconds; sync the companion."
        )
    readiness["ready"] = bool(readiness.get("ready")) and snapshot_fresh and not reasons
    readiness["label"] = "READY" if readiness["ready"] else "NOT READY"
    readiness["reasons"] = reasons
    readiness["sources"] = {
        **dict(source_health.get("sources", {})),
        "espn": {
            "connected": bool(state.get("connected")),
            "received_at": state.get("received_at"),
            "age_seconds": round(snapshot_age, 1) if snapshot_age is not None else None,
            "fresh": snapshot_fresh,
            "max_age_seconds": ESPN_SNAPSHOT_MAX_AGE_SECONDS,
        },
    }
    state["readiness"] = readiness
    if not readiness["ready"]:
        state["recommendations"] = []
        state["prequeue_espn_player_ids"] = []
        state["pending_espn_player_id"] = None
        state["mock_command_ready"] = False
    return state


def _restore_cached_data(
    nflverse_provider: NflverseProvider | None = None,
    signal_provider: FreeSignalProvider | None = None,
    vegas_provider: NflverseVegasProvider | None = None,
) -> bool:
    """Restore complete caches at startup without falling through to network."""
    global DATA_SOURCE, SIGNAL_RECORDS, VEGAS_RESULT
    nflverse_provider = nflverse_provider or NflverseProvider()
    signal_provider = signal_provider or FreeSignalProvider()
    vegas_provider = vegas_provider or NflverseVegasProvider()
    try:
        VEGAS_RESULT = vegas_provider.load_cached()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        VEGAS_RESULT = VegasProviderResult(
            None,
            "invalid",
            True,
            "Cached Vegas data was invalid; refresh complete free data.",
        )
    try:
        historical = nflverse_provider.load_cached()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        historical = None
        warning = "Cached nflverse data was invalid; reload complete free data."
    else:
        warning = None
    if historical is None:
        SIGNAL_RECORDS = []
        _replace_session(demo_players())
        _sync_session_vegas()
        DATA_SOURCE = _demo_data_source(warning)
        return False
    _replace_session(historical.players)
    DATA_SOURCE = {
        "name": historical.source,
        "kind": "nflverse",
        "season": historical.season,
        "cached": True,
        "fetched_at": historical.fetched_at,
        "warning": "Historical baseline only; not a current expert projection.",
        "mapped_espn_ids": historical.mapped_espn_ids,
    }
    try:
        signals = signal_provider.load_cached()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        signals = None
        DATA_SOURCE["restore_warning"] = (
            "Cached current signals were invalid; refresh complete free data."
        )
    if signals is None:
        SIGNAL_RECORDS = []
        _sync_session_vegas()
        return False
    SIGNAL_RECORDS = signals.records
    enriched, matched = apply_signals(list(SESSION.players.values()), signals.records)
    _replace_session(enriched)
    _sync_session_vegas()
    DATA_SOURCE = {
        **DATA_SOURCE,
        "signals": signals.sources,
        "signals_cached": True,
        "signals_fetched_at": signals.fetched_at,
        "signals_matched": matched,
        "signals_available": len(signals.records),
    }
    return bool(_data_health()["ready"])


def validate_preferences(values: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in ("prefer", "fade", "never"):
        names = values.get(key, [])
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError(f"{key} must be a list of player names")
        cleaned = list(dict.fromkeys(name.strip() for name in names if name.strip()))
        if len(cleaned) > 50 or any(len(name) > 80 for name in cleaned):
            raise ValueError(f"{key} contains too many or overly long player names")
        result[key] = cleaned
    exposure = int(values.get("mock_exposure_limit", 0))
    if not 0 <= exposure <= 100:
        raise ValueError("mock_exposure_limit must be between 0 and 100")
    result["mock_exposure_limit"] = exposure
    return result


def _save_preferences() -> None:
    PREFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFERENCE_PATH.write_text(json.dumps(PLAYER_PREFERENCES, indent=2), encoding="utf-8")


def _load_preferences() -> dict[str, object] | None:
    if not PREFERENCE_PATH.exists() or PREFERENCE_PATH.stat().st_size > 32_000:
        return None
    try:
        payload = json.loads(PREFERENCE_PATH.read_text(encoding="utf-8"))
        return validate_preferences(payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _state_payload() -> dict[str, object]:
    # While ESPN is connected its bridge already owns the live recommendation.
    # Avoid running a second Monte Carlo draft on every snapshot and dashboard poll.
    source_health = _data_health()
    payload = SESSION.as_dict(include_recommendations=not bool(ESPN_BRIDGE.state.get("connected")))
    espn_state = _espn_state_payload(source_health)
    payload["settings"] = {
        "league_profile": SESSION.config.profile_id,
        "roster_size": SESSION.config.roster_size,
        "user_slot": SESSION.config.user_slot,
        "override_seconds": OVERRIDE_SECONDS,
        "simulation_samples": SESSION.engine.simulation_samples,
    }
    payload["league_profiles"] = league_profiles_payload()
    payload["data_source"] = {
        **DATA_SOURCE,
        "freshness": source_health["sources"],
    }
    payload["preferences"] = PLAYER_PREFERENCES
    payload["espn"] = espn_state
    payload["readiness"] = espn_state["readiness"]
    return payload


def validate_settings(
    values: dict[str, Any], current_slot: int, current_seconds: int, teams: int,
    current_samples: int = 200,
) -> tuple[int, int, int]:
    slot = int(values.get("user_slot", current_slot))
    seconds = int(values.get("override_seconds", current_seconds))
    samples = int(values.get("simulation_samples", current_samples))
    if not 1 <= slot <= teams:
        raise ValueError(f"user_slot must be between 1 and {teams}")
    if not 5 <= seconds <= 120:
        raise ValueError("override_seconds must be between 5 and 120")
    if not 50 <= samples <= 2000:
        raise ValueError("simulation_samples must be between 50 and 2000")
    return slot, seconds, samples


def _validate_profile(values: dict[str, Any], current_profile: str) -> str:
    profile_id = str(values.get("league_profile", current_profile)).strip()
    if profile_id not in LEAGUE_PROFILES:
        raise ValueError("league_profile is not supported")
    return profile_id


def _runtime_settings_payload() -> dict[str, object]:
    return {
        "league_profile": SESSION.config.profile_id,
        "user_slot": SESSION.config.user_slot,
        "override_seconds": OVERRIDE_SECONDS,
        "simulation_samples": SESSION.engine.simulation_samples,
    }


def _save_runtime_settings() -> None:
    RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RUNTIME_SETTINGS_PATH.with_suffix(
        RUNTIME_SETTINGS_PATH.suffix + ".tmp"
    )
    temporary.write_text(
        json.dumps(_runtime_settings_payload(), indent=2),
        encoding="utf-8",
    )
    temporary.replace(RUNTIME_SETTINGS_PATH)


def _load_runtime_settings() -> dict[str, object] | None:
    try:
        if (
            not RUNTIME_SETTINGS_PATH.exists()
            or RUNTIME_SETTINGS_PATH.stat().st_size > 32_000
        ):
            return None
        payload = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        profile_id = _validate_profile(payload, SESSION.config.profile_id)
        slot, seconds, samples = validate_settings(
            payload,
            SESSION.config.user_slot,
            OVERRIDE_SECONDS,
            SESSION.config.teams,
            SESSION.engine.simulation_samples,
        )
        return {
            "league_profile": profile_id,
            "user_slot": slot,
            "override_seconds": seconds,
            "simulation_samples": samples,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def apply_settings(
    values: dict[str, Any],
    *,
    persist: bool = True,
    invalidate: bool = True,
) -> None:
    global OVERRIDE_SECONDS, SIMULATION_SAMPLES
    previous_profile = SESSION.config.profile_id
    previous_slot = SESSION.config.user_slot
    previous_samples = SESSION.engine.simulation_samples
    profile_id = _validate_profile(values, previous_profile)
    slot, OVERRIDE_SECONDS, SIMULATION_SAMPLES = validate_settings(
        values,
        previous_slot,
        OVERRIDE_SECONDS,
        SESSION.config.teams,
        previous_samples,
    )
    if profile_id != previous_profile:
        _replace_session(
            config=league_config_for_profile(
                profile_id,
                user_slot=slot,
                teams=SESSION.config.teams,
            )
        )
    elif slot != previous_slot:
        _replace_session(config=replace(SESSION.config, user_slot=slot))
    else:
        _configure_session(SESSION)
    if persist:
        _save_runtime_settings()
    if invalidate and (
        profile_id != previous_profile
        or slot != previous_slot
        or SIMULATION_SAMPLES != previous_samples
    ):
        ESPN_BRIDGE.invalidate("Draft settings changed; waiting for a fresh ESPN snapshot.")


class DraftRequestHandler(BaseHTTPRequestHandler):
    server_version = "DraftAgent/0.1"

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 256_000:
            raise ValueError("request body is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        request = urlparse(self.path)
        if request.path == "/api/state":
            self._json(_state_payload())
            return
        if request.path == "/api/decisions":
            try:
                limit = int(parse_qs(request.query).get("limit", ["500"])[0])
                self._json({"decisions": ESPN_BRIDGE.decisions(limit)})
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if request.path in {"/", "/index.html"}:
            body = files("draft_agent").joinpath("web/index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        global DATA_SOURCE, OVERRIDE_SECONDS, PLAYER_PREFERENCES, SESSION, SIGNAL_RECORDS, SIMULATION_SAMPLES, VEGAS_RESULT
        try:
            body = self._body()
            if self.path == "/api/pick":
                SESSION.make_user_pick(str(body["player_id"]), str(body.get("source", "manual")))
            elif self.path == "/api/weights":
                SESSION.engine.weights.update(body)
                ESPN_BRIDGE.invalidate("Strategy weights changed; waiting for a fresh ESPN snapshot.")
            elif self.path == "/api/preferences":
                PLAYER_PREFERENCES = validate_preferences(body)
                _save_preferences()
                _configure_session(SESSION)
                ESPN_BRIDGE.configure_mock_exposure(
                    int(PLAYER_PREFERENCES["mock_exposure_limit"])
                )
                ESPN_BRIDGE.invalidate("Player preferences changed; waiting for a fresh ESPN snapshot.")
            elif self.path == "/api/settings":
                apply_settings(body)
            elif self.path == "/api/data/nflverse":
                season = int(body.get("season", date.today().year - 1))
                if not 1999 <= season <= date.today().year:
                    raise ValueError("season is outside the available nflverse range")
                result = NflverseProvider().load(season, bool(body.get("refresh", False)))
                SIGNAL_RECORDS = []
                _replace_session(result.players)
                DATA_SOURCE = {
                    "name": result.source,
                    "kind": "nflverse",
                    "season": result.season,
                    "cached": result.cached,
                    "fetched_at": result.fetched_at,
                    "warning": "Historical baseline only; not a current expert projection.",
                    "mapped_espn_ids": result.mapped_espn_ids,
                }
                _sync_session_vegas()
                ESPN_BRIDGE.invalidate("Historical data changed; load current signals and sync ESPN again.")
            elif self.path == "/api/data/signals":
                signal_source = dict(_data_health()["sources"])["signals"]
                refresh = bool(body.get("refresh", False)) or not bool(
                    signal_source.get("fresh")
                )
                result = FreeSignalProvider().load(refresh)
                SIGNAL_RECORDS = result.records
                enriched, matched = apply_signals(list(SESSION.players.values()), result.records)
                _replace_session(enriched)
                DATA_SOURCE = {
                    **DATA_SOURCE,
                    "signals": result.sources,
                    "signals_cached": result.cached,
                    "signals_fetched_at": result.fetched_at,
                    "signals_matched": matched,
                    "signals_available": len(result.records),
                }
                _sync_session_vegas()
                ESPN_BRIDGE.invalidate("Current signals changed; waiting for a fresh ESPN snapshot.")
            elif self.path == "/api/data/vegas":
                VEGAS_RESULT = NflverseVegasProvider().refresh(as_of=date.today())
                _sync_session_vegas()
                if VEGAS_RESULT.status == "refreshed":
                    ESPN_BRIDGE.invalidate(
                        "Vegas team context changed; waiting for a fresh ESPN snapshot."
                    )
            elif self.path == "/api/espn/snapshot":
                _sync_session_vegas()
                ESPN_BRIDGE.ingest(
                    body,
                    list(SESSION.players.values()),
                    SESSION.engine,
                    SESSION.config,
                    SIGNAL_RECORDS,
                    _data_health(),
                    vegas_snapshot=_usable_vegas_snapshot(),
                )
            elif self.path == "/api/espn/pick-result":
                ESPN_BRIDGE.record_pick_result(body)
            elif self.path == "/api/reset":
                _replace_session()
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(_state_payload())
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[draft-agent] {format % args}")


def main() -> None:
    global PLAYER_PREFERENCES
    saved_preferences = _load_preferences()
    if saved_preferences is not None:
        PLAYER_PREFERENCES = saved_preferences
    saved_settings = _load_runtime_settings()
    if saved_settings is not None:
        apply_settings(saved_settings, persist=False, invalidate=False)
    _restore_cached_data()
    _configure_session(SESSION)
    ESPN_BRIDGE.configure_mock_exposure(
        int(PLAYER_PREFERENCES["mock_exposure_limit"])
    )
    address = ("127.0.0.1", 8765)
    print(f"Draft Agent running at http://{address[0]}:{address[1]}")
    ThreadingHTTPServer(address, DraftRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
