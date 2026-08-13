import copy
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from draft_agent import server
from draft_agent.engine import DraftEngine
from draft_agent.espn import EspnDraftBridge
from draft_agent.providers import NflverseProvider
from draft_agent.scoring import projected_points
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


class ServerDataSafetyTests(unittest.TestCase):
    def setUp(self):
        self.original_session = server.SESSION
        self.original_data_source = server.DATA_SOURCE
        self.original_signals = server.SIGNAL_RECORDS
        self.original_override = server.OVERRIDE_SECONDS
        self.original_samples = server.SIMULATION_SAMPLES
        self.original_bridge_state = copy.deepcopy(server.ESPN_BRIDGE.state)

    def tearDown(self):
        server.SESSION = self.original_session
        server.DATA_SOURCE = self.original_data_source
        server.SIGNAL_RECORDS = self.original_signals
        server.OVERRIDE_SECONDS = self.original_override
        server.SIMULATION_SAMPLES = self.original_samples
        server.ESPN_BRIDGE.state = self.original_bridge_state

    def test_valid_caches_restore_offline_and_settings_preserve_loaded_players(self):
        with TemporaryDirectory() as directory:
            nflverse, signals = write_fake_caches(Path(directory))
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                self.assertTrue(server._restore_cached_data(nflverse, signals))

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
            (signals.cache_dir / "sleeper_players.json").write_text(
                "not valid json", encoding="utf-8"
            )
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                self.assertFalse(server._restore_cached_data(nflverse, signals))
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


if __name__ == "__main__":
    unittest.main()
