import copy
import json
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from draft_agent import server
from draft_agent.data import demo_players
from draft_agent.engine import DraftEngine
from draft_agent.espn import EspnDraftBridge
from draft_agent.models import Player
from draft_agent.providers import (
    CANONICAL_NFL_TEAMS,
    NFLVERSE_SCHEDULES_TIMESTAMP_URL,
    NFLVERSE_SCHEDULES_URL,
    VEGAS_ATTRIBUTION,
    VEGAS_LICENSE,
    NflverseProvider,
    NflverseVegasProvider,
    VegasProviderResult,
    VegasSnapshot,
    VegasTeamTotal,
)
from draft_agent.scoring import projected_points
from draft_agent.session import DraftSession
from draft_agent.signals import FreeSignalProvider


NFLVERSE_HEADER = (
    "player_id,player_display_name,position,recent_team,games,passing_yards,"
    "passing_tds,passing_interceptions,rushing_yards,rushing_tds,receptions,"
    "receiving_yards,receiving_tds,pat_made,fg_missed,fg_made_0_19,"
    "fg_made_20_29,fg_made_30_39,fg_made_40_49,fg_made_50_59,fg_made_60_\n"
)


def write_fake_caches(root: Path) -> tuple[NflverseProvider, FreeSignalProvider]:
    nflverse_dir = root / "nflverse"
    signal_dir = root / "signals"
    nflverse_dir.mkdir(parents=True)
    signal_dir.mkdir(parents=True)
    stats_rows = []
    player_ids = ["gsis_id,display_name,espn_id\n"]
    entries = []
    number = 0
    for position, count in {"QB": 36, "RB": 84, "WR": 100, "TE": 40, "K": 24}.items():
        for rank in range(count):
            number += 1
            player_id = f"id-{number}"
            name = f"Fake {position} {rank}"
            espn_id = str(10_000 + number)
            team = f"T{number % 32:02d}"
            stats_rows.append(
                f"{player_id},{name},{position},{team},17,{4000-rank * 10},25,8,"
                "700,6,70,900,6,35,2,2,8,8,7,4,1\n"
            )
            player_ids.append(f"{player_id},{name},{espn_id}\n")
            entries.append((player_id, name, position, team, espn_id))
    (nflverse_dir / "stats_player_reg_2025.csv").write_text(
        NFLVERSE_HEADER + "".join(stats_rows), encoding="utf-8"
    )
    (nflverse_dir / "players.csv").write_text("".join(player_ids), encoding="utf-8")

    id_rows = ["fantasypros_id,espn_id,sleeper_id,gsis_id\n"]
    consensus_rows = [
        "page_type,player,id,pos,team,ecr,sd,best,worst,rank_delta,bye,scrape_date\n"
    ]
    sleeper_players = {}
    for rank, (gsis_id, name, position, team, espn_id) in enumerate(entries[:100], 1):
        fantasypros_id = f"fp-{rank}"
        sleeper_id = f"sl-{rank}"
        id_rows.append(f"{fantasypros_id},{espn_id},{sleeper_id},{gsis_id}\n")
        consensus_rows.append(
            f"redraft-overall,{name},{fantasypros_id},{position},{team},{rank},4,1,"
            f"120,0,8,2026-08-11\n"
        )
        sleeper_players[sleeper_id] = {
            "espn_id": espn_id,
            "depth_chart_order": 1,
            "years_exp": 2,
            "status": "Active",
        }
    (signal_dir / "player_ids.csv").write_text("".join(id_rows), encoding="utf-8")
    (signal_dir / "consensus.csv").write_text(
        "".join(consensus_rows), encoding="utf-8"
    )
    (signal_dir / "sleeper_players.json").write_text(
        json.dumps(sleeper_players), encoding="utf-8"
    )
    (signal_dir / "trending_add.json").write_text("[]", encoding="utf-8")
    (signal_dir / "trending_drop.json").write_text("[]", encoding="utf-8")
    return NflverseProvider(nflverse_dir), FreeSignalProvider(signal_dir)


