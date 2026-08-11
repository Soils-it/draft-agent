import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from draft_agent.config import LeagueConfig
from draft_agent.data import demo_players
from draft_agent.engine import DraftEngine
from draft_agent.espn import EspnDraftBridge
from draft_agent.signals import SignalRecord


class EspnBridgeTests(unittest.TestCase):
    def setUp(self):
        self.config = LeagueConfig(user_slot=6)
        self.engine = DraftEngine(self.config)
        self.players = [
            replace(
                player,
                external_ids={"espn": str(index)},
                signals={"consensus_rank": float(index - 999)},
            )
            for index, player in enumerate(demo_players(), 1000)
        ]
        self.bridge = EspnDraftBridge()

    def payload(self):
        catalog = [
            {
                "id": player.external_ids["espn"],
                "name": player.name,
                "team": player.team,
                "position": player.position,
                "rank": index,
                "projected_points": 350 - index,
            }
            for index, player in enumerate(self.players[:200], 1)
        ]
        return {
            "league_id": "example-league",
            "draft_id": "example-draft",
            "overall_pick": 6,
            "on_clock": True,
            "user_slot": 6,
            "player_catalog": catalog,
            "available_player_ids": [player.external_ids["espn"] for player in self.players[:200]],
            "roster_player_ids": [],
        }

    def test_shadow_snapshot_returns_mapped_recommendation(self):
        state = self.bridge.ingest(self.payload(), self.players, self.engine, self.config)
        self.assertTrue(state["connected"])
        self.assertFalse(state["can_submit"])
        self.assertEqual(state["match_rate"], 1)
        self.assertEqual(state["catalog_size"], 200)
        self.assertEqual(state["historical_enrichment_rate"], 1)
        self.assertEqual(state["roster"], [])
        self.assertEqual(len(state["recommendations"]), 5)
        self.assertIsNotNone(state["pending_espn_player_id"])
        self.assertTrue(state["mock_command_ready"])
        self.assertTrue(state["readiness"]["ready"])

    def test_rejects_duplicate_and_low_match_snapshots(self):
        duplicate = self.payload()
        duplicate["available_player_ids"] = ["1000", "1000"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.bridge.ingest(duplicate, self.players, self.engine, self.config)
        missing = self.payload()
        missing.pop("player_catalog")
        missing["available_player_ids"] = ["unknown-1", "unknown-2"]
        with self.assertRaisesRegex(ValueError, "50%"):
            self.bridge.ingest(missing, self.players, self.engine, self.config)

        poor = self.payload()
        poor["available_player_ids"].extend(
            f"unknown-{number}" for number in range(60)
        )
        poor_state = self.bridge.ingest(poor, self.players, self.engine, self.config)
        self.assertFalse(poor_state["readiness"]["ready"])
        self.assertTrue(
            any("mapped" in reason for reason in poor_state["readiness"]["reasons"])
        )
        self.assertIsNone(poor_state["pending_espn_player_id"])

    def test_off_clock_snapshot_precomputes_next_pick_without_pending_submission(self):
        payload = self.payload()
        payload["on_clock"] = False
        state = self.bridge.ingest(payload, self.players, self.engine, self.config)
        self.assertEqual(state["decision_pick"], 19)
        self.assertEqual(len(state["recommendations"]), 5)
        self.assertEqual(len(state["prequeue_espn_player_ids"]), 5)
        self.assertIsNone(state["pending_espn_player_id"])
        self.assertFalse(state["mock_command_ready"])

    def test_server_rejects_false_on_clock_signal_on_an_opponent_pick(self):
        payload = self.payload()
        payload["overall_pick"] = 7
        payload["on_clock"] = True
        roster_id = payload["available_player_ids"].pop(0)
        payload["roster_player_ids"] = [roster_id]
        state = self.bridge.ingest(payload, self.players, self.engine, self.config)
        self.assertFalse(state["on_clock"])
        self.assertEqual(state["decision_pick"], 19)
        self.assertIsNone(state["pending_espn_player_id"])
        self.assertEqual(self.bridge.decisions()[0]["decision_pick"], 19)

    def test_slot_one_pick_24_empty_roster_fails_closed_after_confirmed_pick(self):
        config = LeagueConfig(user_slot=1)
        engine = DraftEngine(config)
        first = self.payload()
        first.update({"overall_pick": 1, "user_slot": 1, "on_clock": True})
        state = self.bridge.ingest(first, self.players, engine, config)
        selected_id = state["pending_espn_player_id"]
        self.bridge.record_pick_result(
            {
                "ok": True,
                "draft_id": first["draft_id"],
                "overall_pick": 1,
                "player_id": selected_id,
                "name": state["recommendations"][0]["name"],
            }
        )

        stale = self.payload()
        stale.update({"overall_pick": 24, "user_slot": 1, "on_clock": True})
        stale_state = self.bridge.ingest(stale, self.players, engine, config)
        self.assertFalse(stale_state["readiness"]["ready"])
        self.assertEqual(stale_state["expected_roster_size"], 1)
        self.assertEqual(stale_state["observed_roster_size"], 0)
        self.assertEqual(stale_state["recommendations"], [])
        self.assertIsNone(stale_state["pending_espn_player_id"])
        self.assertFalse(stale_state["mock_command_ready"])
        self.assertEqual(self.bridge.decision_summary()["total"], 1)

        replacement_id = next(item for item in stale["available_player_ids"] if item != selected_id)
        swapped = self.payload()
        swapped.update(
            {
                "overall_pick": 24,
                "user_slot": 1,
                "on_clock": True,
                "roster_player_ids": [replacement_id],
            }
        )
        swapped["available_player_ids"].remove(replacement_id)
        swapped_state = self.bridge.ingest(swapped, self.players, engine, config)
        self.assertFalse(swapped_state["readiness"]["ready"])
        self.assertTrue(
            any("previously confirmed" in reason for reason in swapped_state["readiness"]["reasons"])
        )
        self.assertEqual(swapped_state["recommendations"], [])

    def test_stale_source_status_blocks_an_otherwise_valid_snapshot(self):
        state = self.bridge.ingest(
            self.payload(),
            self.players,
            self.engine,
            self.config,
            source_status={
                "ready": False,
                "reasons": ["Current signals are stale."],
                "sources": {"signals": {"fresh": False}},
            },
        )
        self.assertFalse(state["readiness"]["ready"])
        self.assertIn("Current signals are stale.", state["readiness"]["reasons"])
        self.assertEqual(state["recommendations"], [])

    def test_current_catalog_supports_rookies_and_negative_dst_ids(self):
        payload = self.payload()
        payload["player_catalog"].extend(
            [
                {
                    "id": "rookie-2026",
                    "name": "Example Rookie",
                    "team": "NYJ",
                    "position": "RB",
                    "rank": 1,
                    "projected_points": 400,
                },
                {
                    "id": "-16001",
                    "name": "Example D/ST",
                    "team": "BUF",
                    "position": "DST",
                    "rank": 199,
                    "projected_points": 120,
                },
            ]
        )
        payload["available_player_ids"] = ["rookie-2026", "-16001"] + payload[
            "available_player_ids"
        ]
        signals = [
            SignalRecord(
                "Example Rookie",
                "NYJ",
                "RB",
                {"espn": "rookie-2026"},
                {"consensus_rank": 8, "years_exp": 0, "depth_chart_order": 1},
                {"practice": "Full"},
            )
        ]
        state = self.bridge.ingest(
            payload, self.players, self.engine, self.config, signals
        )
        self.assertEqual(state["match_rate"], 1)
        self.assertLess(state["historical_enrichment_rate"], 1)
        self.assertGreater(state["signal_enrichment_rate"], 0)
        self.assertIn("rookie-2026", state["prequeue_espn_player_ids"])
        rookie = next(
            player
            for player in state["recommendations"]
            if player["espn_id"] == "rookie-2026"
        )
        self.assertEqual(rookie["signals"]["years_exp"], 0)
        self.assertEqual(rookie["signals"]["depth_chart_order"], 1)

    def test_rejects_duplicate_catalog_ids(self):
        payload = self.payload()
        payload["player_catalog"].append(dict(payload["player_catalog"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.bridge.ingest(payload, self.players, self.engine, self.config)

    def test_live_roster_uses_current_espn_catalog(self):
        payload = self.payload()
        roster_id = payload["available_player_ids"].pop(0)
        payload["roster_player_ids"] = [roster_id]
        state = self.bridge.ingest(payload, self.players, self.engine, self.config)
        self.assertEqual(state["mapped_roster"], 1)
        self.assertEqual(state["roster"][0]["espn_id"], roster_id)
        self.assertIn("projected_points", state["roster"][0])

    def test_mock_exposure_limit_uses_prior_mock_rosters_only(self):
        self.bridge.configure_mock_exposure(50)
        first = self.payload()
        first["is_mock"] = True
        exposed_id = first["available_player_ids"].pop(0)
        first["roster_player_ids"] = [exposed_id]
        self.bridge.ingest(first, self.players, self.engine, self.config)

        second = self.payload()
        second["is_mock"] = True
        second["draft_id"] = "second-mock"
        second["league_id"] = "second-mock"
        state = self.bridge.ingest(second, self.players, self.engine, self.config)
        self.assertEqual(state["mock_history_count"], 1)
        self.assertEqual(state["mock_exposure_limit"], 0.5)
        self.assertNotIn(exposed_id, state["prequeue_espn_player_ids"])

        real = self.payload()
        real["is_mock"] = False
        real_state = self.bridge.ingest(real, self.players, self.engine, self.config)
        self.assertFalse(real_state["is_mock"])

    def test_decision_audit_covers_all_positions_reconciles_and_persists(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            bridge = EspnDraftBridge(path)
            payload = self.payload()
            payload["is_mock"] = True
            state = bridge.ingest(payload, self.players, self.engine, self.config)

            self.assertEqual(state["decision_log"]["total"], 1)
            first = bridge.decisions()[0]
            self.assertEqual(set(first["top_by_position"]), set(bridge.positions))
            self.assertEqual(first["teams"], 12)
            self.assertEqual(len(first["top_overall"]), 5)
            self.assertIn("components", first["recommended_player"])
            self.assertIn("contributions", first["recommended_player"])
            self.assertTrue(path.exists())

            selected_id = state["pending_espn_player_id"]
            bridge.record_pick_result(
                {
                    "ok": True,
                    "league_id": payload["league_id"],
                    "draft_id": payload["draft_id"],
                    "overall_pick": payload["overall_pick"],
                    "player_id": selected_id,
                    "name": state["recommendations"][0]["name"],
                }
            )
            self.assertEqual(bridge.decisions()[0]["status"], "submitted")

            followup = self.payload()
            followup["is_mock"] = True
            followup["overall_pick"] = 7
            followup["on_clock"] = False
            followup["available_player_ids"].remove(selected_id)
            followup["roster_player_ids"] = [selected_id]
            followup_state = bridge.ingest(
                followup, self.players, self.engine, self.config
            )
            decisions = bridge.decisions()
            self.assertEqual(decisions[0]["status"], "selected")
            self.assertTrue(decisions[0]["matched_recommendation"])
            self.assertEqual(followup_state["decision_log"]["total"], 2)

            reloaded = EspnDraftBridge(path)
            self.assertEqual(reloaded.decision_summary()["total"], 2)
            self.assertEqual(reloaded.decisions()[0]["selected_player"]["espn_id"], selected_id)

    def test_decision_audit_discards_impossible_snake_turns(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(
                """[
                    {"key":"draft:6","draft_id":"draft","decision_pick":6,"user_slot":6,"teams":12},
                    {"key":"draft:7","draft_id":"draft","decision_pick":7,"user_slot":6,"teams":12}
                ]""",
                encoding="utf-8",
            )
            bridge = EspnDraftBridge(path)
            self.assertEqual(bridge.decision_summary()["total"], 1)
            self.assertEqual(bridge.decisions()[0]["decision_pick"], 6)

    def test_pick_result_requires_matching_record(self):
        with self.assertRaisesRegex(ValueError, "recorded decision"):
            self.bridge.record_pick_result(
                {
                    "ok": True,
                    "draft_id": "missing",
                    "overall_pick": 6,
                    "player_id": "1000",
                }
            )


if __name__ == "__main__":
    unittest.main()
