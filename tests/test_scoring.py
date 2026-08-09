import unittest

from draft_agent.models import Player
from draft_agent.scoring import projected_points


class ScoringTests(unittest.TestCase):
    def test_full_ppr_offense(self):
        player = Player(
            "p1", "Test Player", "T01", "WR", 1,
            {"receptions": 10, "receiving_yards": 100, "receiving_tds": 2},
        )
        self.assertEqual(projected_points(player), 32)

    def test_qb_scoring_and_interception_penalty(self):
        player = Player(
            "p2", "Test QB", "T02", "QB", 1,
            {"passing_yards": 300, "passing_tds": 2, "interceptions": 1},
        )
        self.assertEqual(projected_points(player), 18)

    def test_kicker_distance_scoring(self):
        player = Player(
            "p3", "Test K", "T03", "K", 1,
            {"pat_made": 2, "fg_0_39": 1, "fg_40_49": 1, "fg_50_59": 1, "fg_60_plus": 1, "fg_missed": 1},
        )
        self.assertEqual(projected_points(player), 19)

    def test_dst_points_and_yards_ranges(self):
        player = Player(
            "p4", "Test DST", "T04", "DST", 1,
            {"games": 1, "sacks": 3, "defensive_interceptions": 2, "points_allowed_per_game": 10, "yards_allowed_per_game": 250},
        )
        self.assertEqual(projected_points(player), 12)

    def test_dst_fractional_projection_uses_whole_number_bucket(self):
        player = Player(
            "p5", "Projected DST", "T05", "DST", 1,
            {"games": 1, "points_allowed_per_game": 27.12, "yards_allowed_per_game": 349.4},
        )
        self.assertEqual(projected_points(player), 0)


if __name__ == "__main__":
    unittest.main()
