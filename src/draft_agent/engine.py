from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from .config import LeagueConfig
from .models import Player
from .scoring import projected_points
from .simulation import simulate_turn_value


@dataclass
class StrategyWeights:
    projection: float = 0.12
    consensus: float = 0.18
    market_quality: float = 0.22
    market_dominance: float = 0.35
    reach_penalty: float = 0.22
    market_disagreement: float = 0.08
    vor: float = 0.12
    scarcity: float = 0.06
    tier_drop: float = 0.05
    te_urgency: float = 0.16
    roster_need: float = 0.16
    position_value: float = 0.16
    lineup_quality: float = 0.18
    bench_opportunity_cost: float = 0.18
    rb_anchor: float = 0.30
    gone_next_pick: float = 0.06
    availability: float = 0.10
    bye_fit: float = 0.01
    portfolio_concentration: float = 0.08
    rb_backfield: float = 0.18
    trend: float = 0.02
    rookie_camp_role: float = 0.03
    preference: float = 0.25
    exposure_penalty: float = 0.18
    upside: float = 0.04
    risk: float = 0.12
    simulation: float = 0.05

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
    replacement_rank = {"QB": 12, "RB": 42, "WR": 36, "TE": 12, "K": 12, "DST": 12}
    strong_starter_market = {"QB": 90, "TE": 60}
    espn_market_weight = 0.70

    def __init__(
        self,
        config: LeagueConfig,
        weights: StrategyWeights | None = None,
        simulation_samples: int = 200,
    ):
        self.config = config
        self.weights = weights or StrategyWeights()
        self.simulation_samples = simulation_samples
        self.prefer_names: set[str] = set()
        self.fade_names: set[str] = set()
        self.never_names: set[str] = set()

    @staticmethod
    def _name_key(value: str) -> str:
        plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        return " ".join(re.sub(r"[^a-z0-9 ]", " ", plain.lower()).split())

    def set_preferences(
        self,
        prefer: list[str] | None = None,
        fade: list[str] | None = None,
        never: list[str] | None = None,
    ) -> None:
        self.prefer_names = {self._name_key(value) for value in prefer or [] if value.strip()}
        self.fade_names = {self._name_key(value) for value in fade or [] if value.strip()}
        self.never_names = {self._name_key(value) for value in never or [] if value.strip()}

    @staticmethod
    def _normalize(values: dict[str, float]) -> dict[str, float]:
        if not values:
            return {}
        low, high = min(values.values()), max(values.values())
        if math.isclose(low, high):
            return {key: 0.5 for key in values}
        return {key: (value - low) / (high - low) for key, value in values.items()}

    @staticmethod
    def _consensus_rank(player: Player) -> float | None:
        value = player.signals.get("consensus_rank")
        if not isinstance(value, (int, float)):
            return None
        rank = float(value)
        return rank if math.isfinite(rank) and rank > 0 else None

    @classmethod
    def _market_rank(cls, player: Player) -> float:
        """Blend ESPN's current room rank with external expert consensus.

        ESPN is the freshest source for the exact room being drafted. External
        ECR remains useful, but it cannot single-handedly turn a player ranked
        multiple rounds later by ESPN into the best available selection.
        """
        consensus_rank = cls._consensus_rank(player)
        if consensus_rank is None:
            return float(player.adp)
        return (
            float(player.adp) * cls.espn_market_weight
            + consensus_rank * (1 - cls.espn_market_weight)
        )

    @classmethod
    def _market_disagreement(cls, player: Player) -> float:
        consensus_rank = cls._consensus_rank(player)
        if consensus_rank is None:
            return 0.0
        return min(abs(float(player.adp) - consensus_rank) / 50, 1.0)

    @staticmethod
    def _market_reach_limit(round_number: int) -> int:
        if round_number == 1:
            return 6
        if round_number <= 4:
            return 10
        if round_number <= 8:
            return 12
        if round_number <= 12:
            return 20
        return 35

    @staticmethod
    def _pick_relative_consensus(
        player: Player,
        current_pick: int,
        reach_limit: int,
    ) -> float:
        """Score market value around this pick instead of the entire pool."""
        market_rank = DraftEngine._market_rank(player)
        return min(
            1.0,
            max(0.0, 0.5 + (current_pick - market_rank) / (2 * reach_limit)),
        )

    @staticmethod
    def _reach_penalty(player: Player, current_pick: int, reach_limit: int) -> float:
        reach = max(0.0, DraftEngine._market_rank(player) - current_pick)
        return min(1.0, reach / reach_limit)

    @staticmethod
    def _market_quality(player: Player, best_market_rank: float) -> float:
        rank_gap = max(0.0, DraftEngine._market_rank(player) - best_market_rank)
        return math.exp(-rank_gap / 12)

    def _preference(self, player: Player) -> float:
        key = self._name_key(player.name)
        if key in self.prefer_names:
            return 1.0
        if key in self.fade_names:
            return -1.0
        return 0.0

    def _rb_anchor(
        self,
        player: Player,
        roster: list[Player],
        round_number: int,
        current_pick: int,
    ) -> float:
        counts = Counter(item.position for item in roster)
        if (
            round_number != 2
            or player.position != "RB"
            or counts["RB"] > 0
            or counts["WR"] == 0
        ):
            return 0.0
        reach = max(0.0, self._market_rank(player) - current_pick)
        if reach > 10:
            return 0.0
        return 1.0 - reach / 20

    def _market_dominated(self, player: Player, pool: list[Player]) -> bool:
        """Reject a lower-market peer unless its projection is materially better."""
        if self._preference(player) > 0:
            return False
        player_rank = self._market_rank(player)
        player_points = projected_points(player)
        return any(
            peer.position == player.position
            and self._market_rank(peer) <= player_rank - 5
            and projected_points(peer) >= player_points * 0.9
            and self._availability(peer) >= self._availability(player)
            and self._preference(peer) >= 0
            for peer in pool
            if peer.player_id != player.player_id
        )

    def _starter_blocks_backup(
        self,
        player: Player,
        roster: list[Player],
    ) -> bool:
        position = player.position
        if position not in self.strong_starter_market:
            return False
        incumbents = [item for item in roster if item.position == position]
        if len(incumbents) < self.config.starters[position]:
            return False
        if self._preference(player) > 0:
            return False
        incumbent = min(incumbents, key=self._market_rank)
        incumbent_market_rank = self._market_rank(incumbent)
        if position == "TE":
            incumbent_points = projected_points(incumbent)
            candidate_points = projected_points(player)
            starter_is_injured = self._availability(incumbent) < 0.86
            material_upgrade = (
                self._market_rank(player) <= incumbent_market_rank - 12
                or (
                    incumbent_points > 0
                    and candidate_points >= incumbent_points * 1.10
                )
            )
            if starter_is_injured:
                return False
            if incumbent_market_rank <= self.strong_starter_market[position]:
                return True
            return not material_upgrade

        # In a 1-QB league, a healthy top-90 overall starter makes QB2 an
        # inefficient bench use unless the candidate is a real upgrade. A weak
        # or injured starter may still justify late insurance.
        starter_is_weak = incumbent_market_rank > self.strong_starter_market["QB"]
        starter_is_injured = self._availability(incumbent) < 0.86
        candidate_market_rank = self._market_rank(player)
        incumbent_points = projected_points(incumbent)
        candidate_points = projected_points(player)
        material_upgrade = (
            candidate_market_rank <= incumbent_market_rank - 15
            or (
                incumbent_points > 0
                and candidate_points >= incumbent_points * 1.05
            )
        )
        return not (starter_is_weak or starter_is_injured or material_upgrade)

    @staticmethod
    def _skill_lineup_points(roster: list[Player]) -> tuple[float, list[float]]:
        by_position = {
            position: sorted(
                (projected_points(item) for item in roster if item.position == position),
                reverse=True,
            )
            for position in ("RB", "WR")
        }
        starters = by_position["RB"][:2] + by_position["WR"][:2]
        flex_pool = by_position["RB"][2:] + by_position["WR"][2:]
        if flex_pool:
            starters.append(max(flex_pool))
        return sum(starters), starters

    def _lineup_quality(self, player: Player, roster: list[Player]) -> float:
        """Measure whether this pick improves starters or only adds redundancy."""
        same_position = [item for item in roster if item.position == player.position]
        required = self.config.starters[player.position]
        if len(same_position) < required:
            return 1.0

        candidate_points = projected_points(player)
        if player.position in {"RB", "WR"}:
            before, current_lineup = self._skill_lineup_points(roster)
            after, _ = self._skill_lineup_points([*roster, player])
            if after > before + 1e-9:
                return 1.0
            if not current_lineup:
                return 1.0
            lineup_cut = min(current_lineup)
            ratio = min(candidate_points / max(lineup_cut, 1.0), 1.0)
            depth = max(0, len(same_position) - required)
            depth_discount = max(0.45, 1.0 - depth * 0.12)
            return max(0.15, 0.7 * ratio * depth_discount)

        incumbent_points = max(projected_points(item) for item in same_position)
        if candidate_points > incumbent_points:
            return 1.0
        if player.position in {"QB", "TE"}:
            best_market_rank = min(self._market_rank(item) for item in same_position)
            threshold = self.strong_starter_market[player.position]
            weakness = min(1.0, max(0.0, (best_market_rank - threshold) / threshold))
            ratio = min(candidate_points / max(incumbent_points, 1.0), 1.0)
            return max(0.05, ratio * (0.2 + 0.45 * weakness))
        return 0.05

    def _bench_opportunity_cost(
        self,
        player: Player,
        roster: list[Player],
        round_number: int,
        current_pick: int,
        next_pick: int,
    ) -> float:
        """Price the last deep bench slot against needs before the next turn.

        The next-pick distance makes this work for every snake slot: the second
        pick at either end of the board carries more pressure than the first,
        while middle slots receive a moderate adjustment.
        """
        counts = Counter(item.position for item in roster)
        depth_threshold = {"RB": 4, "WR": 6}
        threshold = depth_threshold.get(player.position)
        if threshold is None or counts[player.position] < threshold:
            return 0.0

        maximum_wait = max(2 * self.config.teams - 2, 1)
        opponents_before_next = max(next_pick - current_pick - 1, 0)
        wait_pressure = min(opponents_before_next / maximum_wait, 1.0)
        core_open = any(
            counts[position] < self.config.starters[position]
            for position in ("QB", "RB", "WR", "TE")
        )
        late_pressure = min(max((round_number - 7) / 6, 0.0), 1.0)
        return min(
            1.0,
            0.45
            + 0.35 * wait_pressure
            + (0.20 * late_pressure if core_open else 0.0),
        )

    def _roster_need(
        self,
        player: Player,
        roster: list[Player],
        round_number: int,
        current_pick: int,
    ) -> float:
        counts = Counter(item.position for item in roster)
        position = player.position
        if counts[position] >= self.config.position_caps[position]:
            return -1.0
        # This league starts one QB and one TE. A second one is a late bench
        # option, never a reason to pass on starting RB/WR talent in rounds 1-12.
        if position == "QB":
            if counts[position] and round_number < 13:
                return -1.0
            # A second quarterback is optional in a 1-QB league. Only spend the
            # roster spot when the market has let one fall at least 20 picks.
            if counts[position] and current_pick - self._market_rank(player) < 20:
                return -1.0
            if counts[position] and self._starter_blocks_backup(player, roster):
                return -1.0
            if not counts[position] and round_number < 4:
                market_rank = self._market_rank(player)
                early_value = market_rank <= 36 and current_pick - market_rank >= 12
                if not early_value:
                    return -1.0
        # This league's RB pool dries up faster than its WR pool. After a WR-WR
        # opening, do not take WR3 before securing RB1, even when the receiver
        # falls past consensus.
        if (
            position == "WR"
            and counts["WR"] >= 2
            and counts["RB"] == 0
            and round_number < 4
        ):
            return -1.0
        if position == "TE":
            if counts[position] and round_number < 13:
                return -1.0
            if counts[position] and self._starter_blocks_backup(player, roster):
                return -1.0
            if counts[position] == 0 and round_number < 4:
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
        if round_number >= 3 and counts["RB"] < 1:
            required.add("RB")
        if round_number >= 3 and counts["WR"] < 1:
            required.add("WR")
        if round_number >= 5 and counts["RB"] < 2:
            required.add("RB")
        if round_number >= 5 and counts["WR"] < 2:
            required.add("WR")
        if round_number >= 8 and counts["RB"] >= counts["WR"] + 2 and counts["WR"] < 5:
            required.add("WR")
        if round_number >= 8 and counts["WR"] >= counts["RB"] + 3 and counts["RB"] < 5:
            required.add("RB")
        if round_number >= 10 and counts["QB"] < 1:
            required.add("QB")
        if round_number >= 13 and counts["TE"] < 1:
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
    def _te_urgency(
        player: Player,
        roster: list[Player],
        round_number: int,
        tier_drop: float,
    ) -> float:
        if player.position != "TE" or any(item.position == "TE" for item in roster):
            return 0.0
        if round_number not in {7, 8, 9, 10, 11, 12}:
            return 0.0
        stage = {7: 0.2, 8: 0.45, 9: 0.7, 10: 1.0, 11: 1.0, 12: 1.0}[
            round_number
        ]
        # Start with a small round-seven warning, then strongly price the final
        # useful starter tier by round ten. A nearby projection cliff increases
        # urgency without forcing a TE who is outside the market reach guard.
        return stage * (0.75 + 0.25 * tier_drop)

    @staticmethod
    def _rookie_camp_role(player: Player) -> float:
        years_exp = player.signals.get("years_exp")
        if years_exp is None or years_exp > 0:
            return 0.5

        depth_order = player.signals.get("depth_chart_order")
        if depth_order is None:
            depth_score = 0.45
        elif depth_order <= 1:
            depth_score = 1.0
        elif depth_order <= 2:
            depth_score = 0.72
        elif depth_order <= 3:
            depth_score = 0.42
        else:
            depth_score = 0.18

        practice = player.context.get("practice", "").lower()
        if "full" in practice:
            practice_score = 1.0
        elif "limited" in practice:
            practice_score = 0.62
        elif "did not" in practice or "dnp" in practice:
            practice_score = 0.1
        else:
            practice_score = 0.5
        return depth_score * 0.75 + practice_score * 0.25

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

    @staticmethod
    def _portfolio_concentration(player: Player, roster: list[Player]) -> float:
        """Penalize a third correlated player much more than a second one."""
        team = player.team.strip().upper()
        same_team = (
            sum(item.team.strip().upper() == team for item in roster)
            if team and team != "FA"
            else 0
        )
        bye = player.signals.get("bye_week")
        same_bye = (
            sum(item.signals.get("bye_week") == bye for item in roster)
            if bye
            else 0
        )

        def severity(count: int, *, bye_week: bool = False) -> float:
            if count <= 0:
                return 0.0
            if count == 1:
                return 0.25
            if bye_week and count == 2:
                return 0.75
            return 1.0

        team_penalty = severity(same_team)
        bye_penalty = severity(same_bye, bye_week=True)
        return 0.65 * team_penalty + 0.35 * bye_penalty

    def _rb_backfield_penalty(
        self,
        player: Player,
        roster: list[Player],
        candidates: list[Player],
        round_number: int,
    ) -> float:
        """Break close early RB ties away from a teammate's backfield.

        The guard is deliberately narrow: it ends in round 12, applies only
        when an RB from that NFL team is already rostered, and disappears for
        an explicit Prefer or when no comparably valued independent RB exists.
        """
        team = player.team.strip().upper()
        if (
            round_number >= 12
            or player.position != "RB"
            or not team
            or team == "FA"
            or self._preference(player) > 0
            or not any(
                item.position == "RB" and item.team.strip().upper() == team
                for item in roster
            )
        ):
            return 0.0
        rostered_rb_teams = {
            item.team.strip().upper()
            for item in roster
            if item.position == "RB" and item.team.strip().upper() not in {"", "FA"}
        }
        player_rank = self._market_rank(player)
        player_points = projected_points(player)
        close_independent = any(
            peer.position == "RB"
            and peer.player_id != player.player_id
            and peer.team.strip().upper() not in rostered_rb_teams
            and abs(self._market_rank(peer) - player_rank) <= 5
            and projected_points(peer) >= player_points * 0.95
            and self._availability(peer) >= self._availability(player)
            and self._preference(peer) >= 0
            for peer in candidates
        )
        return 1.0 if close_independent else 0.0

    def rank(
        self,
        available: list[Player],
        roster: list[Player],
        current_pick: int,
        next_pick: int,
        limit: int = 10,
        exposure_rates: dict[str, float] | None = None,
        exposure_limit: float = 0.0,
    ) -> list[dict[str, object]]:
        round_number = (current_pick - 1) // self.config.teams + 1
        required_positions = self._required_positions(roster, round_number)
        base_eligible = [
            player
            for player in available
            if self._roster_need(player, roster, round_number, current_pick) >= 0
            and self._name_key(player.name) not in self.never_names
        ]
        # Zero explicitly means "off": prior mock results must not influence a
        # best-player draft unless the user configures an exposure percentage.
        exposure_rates = (exposure_rates or {}) if exposure_limit > 0 else {}
        if exposure_limit > 0:
            under_limit = [
                player
                for player in base_eligible
                if exposure_rates.get(player.external_ids.get("espn", ""), 0.0)
                < exposure_limit
            ]
            if under_limit:
                base_eligible = under_limit

        reach_limit = self._market_reach_limit(round_number)
        eligible = base_eligible
        if required_positions:
            required_pool = [
                player for player in base_eligible if player.position in required_positions
            ]
            required_limit = (
                min(reach_limit, 10)
                if required_positions.issubset({"RB", "WR"})
                else reach_limit
            )
            required_in_range = [
                player
                for player in required_pool
                if self._market_rank(player) - current_pick <= required_limit
            ]
            if required_in_range:
                eligible = required_in_range
            elif round_number >= 13:
                eligible = required_pool
        # Use consensus/ADP as a guardrail rather than letting a noisy model
        # component reach multiple rounds. Early RB/WR targets are soft when
        # every candidate would exceed a ten-pick reach.
        market_eligible = [
            player
            for player in eligible
            if self._market_rank(player) - current_pick <= reach_limit
        ]
        if market_eligible:
            eligible = market_eligible
        # Do not let an IR/PUP projection anomaly become an early-round auto
        # pick. Late IR stashes remain possible after the starting core is built.
        if round_number <= 12:
            active_eligible = [player for player in eligible if self._availability(player) > 0]
            if active_eligible:
                eligible = active_eligible
        points = {p.player_id: projected_points(p) for p in eligible}
        by_position: dict[str, list[Player]] = defaultdict(list)
        for player in eligible:
            by_position[player.position].append(player)
        for group in by_position.values():
            group.sort(key=lambda item: points[item.player_id], reverse=True)

        projection_relative: dict[str, float] = {}
        for group in by_position.values():
            position_high = max(points[player.player_id] for player in group)
            projection_relative.update(
                {
                    player.player_id: (
                        points[player.player_id] / position_high
                        if position_high > 0
                        else 0.5
                    )
                    for player in group
                }
            )

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

        consensus_score = {
            player.player_id: self._pick_relative_consensus(
                player, current_pick, reach_limit
            )
            for player in eligible
        }
        best_market_rank = min(
            (self._market_rank(player) for player in eligible),
            default=float(current_pick),
        )
        market_quality = {
            player.player_id: self._market_quality(player, best_market_rank)
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
            "consensus": consensus_score,
            "market_quality": market_quality,
            "vor": self._normalize(vor_raw),
            "scarcity": self._normalize(scarcity_raw),
            "tier_drop": self._normalize(tier_raw),
            "trend": self._normalize(trend_raw),
        }
        results: list[dict[str, object]] = []
        picks_away = max(next_pick - current_pick, 1)
        for player in eligible:
            # Logistic approximation: earlier ADP and a longer wait increase the chance gone.
            market_rank = self._market_rank(player)
            market_dominance = 1.0 if self._market_dominated(player, eligible) else 0.0
            reach_penalty = self._reach_penalty(player, current_pick, reach_limit)
            market_disagreement = self._market_disagreement(player)
            exposure = exposure_rates.get(player.external_ids.get("espn", ""), 0.0)
            uncertainty = min(player.signals.get("consensus_sd", 8.0) / 30, 1.0)
            gone = 1 / (1 + math.exp((market_rank - (current_pick + picks_away * 0.55)) / (6.5 + 4 * uncertainty)))
            roster_need = self._roster_need(player, roster, round_number, current_pick)
            position_value = self._position_value(player, roster, round_number)
            lineup_quality = self._lineup_quality(player, roster)
            bench_opportunity_cost = self._bench_opportunity_cost(
                player,
                roster,
                round_number,
                current_pick,
                next_pick,
            )
            rb_anchor = self._rb_anchor(player, roster, round_number, current_pick)
            preference = self._preference(player)
            te_urgency = self._te_urgency(
                player,
                roster,
                round_number,
                components["tier_drop"][player.player_id],
            )
            rookie_camp_role = self._rookie_camp_role(player)
            availability = self._availability(player)
            bye_fit = self._bye_fit(player, roster)
            portfolio_concentration = self._portfolio_concentration(player, roster)
            rb_backfield = self._rb_backfield_penalty(
                player, roster, eligible, round_number
            )
            effective_risk = min(1.0, player.risk + (1 - availability) * 0.7 + uncertainty * 0.15)
            detail = {
                "projection": components["projection"][player.player_id],
                "consensus": components["consensus"][player.player_id],
                "market_quality": components["market_quality"][player.player_id],
                "market_dominance": market_dominance,
                "reach_penalty": reach_penalty,
                "market_disagreement": market_disagreement,
                "vor": components["vor"][player.player_id],
                "scarcity": components["scarcity"][player.player_id],
                "tier_drop": components["tier_drop"][player.player_id],
                "te_urgency": te_urgency,
                "roster_need": roster_need,
                "position_value": position_value,
                "lineup_quality": lineup_quality,
                "bench_opportunity_cost": bench_opportunity_cost,
                "rb_anchor": rb_anchor,
                "gone_next_pick": gone,
                "availability": availability,
                "bye_fit": bye_fit,
                "portfolio_concentration": portfolio_concentration,
                "rb_backfield": rb_backfield,
                "trend": components["trend"][player.player_id],
                "rookie_camp_role": rookie_camp_role,
                "preference": preference,
                "exposure_penalty": exposure,
                "upside": player.upside,
                "risk": effective_risk,
            }
            penalty_components = {
                "market_dominance",
                "reach_penalty",
                "market_disagreement",
                "exposure_penalty",
                "bench_opportunity_cost",
                "portfolio_concentration",
                "rb_backfield",
                "risk",
            }
            contributions = {
                key: getattr(self.weights, key) * value * (-1 if key in penalty_components else 1)
                for key, value in detail.items()
            }
            score = sum(contributions.values())
            results.append(
                {
                    **player.as_dict(),
                    "projected_points": round(points[player.player_id], 1),
                    "espn_rank": round(float(player.adp), 1),
                    "consensus_rank": (
                        round(self._consensus_rank(player), 1)
                        if self._consensus_rank(player) is not None
                        else None
                    ),
                    "market_rank": round(market_rank, 1),
                    "market_disagreement": round(market_disagreement, 3),
                    "market_reach": round(max(0.0, market_rank - current_pick), 1),
                    "market_reach_limit": reach_limit,
                    "draft_score": round(score, 4),
                    "components": {key: round(value, 3) for key, value in detail.items()},
                    "contributions": {
                        key: round(value, 4) for key, value in contributions.items()
                    },
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
                result["contributions"]["simulation"] = 0.0
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
            result["contributions"]["simulation"] = round(
                self.weights.simulation * future_values[player_id], 4
            )
        results.sort(key=lambda item: (-float(item["draft_score"]), float(item["adp"])))
        return results[:limit]
