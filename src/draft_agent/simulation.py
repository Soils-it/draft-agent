from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

from .models import Player


@dataclass(frozen=True)
class SimulationResult:
    survival_probability: float
    expected_roster_value: float


def _seed(players: list[Player], current_pick: int, next_pick: int, samples: int) -> int:
    identity = "|".join(sorted(player.player_id for player in players))
    digest = hashlib.sha256(f"{current_pick}:{next_pick}:{samples}:{identity}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _market_rank(player: Player) -> float:
    return player.signals.get("consensus_rank", player.adp)


def _weighted_pick(
    rng: random.Random,
    pool: list[Player],
    pick_number: int,
    market_ranks: dict[str, float] | None = None,
) -> int:
    weights = [
        math.exp(
            max(
                -4.0,
                min(
                    4.0,
                    (
                        pick_number
                        + 8
                        - (market_ranks or {}).get(player.player_id, _market_rank(player))
                    )
                    / 18,
                ),
            )
        )
        for player in pool
    ]
    target = rng.random() * sum(weights)
    running = 0.0
    for index, weight in enumerate(weights):
        running += weight
        if running >= target:
            return index
    return len(pool) - 1


def simulate_turn_value(
    players: list[Player],
    points: dict[str, float],
    current_pick: int,
    next_pick: int,
    samples: int = 200,
    candidate_limit: int = 24,
    market_ranks: dict[str, float] | None = None,
) -> dict[str, SimulationResult]:
    """Estimate market survival and two-pick roster value with deterministic trials."""
    if samples <= 0 or not players:
        return {}
    opponents = max(0, next_pick - current_pick - 1)
    candidates = sorted(
        players,
        key=lambda player: (
            (market_ranks or {}).get(player.player_id, _market_rank(player)),
            -points[player.player_id],
        ),
    )[:candidate_limit]
    rng = random.Random(_seed(players, current_pick, next_pick, samples))
    survived = {player.player_id: 0 for player in candidates}
    followup_total = {player.player_id: 0.0 for player in candidates}

    for _ in range(samples):
        drafted: set[str] = set()
        pool = list(players)
        for offset in range(min(opponents, len(pool))):
            chosen = pool.pop(
                _weighted_pick(
                    rng,
                    pool,
                    current_pick + offset + 1,
                    market_ranks,
                )
            )
            drafted.add(chosen.player_id)
        remaining = [player for player in players if player.player_id not in drafted]
        for candidate in candidates:
            if candidate.player_id not in drafted:
                survived[candidate.player_id] += 1
            followup_total[candidate.player_id] += max(
                (
                    points[player.player_id]
                    for player in remaining
                    if player.player_id != candidate.player_id
                ),
                default=0.0,
            )

    return {
        player.player_id: SimulationResult(
            survival_probability=survived[player.player_id] / samples,
            expected_roster_value=(
                points[player.player_id] + followup_total[player.player_id] / samples
            ),
        )
        for player in candidates
    }
