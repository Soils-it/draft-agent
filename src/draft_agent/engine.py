from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from .config import LeagueConfig
from .models import Player
from .scoring import projected_points
from .simulation import simulate_turn_value


@dataclass
class StrategyWeights:
    projection: float = 0.20
    consensus: float = 0.16
    vor: float = 0.18
    scarcity: float = 0.10
    tier_drop: float = 0.08
    roster_need: float = 0.12
    position_value: float = 0.12
    gone_next_pick: float = 0.08
    availability: float = 0.06
    bye_fit: float = 0.02
    trend: float = 0.02
    upside: float = 0.05
    risk: float = 0.07
    simulation: float = 0.12

    def update(self, values: dict[str, float]) -> None:
        for key in asdict(self):
            if key in values:
                value = float(values[key])
                if not 0 <= value <= 1:
                    raise ValueError(f"{key} must be between 0 and 1")
                setattr(self, key, value)


class DraftEngine:
    # A 12-team, 1-QB league replaces quarterbacks near QB12, while the FLEX
    # and deeper RB/WR benches push replacement much farther down those pools.
    replacement_rank = {"QB": 12, "RB": 36, "WR": 42, "TE": 12, "K": 12, "DST": 12}

    def __init__(
        self,
        config: LeagueConfig,
        weights: StrategyWeights | None = None,
        simulation_samples: int = 200,
    ):
        self.config = config
        self.weights = weights or StrategyWeights()
        self.simulation_samples = simulation_samples

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
        # This league starts one QB and one TE. A second one is a late bench
        # option, never a reason to pass on starting RB/WR talent in rounds 1-12.
        if position == "QB":
            if counts[position] and round_number < 13:
                return -1.0
            if not counts[position] and round_number < 4:
                return -1.0
        if position == "TE" and counts[position] and round_number < 13:
            return -1.0
        if position in {"K", "DST"}:
            return 1.25 if round_number >= 15 and counts[position] == 0 else -1.0
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

    def _required_positions(self, roster: list[Player], round_number: int) -> set[str]:
        """Return starter positions that can no longer safely be deferred."""
        counts = Counter(item.position for item in roster)
        required: set[str] = set()
        if round_number >= 4 and counts["RB"] < 1:
            required.add("RB")
        if round_number >= 4 and counts["WR"] < 1:
            required.add("WR")
        if round_number >= 6 and counts["RB"] < 2:
            required.add("RB")
        if round_number >= 7 and counts["WR"] < 2:
            required.add("WR")
        if round_number >= 8 and counts["RB"] >= counts["WR"] + 2 and counts["WR"] < 5:
            required.add("WR")
        if round_number >= 8 and counts["WR"] >= counts["RB"] + 3 and counts["RB"] < 5:
            required.add("RB")
        if round_number >= 10 and counts["QB"] < 1:
            required.add("QB")
        if round_number >= 11 and counts["TE"] < 1:
            required.add("TE")
        if round_number >= 15:
            required.update(position for position in ("K", "DST") if counts[position] < 1)
        return required

    @staticmethod
    def _position_value(player: Player, roster: list[Player], round_number: int) -> float:
        counts = Counter(item.position for item in roster)
        position = player.position
        if position == "RB":
            if counts[position] < 2:
                return 1.0
            if counts[position] >= 4:
                return 0.3
            return 0.9 if round_number <= 8 else 0.65
        if position == "WR":
            if counts[position] < 2:
                return 0.92
            if counts[position] >= 5:
                return 0.25
            return 0.78 if round_number <= 8 else 0.62
        if position == "QB":
            return 0.62 if counts[position] == 0 else 0.12
        if position == "TE":
            return 0.6 if counts[position] == 0 else 0.12
        return 0.05

    @staticmethod
    def _availability(player: Player) -> float:
        injury = player.context.get("injury_status", "").lower()
        nfl_status = player.context.get("nfl_status", "").lower()
        if any(value in injury or value in nfl_status for value in ("reserve", "injured reserve", "pup")):
            return 0.0
        if injury in {"out", "doubtful"} or nfl_status in {"out", "inactive"}:
            return 0.12
        if injury in {"questionable", "probable"}:
            return 0.62 if injury == "questionable" else 0.86
        return 1.0

    @staticmethod
    def _bye_fit(player: Player, roster: list[Player]) -> float:
        bye = player.signals.get("bye_week")
        if not bye:
            return 0.5
        clashes = sum(
            item.position == player.position and item.signals.get("bye_week") == bye
            for item in roster
        )
        return max(0.0, 1.0 - clashes * 0.5)

    def rank(
        self,
        available: list[Player],
        roster: list[Player],
        current_pick: int,
        next_pick: int,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        round_number = (current_pick - 1) // self.config.teams + 1
        required_positions = self._required_positions(roster, round_number)
        eligible = [
            player
            for player in available
            if self._roster_need(player, roster, round_number) >= 0
            and (not required_positions or player.position in required_positions)
        ]
        points = {p.player_id: projected_points(p) for p in eligible}
        by_position: dict[str, list[Player]] = defaultdict(list)
        for player in eligible:
            by_position[player.position].append(player)
        for group in by_position.values():
            group.sort(key=lambda item: points[item.player_id], reverse=True)

        projection_relative: dict[str, float] = {}
        for group in by_position.values():
            normalized = self._normalize({player.player_id: points[player.player_id] for player in group})
            projection_relative.update(normalized)

        vor_raw: dict[str, float] = {}
        scarcity_raw: dict[str, float] = {}
        tier_raw: dict[str, float] = {}
        for player in eligible:
            group = by_position[player.position]
            replacement_index = min(self.replacement_rank[player.position] - 1, len(group) - 1)
            replacement = points[group[replacement_index].player_id]
            vor_raw[player.player_id] = points[player.player_id] - replacement
            player_index = group.index(player)
            lookahead = min(player_index + 3, len(group) - 1)
            scarcity_raw[player.player_id] = points[player.player_id] - points[group[lookahead].player_id]
            next_index = min(player_index + 1, len(group) - 1)
            tier_raw[player.player_id] = points[player.player_id] - points[group[next_index].player_id]

        consensus_raw = {
            player.player_id: -player.signals.get("consensus_rank", player.adp)
            for player in eligible
        }
        trend_raw = {
            player.player_id: math.copysign(
                math.log1p(abs(player.signals.get("trend_adds_24h", 0) - player.signals.get("trend_drops_24h", 0))),
                player.signals.get("trend_adds_24h", 0) - player.signals.get("trend_drops_24h", 0),
            )
            for player in eligible
        }

        components = {
            # Raw QB totals cannot be compared to RB/WR totals in a 1-QB league.
            # This measures projection quality within each player's position.
            "projection": projection_relative,
            "consensus": self._normalize(consensus_raw),
            "vor": self._normalize(vor_raw),
            "scarcity": self._normalize(scarcity_raw),
            "tier_drop": self._normalize(tier_raw),
            "trend": self._normalize(trend_raw),
        }
        results: list[dict[str, object]] = []
        picks_away = max(next_pick - current_pick, 1)
        for player in eligible:
            # Logistic approximation: earlier ADP and a longer wait increase the chance gone.
            market_rank = player.signals.get("consensus_rank", player.adp)
            blended_rank = market_rank * 0.7 + player.adp * 0.3
            uncertainty = min(player.signals.get("consensus_sd", 8.0) / 30, 1.0)
            gone = 1 / (1 + math.exp((blended_rank - (current_pick + picks_away * 0.55)) / (6.5 + 4 * uncertainty)))
            roster_need = self._roster_need(player, roster, round_number)
            position_value = self._position_value(player, roster, round_number)
            availability = self._availability(player)
            bye_fit = self._bye_fit(player, roster)
            effective_risk = min(1.0, player.risk + (1 - availability) * 0.7 + uncertainty * 0.15)
            score = (
                self.weights.projection * components["projection"][player.player_id]
                + self.weights.consensus * components["consensus"][player.player_id]
                + self.weights.vor * components["vor"][player.player_id]
                + self.weights.scarcity * components["scarcity"][player.player_id]
                + self.weights.tier_drop * components["tier_drop"][player.player_id]
                + self.weights.roster_need * roster_need
                + self.weights.position_value * position_value
                + self.weights.gone_next_pick * gone
                + self.weights.availability * availability
                + self.weights.bye_fit * bye_fit
                + self.weights.trend * components["trend"][player.player_id]
                + self.weights.upside * player.upside
                - self.weights.risk * effective_risk
            )
            detail = {
                "projection": components["projection"][player.player_id],
                "consensus": components["consensus"][player.player_id],
                "vor": components["vor"][player.player_id],
                "scarcity": components["scarcity"][player.player_id],
                "tier_drop": components["tier_drop"][player.player_id],
                "roster_need": roster_need,
                "position_value": position_value,
                "gone_next_pick": gone,
                "availability": availability,
                "bye_fit": bye_fit,
                "trend": components["trend"][player.player_id],
                "upside": player.upside,
                "risk": effective_risk,
            }
            results.append(
                {
                    **player.as_dict(),
                    "projected_points": round(points[player.player_id], 1),
                    "draft_score": round(score, 4),
                    "components": {key: round(value, 3) for key, value in detail.items()},
                }
            )
        # Simulate marginal value over replacement, not raw fantasy points.
        # Otherwise every trial incorrectly treats a second high-scoring QB as
        # more useful than a starting RB or WR.
        simulation_values = {
            player_id: max(value, 0.0) for player_id, value in vor_raw.items()
        }
        simulation = simulate_turn_value(
            eligible,
            simulation_values,
            current_pick,
            next_pick,
            self.simulation_samples,
        )
        future_values = self._normalize(
            {player_id: value.expected_roster_value for player_id, value in simulation.items()}
        )
        for result in results:
            player_id = str(result["id"])
            outcome = simulation.get(player_id)
            if outcome is None:
                result["survival_probability"] = None
                result["expected_roster_value"] = None
                result["simulation_samples"] = self.simulation_samples
                result["components"]["simulation"] = 0.0
                continue
            result["draft_score"] = round(
                float(result["draft_score"])
                + self.weights.simulation * future_values[player_id],
                4,
            )
            result["survival_probability"] = round(outcome.survival_probability, 3)
            result["expected_roster_value"] = round(outcome.expected_roster_value, 1)
            result["simulation_samples"] = self.simulation_samples
            result["components"]["simulation"] = round(future_values[player_id], 3)
        results.sort(key=lambda item: (-float(item["draft_score"]), float(item["adp"])))
        return results[:limit]
