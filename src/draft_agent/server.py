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

from .config import LeagueConfig
from .data import demo_players
from .espn import EspnDraftBridge
from .models import Player
from .providers import NflverseProvider
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
PLAYER_PREFERENCES: dict[str, object] = {
    "prefer": [],
    "fade": [],
    "never": [],
    "mock_exposure_limit": 0,
}
PREFERENCE_PATH = Path(".cache/player_preferences.json")
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


def _data_health(now: datetime | None = None) -> dict[str, object]:
    players = list(SESSION.players.values())
    historical_players = sum(
        player.player_id.startswith("nflverse-") for player in players
    )
    signaled_players = sum(bool(player.signals) for player in players)
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
        },
    }


def _espn_state_payload(source_health: dict[str, object]) -> dict[str, object]:
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
) -> bool:
    """Restore complete caches at startup without falling through to network."""
    global DATA_SOURCE, SIGNAL_RECORDS
    nflverse_provider = nflverse_provider or NflverseProvider()
    signal_provider = signal_provider or FreeSignalProvider()
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
        return False
    SIGNAL_RECORDS = signals.records
    enriched, matched = apply_signals(list(SESSION.players.values()), signals.records)
    _replace_session(enriched)
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
    payload = SESSION.as_dict(include_recommendations=not bool(ESPN_BRIDGE.state.get("connected")))
    source_health = _data_health()
    espn_state = _espn_state_payload(source_health)
    payload["settings"] = {
        "user_slot": SESSION.config.user_slot,
        "override_seconds": OVERRIDE_SECONDS,
        "simulation_samples": SESSION.engine.simulation_samples,
    }
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


def apply_settings(values: dict[str, Any]) -> None:
    global OVERRIDE_SECONDS, SIMULATION_SAMPLES
    previous_slot = SESSION.config.user_slot
    previous_samples = SESSION.engine.simulation_samples
    slot, OVERRIDE_SECONDS, SIMULATION_SAMPLES = validate_settings(
        values,
        previous_slot,
        OVERRIDE_SECONDS,
        SESSION.config.teams,
        previous_samples,
    )
    if slot != previous_slot:
        _replace_session(config=replace(SESSION.config, user_slot=slot))
    else:
        _configure_session(SESSION)
    if slot != previous_slot or SIMULATION_SAMPLES != previous_samples:
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
        global DATA_SOURCE, OVERRIDE_SECONDS, PLAYER_PREFERENCES, SESSION, SIGNAL_RECORDS, SIMULATION_SAMPLES
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
                ESPN_BRIDGE.invalidate("Current signals changed; waiting for a fresh ESPN snapshot.")
            elif self.path == "/api/espn/snapshot":
                ESPN_BRIDGE.ingest(
                    body,
                    list(SESSION.players.values()),
                    SESSION.engine,
                    SESSION.config,
                    SIGNAL_RECORDS,
                    _data_health(),
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
