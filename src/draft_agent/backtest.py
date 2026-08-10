from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import LeagueConfig
from .engine import DraftEngine
from .models import Player


REQUIRED_COLUMNS = {
    "snapshot_date",
    "season_start",
    "player_id",
    "name",
    "team",
    "position",
    "adp",
    "projected_points",
    "actual_points",
}


@dataclass(frozen=True)
class BacktestPlayer:
    player: Player
    actual_points: float


def load_backtest_csv(path: Path) -> dict[str, list[BacktestPlayer]]:
    grouped: dict[str, list[BacktestPlayer]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"backtest CSV is missing columns: {', '.join(sorted(missing))}")
        for line, row in enumerate(reader, 2):
            snapshot = date.fromisoformat(row["snapshot_date"])
            season_start = date.fromisoformat(row["season_start"])
            if snapshot >= season_start:
                raise ValueError(f"line {line} snapshot_date must be before season_start")
            signals = {}
            for name in ("consensus_rank", "consensus_sd", "bye_week"):
                if row.get(name):
                    signals[name] = float(row[name])
            player = Player(
                player_id=row["player_id"],
                name=row["name"],
                team=row["team"].upper(),
                position=row["position"].upper().replace("D/ST", "DST"),
                adp=float(row["adp"]),
                projected_points_override=float(row["projected_points"]),
                signals=signals,
                context={"injury_status": row.get("injury_status", "")},
            )
            grouped[row["snapshot_date"]].append(
                BacktestPlayer(player, float(row["actual_points"]))
            )
    if not grouped:
        raise ValueError("backtest CSV contains no rows")
    return dict(grouped)


def _metrics(selected: list[str], actual: dict[str, float], top_n: int) -> dict[str, float]:
    best_actual = {
        player_id
        for player_id, _ in sorted(actual.items(), key=lambda item: item[1], reverse=True)[:top_n]
    }
    return {
        "actual_points": round(sum(actual[player_id] for player_id in selected), 2),
        "top_player_hit_rate": round(len(set(selected) & best_actual) / max(top_n, 1), 4),
    }


def evaluate_snapshot(rows: list[BacktestPlayer], top_n: int = 12) -> dict[str, object]:
    top_n = min(top_n, len(rows))
    players = [row.player for row in rows]
    actual = {row.player.player_id: row.actual_points for row in rows}
    baseline = sorted(players, key=lambda player: player.projected_points_override or 0, reverse=True)
    engine = DraftEngine(LeagueConfig(), simulation_samples=50)
    enhanced = engine.rank(players, [], 1, 24, top_n)
    baseline_metrics = _metrics([player.player_id for player in baseline[:top_n]], actual, top_n)
    enhanced_metrics = _metrics([str(item["id"]) for item in enhanced], actual, top_n)
    return {
        "players": len(rows),
        "top_n": top_n,
        "baseline": baseline_metrics,
        "enhanced": enhanced_metrics,
        "actual_points_lift": round(
            enhanced_metrics["actual_points"] - baseline_metrics["actual_points"], 2
        ),
        "hit_rate_lift": round(
            enhanced_metrics["top_player_hit_rate"] - baseline_metrics["top_player_hit_rate"],
            4,
        ),
    }


def run_backtest(path: Path, top_n: int = 12) -> dict[str, object]:
    snapshots = load_backtest_csv(path)
    results = {snapshot: evaluate_snapshot(rows, top_n) for snapshot, rows in snapshots.items()}
    return {
        "input": str(path),
        "snapshots": results,
        "average_actual_points_lift": round(
            sum(float(result["actual_points_lift"]) for result in results.values()) / len(results),
            2,
        ),
        "average_hit_rate_lift": round(
            sum(float(result["hit_rate_lift"]) for result in results.values()) / len(results),
            4,
        ),
        "note": "A negative lift means the enhanced strategy needs tuning on these snapshots.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest dated fantasy draft ranking snapshots")
    parser.add_argument("csv", type=Path, help="Dated rankings joined to final fantasy outcomes")
    parser.add_argument("--top", type=int, default=12, help="Number of recommendations to score")
    args = parser.parse_args()
    if args.top < 1:
        parser.error("--top must be positive")
    print(json.dumps(run_backtest(args.csv, args.top), indent=2))


if __name__ == "__main__":
    main()
