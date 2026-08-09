import unittest

from draft_agent.providers import players_from_nflverse_csv


HEADER = "player_id,player_display_name,position,recent_team,games,passing_yards,passing_tds,passing_interceptions,rushing_yards,rushing_tds,receptions,receiving_yards,receiving_tds,pat_made,fg_missed,fg_made_0_19,fg_made_20_29,fg_made_30_39,fg_made_40_49,fg_made_50_59,fg_made_60_\n"


class ProviderTests(unittest.TestCase):
    def test_rejects_incomplete_feed(self):
        row = "id-1,Example Player,WR,ABC,17,0,0,0,0,0,80,1000,8,0,0,0,0,0,0,0,0\n"
        with self.assertRaisesRegex(ValueError, "enough draftable players"):
            players_from_nflverse_csv(HEADER + row, 2025)

    def test_parses_complete_fake_feed(self):
        rows = []
        positions = {"QB": 36, "RB": 84, "WR": 100, "TE": 40, "K": 24}
        number = 0
        for position, count in positions.items():
            for rank in range(count):
                number += 1
                rows.append(
                    f"id-{number},Fake {position} {rank},{position},TST,17,"
                    f"{4000-rank * 10},25,8,700,6,70,900,6,35,2,2,8,8,7,4,1\n"
                )
        players = players_from_nflverse_csv(HEADER + "".join(rows), 2025)
        self.assertGreaterEqual(len(players), 150)
        receiver = next(player for player in players if player.position == "WR")
        self.assertEqual(receiver.status, "2025 BASELINE")
        self.assertTrue(receiver.player_id.startswith("nflverse-"))
        self.assertEqual(sum(player.position == "DST" for player in players), 32)


if __name__ == "__main__":
    unittest.main()
