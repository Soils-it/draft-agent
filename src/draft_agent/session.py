from __future__ import annotations

from collections import Counter

from .config import LeagueConfig
from .engine import DraftEngine
from .models import Pick, Player
from .scoring import projected_points


class DraftSession:
    def __init__(self, players: list[Player], config: LeagueConfig | None = None):
        self.config = config or LeagueConfig()
        self.players = {player.player_id: player for player in players}
        self.engine = DraftEngine(self.config)
        self.picks: list[Pick] = []
        self._advance_opponents()

    @property
    def current_pick(self) -> int:
        return len(self.picks) + 1

    def team_for_pick(self, overall: int) -> int:
        round_number = (overall - 1) // self.config.teams + 1
        within_round = (overall - 1) % self.config.teams + 1
        return within_round if round_number % 2 else self.config.teams + 1 - within_round

    def next_user_pick(self, after: int | None = None) -> int:
        start = self.current_pick if after is None else after + 1
        final_pick = self.config.teams * self.config.roster_size
        for overall in range(start, final_pick + 1):
            if self.team_for_pick(overall) == self.config.user_slot:
                return overall
        return final_pick + 1

    def available(self) -> list[Player]:
        drafted = {pick.player_id for pick in self.picks}
        return [player for player_id, player in self.players.items() if player_id not in drafted]

    def user_roster(self) -> list[Player]:
        return [
            self.players[pick.player_id]
            for pick in self.picks
            if pick.team_slot == self.config.user_slot
        ]

    def recommendations(self, limit: int = 5) -> list[dict[str, object]]:
        if self.is_complete or self.team_for_pick(self.current_pick) != self.config.user_slot:
            return []
        return self.engine.rank(
            self.available(),
            self.user_roster(),
            self.current_pick,
            self.next_user_pick(self.current_pick),
            limit,
        )

    @property
    def is_complete(self) -> bool:
        return self.current_pick > self.config.teams * self.config.roster_size

    def make_user_pick(self, player_id: str, source: str = "manual") -> None:
        if self.is_complete:
            raise ValueError("draft is complete")
        if self.team_for_pick(self.current_pick) != self.config.user_slot:
            raise ValueError("it is not the user's turn")
        if player_id not in {player.player_id for player in self.available()}:
            raise ValueError("player is not available")
        player = self.players[player_id]
        counts = Counter(item.position for item in self.user_roster())
        if counts[player.position] >= self.config.position_caps[player.position]:
            raise ValueError(f"roster already has the maximum number of {player.position}")
        self.picks.append(Pick(self.current_pick, self.config.user_slot, player_id, source))
        self._advance_opponents()

    def _advance_opponents(self) -> None:
        while not self.is_complete and self.team_for_pick(self.current_pick) != self.config.user_slot:
            available = self.available()
            if not available:
                return
            # Deterministic mock opponents use the active profile's market. This
            # matters in Superflex, where the normal ESPN overall rank materially
            # understates how quickly quarterbacks leave the board.
            player = min(
                available,
                key=lambda item: (
                    (
                        self.engine._draft_market_rank(item)
                        if self.config.is_superflex
                        else item.adp
                    ),
                    -item.upside,
                    item.player_id,
                ),
            )
            self.picks.append(
                Pick(self.current_pick, self.team_for_pick(self.current_pick), player.player_id, "opponent")
            )

    def as_dict(self, include_recommendations: bool = True) -> dict[str, object]:
        roster = self.user_roster()
        latest = self.picks[-12:]
        return {
            "current_pick": self.current_pick,
            "round": min((self.current_pick - 1) // self.config.teams + 1, self.config.roster_size),
            "user_slot": self.config.user_slot,
            "is_complete": self.is_complete,
            "on_clock": not self.is_complete and self.team_for_pick(self.current_pick) == self.config.user_slot,
            "next_user_pick": None if self.is_complete else self.next_user_pick(self.current_pick),
            "roster": [
                {**player.as_dict(), "projected_points": round(projected_points(player), 1)}
                for player in roster
            ],
            "recommendations": self.recommendations() if include_recommendations else [],
            "weights": self.engine.weights.__dict__,
            "recent_picks": [
                {
                    "overall": pick.overall,
                    "team_slot": pick.team_slot,
                    "player": self.players[pick.player_id].name,
                    "position": self.players[pick.player_id].position,
                    "source": pick.source,
                }
                for pick in latest
            ],
        }
