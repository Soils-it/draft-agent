import tempfile
import unittest
from pathlib import Path

from draft_agent.backtest import load_backtest_csv, run_backtest


HEADER = "snapshot_date,season_start,player_id,name,team,position,adp,projected_points,actual_points,consensus_rank,consensus_sd,bye_week,injury_status\n"


class BacktestTests(unittest.TestCase):
    def _file(self, content):
        temp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        temp.write(content)
        temp.close()
        self.addCleanup(Path(temp.name).unlink)
        return Path(temp.name)

    def test_rejects_post_start_snapshot_to_prevent_leakage(self):
        path = self._file(
            HEADER + "2025-09-10,2025-09-04,1,Player,AAA,WR,1,200,220,1,2,8,\n"
        )
        with self.assertRaisesRegex(ValueError, "before season_start"):
            load_backtest_csv(path)

    def test_compares_baseline_and_enhanced_rankings(self):
        rows = []
        for rank in range(1, 25):
            rows.append(
                f"2025-08-20,2025-09-04,{rank},Player {rank},TST,WR,{rank},"
                f"{300-rank},{250-rank},{rank},3,8,\n"
            )
        result = run_backtest(self._file(HEADER + "".join(rows)), top_n=6)
        snapshot = result["snapshots"]["2025-08-20"]
        self.assertEqual(snapshot["players"], 24)
        self.assertIn("actual_points_lift", snapshot)
        self.assertIn("top_player_hit_rate", snapshot["enhanced"])

    def test_requires_documented_columns(self):
        with self.assertRaisesRegex(ValueError, "missing columns"):
            load_backtest_csv(self._file("name,actual_points\nExample,1\n"))


if __name__ == "__main__":
    unittest.main()
