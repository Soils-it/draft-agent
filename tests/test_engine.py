import unittest

from draft_agent.config import LeagueConfig
from draft_agent.data import demo_players
from draft_agent.engine import DraftEngine
from draft_agent.models import Player
from draft_agent.session import DraftSession
from draft_agent.server import validate_settings


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.players = demo_players()
        self.config = LeagueConfig(user_slot=6)

    def test_snake_order(self):
        session = DraftSession(self.players, self.config)
        self.assertEqual(session.team_for_pick(1), 1)
        self.assertEqual(session.team_for_pick(12), 12)
        self.assertEqual(session.team_for_pick(13), 12)
        self.assertEqual(session.team_for_pick(24), 1)

    def test_session_advances_to_user_pick(self):
        session = DraftSession(self.players, self.config)
        self.assertEqual(session.current_pick, 6)
        self.assertTrue(session.as_dict()["on_clock"])
        self.assertEqual(len(session.recommendations()), 5)
        self.assertEqual(session.as_dict(include_recommendations=False)["recommendations"], [])

    def test_user_pick_is_unique_and_advances_round(self):
        session = DraftSession(self.players, self.config)
        choice = session.recommendations()[0]["id"]
        session.make_user_pick(str(choice), "test")
        self.assertEqual(session.current_pick, 19)
        self.assertEqual(len(session.user_roster()), 1)
        self.assertNotIn(choice, {player.player_id for player in session.available()})

    def test_weights_change_score_transparently(self):
        engine = DraftEngine(self.config)
        first = engine.rank(self.players, [], 6, 19, 1)[0]
        engine.weights.update({"projection": 0, "vor": 0, "scarcity": 0, "roster_need": 0, "gone_next_pick": 0, "upside": 1, "risk": 0})
        second = engine.rank(self.players, [], 6, 19, 1)[0]
        self.assertNotEqual(first["draft_score"], second["draft_score"])
        self.assertEqual(second["components"]["upside"], 0.87)

    def test_consensus_can_break_equal_projection_tie(self):
        players = [
            Player("late", "Late Market", "AAA", "WR", 20, projected_points_override=200, signals={"consensus_rank": 40}),
            Player("early", "Early Market", "BBB", "WR", 20, projected_points_override=200, signals={"consensus_rank": 5}),
        ]
        engine = DraftEngine(self.config)
        engine.weights.update({key: 0 for key in engine.weights.__dict__})
        engine.weights.update({"consensus": 1})
        self.assertEqual(engine.rank(players, [], 6, 19, 1)[0]["id"], "early")

    def test_injury_and_bye_overlap_reduce_components(self):
        healthy = Player("healthy", "Healthy", "AAA", "WR", 10, projected_points_override=200, signals={"bye_week": 8})
        hurt = Player("hurt", "Hurt", "BBB", "WR", 10, projected_points_override=200, signals={"bye_week": 7}, context={"injury_status": "Out"})
        roster = [Player("roster", "Roster", "CCC", "WR", 1, signals={"bye_week": 8})]
        ranked = {item["id"]: item for item in DraftEngine(self.config).rank([healthy, hurt], roster, 20, 30)}
        self.assertEqual(ranked["hurt"]["components"]["availability"], 0.12)
        self.assertEqual(ranked["healthy"]["components"]["bye_fit"], 0.5)
        self.assertGreater(ranked["healthy"]["components"]["availability"], ranked["hurt"]["components"]["availability"])

    def test_full_mock_draft_builds_legal_roster(self):
        session = DraftSession(self.players, self.config)
        while not session.is_complete:
            session.make_user_pick(str(session.recommendations()[0]["id"]), "test")
        roster = session.user_roster()
        self.assertEqual(len(roster), self.config.roster_size)
        for position, cap in self.config.position_caps.items():
            self.assertLessEqual(sum(player.position == position for player in roster), cap)

    def test_runtime_settings_validation(self):
        self.assertEqual(validate_settings({"user_slot": 12, "override_seconds": 30}, 6, 20, 12), (12, 30, 200))
        with self.assertRaises(ValueError):
            validate_settings({"user_slot": 0}, 6, 20, 12)
        with self.assertRaises(ValueError):
            validate_settings({"override_seconds": 3}, 6, 20, 12)
        with self.assertRaises(ValueError):
            validate_settings({"simulation_samples": 20}, 6, 20, 12)

    def test_simulation_is_deterministic_and_exposed(self):
        engine = DraftEngine(self.config, simulation_samples=50)
        first = engine.rank(self.players, [], 6, 19, 5)
        second = engine.rank(self.players, [], 6, 19, 5)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["simulation_samples"], 50)
        self.assertIsNotNone(first[0]["survival_probability"])
        self.assertIn("simulation", first[0]["components"])


if __name__ == "__main__":
    unittest.main()