def write_fake_vegas_cache(root: Path) -> NflverseVegasProvider:
    provider = NflverseVegasProvider(root / "vegas")
    teams = {
        team: VegasTeamTotal(
            team,
            1,
            22 + (index % 6),
            27 - (index % 6),
        )
        for index, team in enumerate(CANONICAL_NFL_TEAMS)
    }
    now = datetime.now(timezone.utc)
    provider._write_snapshot(
        VegasSnapshot(
            teams=teams,
            source_url=NFLVERSE_SCHEDULES_URL,
            timestamp_url=NFLVERSE_SCHEDULES_TIMESTAMP_URL,
            attribution=VEGAS_ATTRIBUTION,
            license=VEGAS_LICENSE,
            fetched_at=now.isoformat(),
            dataset_timestamp=now.isoformat(),
            season=date.today().year,
            coverage=32,
            lined_games=16,
        )
    )
    return provider


class ServerDataSafetyTests(unittest.TestCase):
    def setUp(self):
        self.original_session = server.SESSION
        self.original_data_source = server.DATA_SOURCE
        self.original_signals = server.SIGNAL_RECORDS
        self.original_vegas = server.VEGAS_RESULT
        self.original_override = server.OVERRIDE_SECONDS
        self.original_samples = server.SIMULATION_SAMPLES
        self.original_bridge = server.ESPN_BRIDGE
        server.ESPN_BRIDGE = EspnDraftBridge()

    def tearDown(self):
        server.SESSION = self.original_session
        server.DATA_SOURCE = self.original_data_source
        server.SIGNAL_RECORDS = self.original_signals
        server.VEGAS_RESULT = self.original_vegas
        server.OVERRIDE_SECONDS = self.original_override
        server.SIMULATION_SAMPLES = self.original_samples
        server.ESPN_BRIDGE = self.original_bridge

    def _ready_live_vegas_state(
        self, root: Path, draft_id: str
    ) -> tuple[
        NflverseVegasProvider,
        VegasSnapshot,
        datetime,
        dict[str, object],
        str,
    ]:
        nflverse, signals = write_fake_caches(root)
        vegas_provider = write_fake_vegas_cache(root)
        with patch(
            "urllib.request.urlopen", side_effect=AssertionError("network used")
        ) as urlopen:
            self.assertTrue(
                server._restore_cached_data(nflverse, signals, vegas_provider)
            )
        urlopen.assert_not_called()

        reference_now = datetime.now(timezone.utc)
        snapshot = server.VEGAS_RESULT.snapshot
        self.assertIsNotNone(snapshot)
        historical = []
        catalog = []
        for index in range(100):
            espn_id = str(50_000 + index)
            market_rank = 1 if index < 6 else 50 + index
            projection = 250 if index < 10 else 220 - index
            if index == 0:
                team = "ARI"
            elif index < 6:
                team = "CHI"
            else:
                team = CANONICAL_NFL_TEAMS[index % 32]
            player = Player(
                f"generated-{index:03d}",
                f"Generated Back {index:03d}",
                team,
                "RB",
                market_rank,
                upside=0.5,
                risk=0.2,
                external_ids={"espn": espn_id},
                projected_points_override=projection,
                signals={"consensus_rank": market_rank, "consensus_sd": 4},
            )
            historical.append(player)
            catalog.append(
                {
                    "id": espn_id,
                    "name": player.name,
                    "team": team,
                    "position": "RB",
                    "rank": market_rank,
                    "projected_points": projection,
                }
            )
        payload = {
            "league_id": "generated-league",
            "draft_id": draft_id,
            "overall_pick": 1,
            "on_clock": True,
            "is_mock": True,
            "user_slot": 1,
            "player_catalog": catalog,
            "available_player_ids": [item["id"] for item in catalog],
            "roster_player_ids": [],
        }
        source_health = server._data_health(reference_now)
        self.assertTrue(source_health["ready"])
        self.assertTrue(source_health["sources"]["vegas"]["usable"])
        config = replace(server.SESSION.config, user_slot=1)

        neutral_bridge = EspnDraftBridge()
        neutral_state = copy.deepcopy(
            neutral_bridge.ingest(
                payload,
                historical,
                DraftEngine(config, simulation_samples=20),
                config,
                None,
                source_health,
                vegas_snapshot=None,
            )
        )
        live_bridge = EspnDraftBridge()
        live_state = live_bridge.ingest(
            payload,
            historical,
            DraftEngine(config, simulation_samples=20),
            config,
            None,
            source_health,
            vegas_snapshot=snapshot,
        )
        self.assertTrue(live_state["readiness"]["ready"])
        self.assertTrue(
            any(
                item["contributions"]["vegas_environment"] != 0
                for item in live_state["recommendations"]
            )
        )
        self.assertNotEqual(
            live_state["prequeue_espn_player_ids"],
            neutral_state["prequeue_espn_player_ids"],
        )
        self.assertNotIn(
            neutral_state["pending_espn_player_id"],
            live_state["prequeue_espn_player_ids"],
        )
        server.ESPN_BRIDGE = live_bridge
        return (
            vegas_provider,
            snapshot,
            reference_now,
            neutral_state,
            str(live_state["received_at"]),
        )

    def _assert_exact_neutral_live_state(
        self,
        espn_state: dict[str, object],
        vegas_health: dict[str, object],
        neutral_state: dict[str, object],
        received_at: str,
    ) -> None:
        self.assertTrue(espn_state["readiness"]["ready"])
        self.assertFalse(vegas_health["fresh"])
        self.assertFalse(vegas_health["usable"])
        self.assertEqual(vegas_health["status"], "stale")
        self.assertEqual(espn_state["received_at"], received_at)
        self.assertEqual(
            espn_state["recommendations"], neutral_state["recommendations"]
        )
        self.assertEqual(
            espn_state["prequeue_espn_player_ids"],
            neutral_state["prequeue_espn_player_ids"],
        )
        self.assertEqual(
            espn_state["pending_espn_player_id"],
            neutral_state["pending_espn_player_id"],
        )
        self.assertEqual(
            espn_state["mock_command_ready"], neutral_state["mock_command_ready"]
        )
        self.assertIn("fully reranked", espn_state["message"])
        for item in espn_state["recommendations"]:
            self.assertEqual(item["components"]["vegas_environment"], 0)
            self.assertEqual(item["contributions"]["vegas_environment"], 0)

        decision = server.ESPN_BRIDGE.decisions(1)[0]
        explained = [decision["recommended_player"], *decision["top_overall"]]
        explained.extend(
            item
            for item in decision["top_by_position"].values()
            if item.get("status") == "eligible"
        )
        for item in explained:
            self.assertEqual(item["components"]["vegas_environment"], 0)
            self.assertEqual(item["contributions"]["vegas_environment"], 0)

    def test_valid_caches_restore_offline_and_settings_preserve_loaded_players(self):
        with TemporaryDirectory() as directory:
            nflverse, signals = write_fake_caches(Path(directory))
            vegas = write_fake_vegas_cache(Path(directory))
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                self.assertTrue(server._restore_cached_data(nflverse, signals, vegas))

            before = {
                player.player_id: (dict(player.external_ids), dict(player.signals))
                for player in server.SESSION.players.values()
            }
            source_before = copy.deepcopy(server.DATA_SOURCE)
            server.apply_settings(
                {"user_slot": 1, "override_seconds": 5, "simulation_samples": 50}
            )
            after = {
                player.player_id: (dict(player.external_ids), dict(player.signals))
                for player in server.SESSION.players.values()
            }
            self.assertEqual(after, before)
            self.assertEqual(server.DATA_SOURCE, source_before)
            self.assertTrue(server._data_health()["ready"])

            players = list(server.SESSION.players.values())[:100]
            catalog = [
                {
                    "id": player.external_ids["espn"],
                    "name": player.name,
                    "team": player.team,
                    "position": player.position,
                    "rank": rank,
                    "projected_points": projected_points(player),
                }
                for rank, player in enumerate(players, 1)
            ]
            payload = {
                "league_id": "fake-league",
                "draft_id": "fake-draft",
                "overall_pick": 1,
                "on_clock": True,
                "is_mock": True,
                "user_slot": 1,
                "player_catalog": catalog,
                "available_player_ids": [item["id"] for item in catalog],
                "roster_player_ids": [],
            }
            bridge = EspnDraftBridge()
            state = bridge.ingest(
                payload,
                list(server.SESSION.players.values()),
                DraftEngine(server.SESSION.config, simulation_samples=20),
                server.SESSION.config,
                server.SIGNAL_RECORDS,
                server._data_health(),
            )
            self.assertEqual(state["historical_enrichment_rate"], 1)
            self.assertEqual(state["signal_enrichment_rate"], 1)
            self.assertTrue(state["readiness"]["ready"])
            state_payload = server._state_payload()
            self.assertIn("historical", state_payload["readiness"]["sources"])
            self.assertIn("signals", state_payload["readiness"]["sources"])
            self.assertIn("vegas", state_payload["readiness"]["sources"])
            vegas_health = state_payload["readiness"]["sources"]["vegas"]
            self.assertTrue(vegas_health["fresh"])
            self.assertEqual(vegas_health["coverage"], 32)
            self.assertTrue(vegas_health["optional"])

            server.DATA_SOURCE["signals_fetched_at"] = "2000-01-01T00:00:00+00:00"
            stale_health = server._data_health()
            self.assertFalse(stale_health["ready"])
            self.assertTrue(
                any("older than" in reason for reason in stale_health["reasons"])
            )

    def test_absent_or_invalid_cache_never_claims_full_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                self.assertFalse(
                    server._restore_cached_data(
                        NflverseProvider(root / "missing-nflverse"),
                        FreeSignalProvider(root / "missing-signals"),
                        NflverseVegasProvider(root / "missing-vegas"),
                    )
                )
            self.assertEqual(server.DATA_SOURCE["kind"], "demo")
            self.assertFalse(server._data_health()["ready"])

            server.DATA_SOURCE = {
                "name": "nflverse historical baseline",
                "kind": "nflverse",
                "season": 2025,
                "cached": True,
            }
            false_claim = server._data_health()
            self.assertFalse(false_claim["ready"])
            self.assertTrue(
                any("does not match" in reason for reason in false_claim["reasons"])
            )

            nflverse, signals = write_fake_caches(root / "valid")
            vegas = write_fake_vegas_cache(root / "valid")
            (signals.cache_dir / "sleeper_players.json").write_text(
                "not valid json", encoding="utf-8"
            )
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                self.assertFalse(server._restore_cached_data(nflverse, signals, vegas))
            self.assertEqual(server.DATA_SOURCE["kind"], "nflverse")
            self.assertNotIn("signals", server.DATA_SOURCE)
            health = server._data_health()
            self.assertFalse(health["ready"])
            self.assertTrue(any("signals" in reason.lower() for reason in health["reasons"]))

    def test_startup_rejects_malformed_trends_and_keeps_not_ready_state(self):
        cases = (
            ("trending_add.json", [1]),
            ("trending_drop.json", [None]),
        )
        for filename, payload in cases:
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                nflverse, signals = write_fake_caches(Path(directory))
                vegas = write_fake_vegas_cache(Path(directory))
                (signals.cache_dir / filename).write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                restore_results = []
                original_restore = server._restore_cached_data

                def track_restore() -> bool:
                    result = original_restore()
                    restore_results.append(result)
                    return result

                http_server = Mock()
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(server, "NflverseProvider", return_value=nflverse)
                    )
                    stack.enter_context(
                        patch.object(server, "FreeSignalProvider", return_value=signals)
                    )
                    stack.enter_context(
                        patch.object(server, "NflverseVegasProvider", return_value=vegas)
                    )
                    stack.enter_context(
                        patch.object(
                            server, "_restore_cached_data", side_effect=track_restore
                        )
                    )
                    stack.enter_context(
                        patch.object(server, "_load_preferences", return_value=None)
                    )
                    stack.enter_context(
                        patch.object(server, "_load_runtime_settings", return_value=None)
                    )
                    stack.enter_context(
                        patch.object(
                            server, "ThreadingHTTPServer", return_value=http_server
                        )
                    )
                    stack.enter_context(patch("builtins.print"))
                    urlopen = stack.enter_context(
                        patch(
                            "urllib.request.urlopen",
                            side_effect=AssertionError("network used"),
                        )
                    )
                    server.main()

                self.assertEqual(restore_results, [False])
                urlopen.assert_not_called()
                http_server.serve_forever.assert_called_once_with()
                self.assertEqual(server.DATA_SOURCE["kind"], "nflverse")
                self.assertIn("invalid", server.DATA_SOURCE["restore_warning"].lower())

                state = server._state_payload()
                self.assertFalse(state["readiness"]["ready"])
                self.assertTrue(
                    any(
                        "load current" in reason.lower()
                        and "signals" in reason.lower()
                        for reason in state["readiness"]["reasons"]
                    )
                )
                self.assertEqual(
                    state["data_source"]["restore_warning"],
                    server.DATA_SOURCE["restore_warning"],
                )

    def test_invalid_optional_vegas_cache_does_not_block_required_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nflverse, signals = write_fake_caches(root)
            vegas = write_fake_vegas_cache(root)
            vegas.cache_path.write_text("not json", encoding="utf-8")

            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                self.assertTrue(server._restore_cached_data(nflverse, signals, vegas))
            health = server._data_health()
            self.assertTrue(health["ready"])
            self.assertEqual(health["sources"]["vegas"]["status"], "invalid")
            self.assertFalse(health["sources"]["vegas"]["fresh"])

    def test_stale_vegas_stays_in_health_but_is_removed_from_players(self):
        with TemporaryDirectory() as directory:
            provider = write_fake_vegas_cache(Path(directory))
            fresh = provider.load_cached()
            fetched = datetime.fromisoformat(fresh.snapshot.fetched_at)
            stale_now = fetched.replace(tzinfo=timezone.utc) + timedelta(hours=49)
            server.VEGAS_RESULT = VegasProviderResult(
                fresh.snapshot,
                "stale",
                True,
            )
            server.SESSION = DraftSession(
                [
                    Player(
                        "ari",
                        "Arizona Player",
                        "ARI",
                        "RB",
                        1,
                        signals={
                            "vegas_implied_points": 30,
                            "vegas_opponent_implied_points": 20,
                            "vegas_games": 1,
                            "vegas_league_implied_points": 24,
                            "vegas_league_opponent_implied_points": 24,
                        },
                    )
                ]
            )

            health = server._data_health(stale_now)
            self.assertTrue(health["sources"]["vegas"]["loaded"])
            self.assertFalse(health["sources"]["vegas"]["fresh"])
            self.assertEqual(health["sources"]["vegas"]["coverage"], 32)
            self.assertNotIn(
                "vegas_implied_points", server.SESSION.players["ari"].signals
            )

    def test_natural_vegas_expiry_fully_reranks_retained_live_espn_state(self):
        with TemporaryDirectory() as directory:
            _, snapshot, now, neutral, received_at = self._ready_live_vegas_state(
                Path(directory), "natural-expiry"
            )
            server.VEGAS_RESULT = VegasProviderResult(
                replace(snapshot, fetched_at=(now - timedelta(hours=49)).isoformat()),
                "cached",
                True,
            )

            with patch(
                "urllib.request.urlopen", side_effect=AssertionError("network used")
            ) as urlopen:
                health = server._data_health(now)
                self.assertEqual(
                    server.ESPN_BRIDGE.state["recommendations"],
                    neutral["recommendations"],
                )
                espn_state = server._espn_state_payload(health)
            urlopen.assert_not_called()

            self._assert_exact_neutral_live_state(
                espn_state,
                health["sources"]["vegas"],
                neutral,
                received_at,
            )

    def test_failed_refresh_stale_fallback_fully_reranks_live_espn_state(self):
        with TemporaryDirectory() as directory:
            provider, snapshot, now, neutral, received_at = self._ready_live_vegas_state(
                Path(directory), "failed-refresh"
            )
            provider._write_snapshot(
                replace(snapshot, fetched_at=(now - timedelta(hours=49)).isoformat())
            )
            handler = object.__new__(server.DraftRequestHandler)
            handler.path = "/api/data/vegas"
            handler._body = lambda: {"refresh": True}
            responses = []
            handler._json = lambda payload, status=None: responses.append(
                (payload, status)
            )

            with patch.object(
                server, "NflverseVegasProvider", return_value=provider
            ), patch(
                "urllib.request.urlopen",
                side_effect=OSError("outbound network denied by test"),
            ) as urlopen:
                handler.do_POST()

            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(len(responses), 1)
            payload = responses[0][0]
            vegas_health = payload["data_source"]["freshness"]["vegas"]
            self.assertIn("refresh failed", vegas_health["error"].lower())
            self._assert_exact_neutral_live_state(
                payload["espn"], vegas_health, neutral, received_at
            )

    def test_vegas_refresh_endpoint_state_and_dashboard_are_explicit(self):
        with TemporaryDirectory() as directory:
            provider = write_fake_vegas_cache(Path(directory))
            cached = provider.load_cached()
            result = VegasProviderResult(cached.snapshot, "refreshed", False)
            handler = object.__new__(server.DraftRequestHandler)
            handler.path = "/api/data/vegas"
            handler._body = lambda: {"refresh": True}
            responses = []
            handler._json = lambda payload, status=None: responses.append((payload, status))

            provider_mock = Mock()
            provider_mock.refresh.return_value = result
            with patch.object(server, "NflverseVegasProvider", return_value=provider_mock):
                handler.do_POST()

            self.assertEqual(len(responses), 1)
            vegas = responses[0][0]["data_source"]["freshness"]["vegas"]
            self.assertEqual(vegas["status"], "refreshed")
            self.assertEqual(vegas["coverage"], 32)
            self.assertEqual(vegas["license"], "CC BY 4.0")
            provider_mock.refresh.assert_called_once()

            html = files("draft_agent").joinpath("web/index.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("/api/data/vegas", html)
            self.assertIn("team-level Vegas", html)
            self.assertIn("not player props", html)
            self.assertIn("CC BY 4.0", html)
            self.assertIn("vegas_environment", html)

    def test_weights_api_cannot_raise_vegas_contribution_above_cap(self):
        server.SESSION = DraftSession(demo_players())
        handler = object.__new__(server.DraftRequestHandler)
        handler.path = "/api/weights"
        handler._body = lambda: {"vegas_environment": 1}
        responses = []
        handler._json = lambda payload, status=None: responses.append((payload, status))

        handler.do_POST()

        self.assertEqual(server.SESSION.engine.weights.vegas_environment, 0.03)
        self.assertEqual(responses[0][0]["weights"]["vegas_environment"], 0.03)


if __name__ == "__main__":
    unittest.main()
