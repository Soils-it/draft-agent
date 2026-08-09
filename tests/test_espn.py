import unittest
from dataclasses import replace

from draft_agent.config import LeagueConfig
from draft_agent.data import demo_players
from draft_agent.engine import DraftEngine
from draft_agent.espn import EspnDraftBridge


class EspnBridgeTests(unittest.TestCase):
    def setUp(self):
        self.config = LeagueConfig(user_slot=6)
        self.engine = DraftEngine(self.config)
        self.players = [
            replace(player, external_ids={"espn": str(index)})
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
        self.assertEqual(len(state["recommendations"]), 5)
        self.assertIsNotNone(state["pending_espn_player_id"])

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

    def test_off_clock_snapshot_precomputes_next_pick_without_pending_submission(self):
        payload = self.payload()
        payload["on_clock"] = False
        state = self.bridge.ingest(payload, self.players, self.engine, self.config)
        self.assertEqual(state["decision_pick"], 19)
        self.assertEqual(len(state["recommendations"]), 5)
        self.assertEqual(len(state["prequeue_espn_player_ids"]), 5)
        self.assertIsNone(state["pending_espn_player_id"])

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
        state = self.bridge.ingest(payload, self.players, self.engine, self.config)
        self.assertEqual(state["match_rate"], 1)
        self.assertLess(state["historical_enrichment_rate"], 1)
        self.assertIn("rookie-2026", state["prequeue_espn_player_ids"])

    def test_rejects_duplicate_catalog_ids(self):
        payload = self.payload()
        payload["player_catalog"].append(dict(payload["player_catalog"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.bridge.ingest(payload, self.players, self.engine, self.config)


if __name__ == "__main__":
    unittest.main()
