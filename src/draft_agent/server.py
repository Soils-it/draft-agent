from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from .data import demo_players
from .espn import EspnDraftBridge
from .providers import NflverseProvider
from .signals import FreeSignalProvider, SignalRecord, apply_signals
from .session import DraftSession


SESSION = DraftSession(demo_players())
OVERRIDE_SECONDS = 20
SIMULATION_SAMPLES = 200
DATA_SOURCE: dict[str, object] = {
    "name": "generated demo data",
    "season": None,
    "cached": False,
}
ESPN_BRIDGE = EspnDraftBridge()
SIGNAL_RECORDS: list[SignalRecord] = []
PLAYER_PREFERENCES: dict[str, object] = {
    "prefer": [],
    "fade": [],
    "never": [],
    "mock_exposure_limit": 0,
}
PREFERENCE_PATH = Path(".cache/player_preferences.json")


def _configure_session(session: DraftSession) -> None:
    session.engine.simulation_samples = SIMULATION_SAMPLES
    session.engine.set_preferences(
        list(PLAYER_PREFERENCES["prefer"]),
        list(PLAYER_PREFERENCES["fade"]),
        list(PLAYER_PREFERENCES["never"]),
    )


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
    payload["settings"] = {
        "user_slot": SESSION.config.user_slot,
        "override_seconds": OVERRIDE_SECONDS,
        "simulation_samples": SESSION.engine.simulation_samples,
    }
    payload["data_source"] = DATA_SOURCE
    payload["preferences"] = PLAYER_PREFERENCES
    payload["espn"] = ESPN_BRIDGE.state
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
        if self.path == "/api/state":
            self._json(_state_payload())
            return
        if self.path in {"/", "/index.html"}:
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
            elif self.path == "/api/preferences":
                PLAYER_PREFERENCES = validate_preferences(body)
                _save_preferences()
                _configure_session(SESSION)
                ESPN_BRIDGE.configure_mock_exposure(
                    int(PLAYER_PREFERENCES["mock_exposure_limit"])
                )
            elif self.path == "/api/settings":
                slot, OVERRIDE_SECONDS, SIMULATION_SAMPLES = validate_settings(
                    body,
                    SESSION.config.user_slot,
                    OVERRIDE_SECONDS,
                    SESSION.config.teams,
                    SESSION.engine.simulation_samples,
                )
                if slot != SESSION.config.user_slot:
                    SESSION = DraftSession(
                        demo_players(), replace(SESSION.config, user_slot=slot)
                    )
                _configure_session(SESSION)
            elif self.path == "/api/data/nflverse":
                season = int(body.get("season", date.today().year - 1))
                if not 1999 <= season <= date.today().year:
                    raise ValueError("season is outside the available nflverse range")
                result = NflverseProvider().load(season, bool(body.get("refresh", False)))
                SESSION = DraftSession(result.players, SESSION.config)
                _configure_session(SESSION)
                DATA_SOURCE = {
                    "name": result.source,
                    "season": result.season,
                    "cached": result.cached,
                    "fetched_at": result.fetched_at,
                    "warning": "Historical baseline only; not a current expert projection.",
                    "mapped_espn_ids": result.mapped_espn_ids,
                }
            elif self.path == "/api/data/signals":
                result = FreeSignalProvider().load(bool(body.get("refresh", False)))
                SIGNAL_RECORDS = result.records
                enriched, matched = apply_signals(list(SESSION.players.values()), result.records)
                SESSION = DraftSession(enriched, SESSION.config)
                _configure_session(SESSION)
                DATA_SOURCE = {
                    **DATA_SOURCE,
                    "signals": result.sources,
                    "signals_cached": result.cached,
                    "signals_fetched_at": result.fetched_at,
                    "signals_matched": matched,
                    "signals_available": len(result.records),
                }
            elif self.path == "/api/espn/snapshot":
                ESPN_BRIDGE.ingest(
                    body,
                    list(SESSION.players.values()),
                    SESSION.engine,
                    SESSION.config,
                    SIGNAL_RECORDS,
                )
            elif self.path == "/api/reset":
                SESSION = DraftSession(demo_players(), SESSION.config)
                _configure_session(SESSION)
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
    _configure_session(SESSION)
    ESPN_BRIDGE.configure_mock_exposure(
        int(PLAYER_PREFERENCES["mock_exposure_limit"])
    )
    address = ("127.0.0.1", 8765)
    print(f"Draft Agent running at http://{address[0]}:{address[1]}")
    ThreadingHTTPServer(address, DraftRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
