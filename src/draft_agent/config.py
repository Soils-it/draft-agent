from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LeagueConfig:
    teams: int = 12
    user_slot: int = 6
    roster_size: int = 16
    ir_slots: int = 1
    starters: dict[str, int] = field(
        default_factory=lambda: {
            "QB": 1,
            "RB": 2,
            "WR": 2,
            "TE": 1,
            "FLEX": 1,
            "K": 1,
            "DST": 1,
        }
    )
    position_caps: dict[str, int] = field(
        default_factory=lambda: {
            "QB": 2,
            "RB": 5,
            "WR": 8,
            "TE": 2,
            "K": 1,
            "DST": 1,
        }
    )


# Keys are projection-stat names. Rates exactly match the supplied league rules.
SCORING_RATES: dict[str, float] = {
    "passing_yards": 0.04,
    "passing_tds": 4,
    "interceptions": -2,
    "passing_2pt": 2,
    "rushing_yards": 0.1,
    "rushing_tds": 6,
    "rushing_2pt": 2,
    "receiving_yards": 0.1,
    "receptions": 1,
    "receiving_tds": 6,
    "receiving_2pt": 2,
    "pat_made": 1,
    "fg_missed": -1,
    "fg_0_39": 3,
    "fg_40_49": 4,
    "fg_50_59": 5,
    "fg_60_plus": 6,
    "kick_return_tds": 6,
    "punt_return_tds": 6,
    "interception_return_tds": 6,
    "fumble_return_tds": 6,
    "blocked_return_tds": 6,
    "two_point_returns": 2,
    "one_point_safeties": 1,
    "sacks": 1,
    "blocks": 2,
    "defensive_interceptions": 2,
    "fumble_recoveries": 2,
    "safeties": 2,
}

POINTS_ALLOWED_BONUS = (
    (0, 0, 5),
    (1, 6, 4),
    (7, 13, 3),
    (14, 17, 1),
    (18, 27, 0),
    (28, 34, -1),
    (35, 45, -3),
    (46, float("inf"), -5),
)

YARDS_ALLOWED_BONUS = (
    (0, 99, 5),
    (100, 199, 3),
    (200, 299, 2),
    (300, 349, 0),
    (350, 399, -1),
    (400, 449, -3),
    (450, 499, -5),
    (500, 549, -6),
    (550, float("inf"), -7),
)
