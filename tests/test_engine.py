import unittest

from draft_agent.config import LeagueConfig
from draft_agent.data import demo_players
from draft_agent.engine import DraftEngine
from draft_agent.session import DraftSession


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

    def test_full_mock_draft_builds_legal_roster(self):
        session = DraftSession(self.players, self.config)
        while not session.is_complete:
            session.make_user_pick(str(session.recommendations()[0]["id"]), "test")
        roster = session.user_roster()
        self.assertEqual(len(roster), self.config.roster_size)
        for position, cap in self.config.position_caps.items():
            self.assertLessEqual(sum(player.position == position for player in roster), cap)


if __name__ == "__main__":
    unittest.main()
