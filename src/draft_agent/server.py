from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
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
        global DATA_SOURCE, OVERRIDE_SECONDS, SESSION, SIGNAL_RECORDS, SIMULATION_SAMPLES
        try:
            body = self._body()
            if self.path == "/api/pick":
                SESSION.make_user_pick(str(body["player_id"]), str(body.get("source", "manual")))
            elif self.path == "/api/weights":
                SESSION.engine.weights.update(body)
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
                SESSION.engine.simulation_samples = SIMULATION_SAMPLES
            elif self.path == "/api/data/nflverse":
                season = int(body.get("season", date.today().year - 1))
                if not 1999 <= season <= date.today().year:
                    raise ValueError("season is outside the available nflverse range")
                result = NflverseProvider().load(season, bool(body.get("refresh", False)))
                SESSION = DraftSession(result.players, SESSION.config)
                SESSION.engine.simulation_samples = SIMULATION_SAMPLES
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
                SESSION.engine.simulation_samples = SIMULATION_SAMPLES
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
                SESSION.engine.simulation_samples = SIMULATION_SAMPLES
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(_state_payload())
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[draft-agent] {format % args}")


def main() -> None:
    address = ("127.0.0.1", 8765)
    print(f"Draft Agent running at http://{address[0]}:{address[1]}")
    ThreadingHTTPServer(address, DraftRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
