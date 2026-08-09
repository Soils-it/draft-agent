from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from .config import LeagueConfig
from .models import Player
from .scoring import projected_points


@dataclass
class StrategyWeights:
    projection: float = 0.25
    vor: float = 0.23
    scarcity: float = 0.14
    roster_need: float = 0.14
    gone_next_pick: float = 0.10
    upside: float = 0.08
    risk: float = 0.06

    def update(self, values: dict[str, float]) -> None:
        for key in asdict(self):
            if key in values:
                value = float(values[key])
                if not 0 <= value <= 1:
                    raise ValueError(f"{key} must be between 0 and 1")
                setattr(self, key, value)


class DraftEngine:
    replacement_rank = {"QB": 12, "RB": 30, "WR": 30, "TE": 12, "K": 12, "DST": 12}

    def __init__(self, config: LeagueConfig, weights: StrategyWeights | None = None):
        self.config = config
        self.weights = weights or StrategyWeights()

    @staticmethod
    def _normalize(values: dict[str, float]) -> dict[str, float]:
        if not values:
            return {}
        low, high = min(values.values()), max(values.values())
        if math.isclose(low, high):
            return {key: 0.5 for key in values}
        return {key: (value - low) / (high - low) for key, value in values.items()}

    def _roster_need(self, player: Player, roster: list[Player], round_number: int) -> float:
        counts = Counter(item.position for item in roster)
        position = player.position
        if counts[position] >= self.config.position_caps[position]:
            return -1.0
        if position in {"K", "DST"}:
            return 0.8 if round_number >= 15 and counts[position] == 0 else 0.02
        required = self.config.starters[position]
        if counts[position] < required:
            return 1.0
        if position in {"RB", "WR"} and counts["RB"] + counts["WR"] < 5:
            return 0.8
        if position in {"RB", "WR"}:
            return 0.55
        if position in {"QB", "TE"} and counts[position] == 0:
            return 0.9
        return 0.18

    def rank(
        self,
        available: list[Player],
        roster: list[Player],
        current_pick: int,
        next_pick: int,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        round_number = (current_pick - 1) // self.config.teams + 1
        eligible = [
            player
            for player in available
            if self._roster_need(player, roster, round_number) >= 0
        ]
        points = {p.player_id: projected_points(p) for p in eligible}
        by_position: dict[str, list[Player]] = defaultdict(list)
        for player in eligible:
            by_position[player.position].append(player)
        for group in by_position.values():
            group.sort(key=lambda item: points[item.player_id], reverse=True)

        vor_raw: dict[str, float] = {}
        scarcity_raw: dict[str, float] = {}
        for player in eligible:
            group = by_position[player.position]
            replacement_index = min(self.replacement_rank[player.position] - 1, len(group) - 1)
            replacement = points[group[replacement_index].player_id]
            vor_raw[player.player_id] = points[player.player_id] - replacement
            player_index = group.index(player)
            lookahead = min(player_index + 3, len(group) - 1)
            scarcity_raw[player.player_id] = points[player.player_id] - points[group[lookahead].player_id]

        components = {
            "projection": self._normalize(points),
            "vor": self._normalize(vor_raw),
            "scarcity": self._normalize(scarcity_raw),
        }
        results: list[dict[str, object]] = []
        picks_away = max(next_pick - current_pick, 1)
        for player in eligible:
            # Logistic approximation: earlier ADP and a longer wait increase the chance gone.
            gone = 1 / (1 + math.exp((player.adp - (current_pick + picks_away * 0.55)) / 7.5))
            roster_need = self._roster_need(player, roster, round_number)
            score = (
                self.weights.projection * components["projection"][player.player_id]
                + self.weights.vor * components["vor"][player.player_id]
                + self.weights.scarcity * components["scarcity"][player.player_id]
                + self.weights.roster_need * roster_need
                + self.weights.gone_next_pick * gone
                + self.weights.upside * player.upside
                - self.weights.risk * player.risk
            )
            detail = {
                "projection": components["projection"][player.player_id],
                "vor": components["vor"][player.player_id],
                "scarcity": components["scarcity"][player.player_id],
                "roster_need": roster_need,
                "gone_next_pick": gone,
                "upside": player.upside,
                "risk": player.risk,
            }
            results.append(
                {
                    **player.as_dict(),
                    "projected_points": round(points[player.player_id], 1),
                    "draft_score": round(score, 4),
                    "components": {key: round(value, 3) for key, value in detail.items()},
                }
            )
        results.sort(key=lambda item: (-float(item["draft_score"]), float(item["adp"])))
        return results[:limit]
