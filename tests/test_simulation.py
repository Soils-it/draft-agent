import unittest

from draft_agent.models import Player
from draft_agent.simulation import simulate_turn_value


class SimulationTests(unittest.TestCase):
    def test_early_market_player_is_less_likely_to_survive(self):
        players = [
            Player(str(rank), f"Player {rank}", "TST", "WR", rank, projected_points_override=300 - rank)
            for rank in range(1, 41)
        ]
        points = {player.player_id: player.projected_points_override for player in players}
        result = simulate_turn_value(players, points, 1, 12, samples=400)
        self.assertLess(result["1"].survival_probability, result["20"].survival_probability)

    def test_empty_and_disabled_simulation_are_safe(self):
        self.assertEqual(simulate_turn_value([], {}, 1, 12), {})


if __name__ == "__main__":
    unittest.main()
