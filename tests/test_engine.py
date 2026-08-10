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

    def test_one_qb_strategy_blocks_early_qbs_and_early_backup(self):
        elite_qb = Player("elite-qb", "Elite QB", "BUF", "QB", 5, projected_points_override=400)
        rb = Player("rb", "Starting RB", "DET", "RB", 20, projected_points_override=270)
        engine = DraftEngine(self.config, simulation_samples=50)
        self.assertEqual(engine.rank([elite_qb, rb], [], 19, 30, 1)[0]["id"], "rb")
        self.assertEqual(engine.rank([elite_qb, rb], [], 30, 43, 1)[0]["id"], "rb")
        roster = [Player("my-qb", "My QB", "KC", "QB", 1, projected_points_override=390)]
        ranked_ids = {item["id"] for item in engine.rank([elite_qb, rb], roster, 43, 54)}
        self.assertNotIn("elite-qb", ranked_ids)

    def test_backup_qb_requires_twenty_pick_market_discount(self):
        roster = [
            Player("my-qb", "My QB", "AAA", "QB", 80),
            Player("rb1", "RB One", "BBB", "RB", 20),
            Player("rb2", "RB Two", "CCC", "RB", 30),
            Player("wr1", "WR One", "DDD", "WR", 15),
            Player("wr2", "WR Two", "EEE", "WR", 25),
            Player("te1", "TE One", "FFF", "TE", 90),
        ]
        candidates = [
            Player("value-qb", "Value QB", "GGG", "QB", 120, signals={"consensus_rank": 120}),
            Player("fair-qb", "Fair QB", "HHH", "QB", 130, signals={"consensus_rank": 130}),
            Player("wr3", "WR Three", "III", "WR", 140, signals={"consensus_rank": 140}),
        ]
        ranked_ids = {
            item["id"]
            for item in DraftEngine(self.config, simulation_samples=50).rank(
                candidates, roster, 145, 156
            )
        }
        self.assertIn("value-qb", ranked_ids)
        self.assertNotIn("fair-qb", ranked_ids)

    def test_market_guardrail_blocks_large_reach_and_has_safe_fallback(self):
        engine = DraftEngine(self.config, simulation_samples=50)
        sensible = Player(
            "sensible",
            "Sensible WR",
            "AAA",
            "WR",
            75,
            projected_points_override=180,
            signals={"consensus_rank": 75},
        )
        reach = Player(
            "reach",
            "Reach WR",
            "BBB",
            "WR",
            100,
            projected_points_override=300,
            signals={"consensus_rank": 100},
        )
        ranked = engine.rank([sensible, reach], [], 70, 79)
        self.assertEqual([item["id"] for item in ranked], ["sensible"])
        self.assertEqual(ranked[0]["market_reach_limit"], 12)
        fallback = engine.rank([reach], [], 70, 79)
        self.assertEqual(fallback[0]["id"], "reach")
        self.assertEqual(fallback[0]["market_reach"], 30)

    def test_te_tier_urgency_applies_in_rounds_eight_through_ten(self):
        engine = DraftEngine(self.config, simulation_samples=50)
        engine.weights.update({key: 0 for key in engine.weights.__dict__})
        engine.weights.update({"te_urgency": 1})
        candidates = [
            Player("te", "Tier TE", "AAA", "TE", 90, projected_points_override=180),
            Player("wr", "Bench WR", "BBB", "WR", 90, projected_points_override=180),
        ]
        roster = [
            Player("rb1", "RB One", "CCC", "RB", 10),
            Player("rb2", "RB Two", "DDD", "RB", 20),
            Player("wr1", "WR One", "EEE", "WR", 15),
            Player("wr2", "WR Two", "FFF", "WR", 25),
        ]
        ranked = engine.rank(candidates, roster, 97, 108)
        self.assertEqual(ranked[0]["id"], "te")
        self.assertGreater(ranked[0]["components"]["te_urgency"], 0)

    def test_rookie_camp_role_rewards_first_team_depth(self):
        engine = DraftEngine(self.config, simulation_samples=50)
        engine.weights.update({key: 0 for key in engine.weights.__dict__})
        engine.weights.update({"rookie_camp_role": 1})
        candidates = [
            Player(
                "first-team",
                "First Team Rookie",
                "AAA",
                "WR",
                70,
                projected_points_override=180,
                signals={"years_exp": 0, "depth_chart_order": 1},
                context={"practice": "Full"},
            ),
            Player(
                "third-team",
                "Third Team Rookie",
                "BBB",
                "WR",
                70,
                projected_points_override=180,
                signals={"years_exp": 0, "depth_chart_order": 3},
                context={"practice": "Limited"},
            ),
        ]
        ranked = engine.rank(candidates, [], 70, 79)
        self.assertEqual(ranked[0]["id"], "first-team")
        self.assertGreater(
            ranked[0]["components"]["rookie_camp_role"],
            ranked[1]["components"]["rookie_camp_role"],
        )

    def test_roster_deadlines_force_second_rb_and_late_specialists(self):
        engine = DraftEngine(self.config, simulation_samples=50)
        roster = [
            Player("rb1", "RB One", "AAA", "RB", 1),
            Player("wr1", "WR One", "BBB", "WR", 2),
            Player("wr2", "WR Two", "CCC", "WR", 3),
        ]
        candidates = [
            Player("rb2", "RB Two", "DDD", "RB", 60, projected_points_override=190),
            Player("te", "Elite TE", "EEE", "TE", 1, projected_points_override=300),
            Player("k", "Kicker", "FFF", "K", 1, projected_points_override=300),
            Player("dst", "Defense", "GGG", "DST", 1, projected_points_override=300),
        ]
        ranked = engine.rank(candidates, roster, 67, 78)
        self.assertEqual([item["id"] for item in ranked], ["rb2"])

    def test_round_four_requires_missing_wr_and_rb_depth_stays_balanced(self):
        engine = DraftEngine(self.config, simulation_samples=50)
        roster = [
            Player("rb1", "RB One", "AAA", "RB", 1),
            Player("rb2", "RB Two", "BBB", "RB", 2),
            Player("te", "TE One", "CCC", "TE", 3),
        ]
        candidates = [
            Player("rb3", "RB Three", "DDD", "RB", 1, projected_points_override=300),
            Player("wr1", "WR One", "EEE", "WR", 50, projected_points_override=180),
        ]
        self.assertEqual(engine.rank(candidates, roster, 43, 54, 1)[0]["id"], "wr1")
        balanced_late = roster + [
            Player("wr1", "WR One", "EEE", "WR", 4),
            Player("rb3", "RB Three", "DDD", "RB", 5),
            Player("rb4", "RB Four", "FFF", "RB", 6),
        ]
        self.assertEqual(
            engine.rank(
                [
                    Player("rb5", "RB Five", "GGG", "RB", 1, projected_points_override=300),
                    Player("wr2", "WR Two", "HHH", "WR", 80, projected_points_override=150),
                ],
                balanced_late,
                91,
                102,
                1,
            )[0]["id"],
            "wr2",
        )

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
