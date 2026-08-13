from __future__ import annotations

from dataclasses import dataclass, field


STANDARD_PPR = "ppr_1qb"
SUPERFLEX_REPLACES_FLEX = "ppr_superflex"
FLEX_AND_SUPERFLEX = "ppr_flex_superflex"


LEAGUE_PROFILES: dict[str, dict[str, object]] = {
    STANDARD_PPR: {
        "name": "12-team PPR · 1 QB",
        "description": "Current model: one QB, one FLEX, and seven bench spots.",
        "starters": {
            "QB": 1,
            "RB": 2,
            "WR": 2,
            "TE": 1,
            "FLEX": 1,
            "K": 1,
            "DST": 1,
        },
        "position_caps": {"QB": 2, "RB": 5, "WR": 8, "TE": 2, "K": 1, "DST": 1},
        "bench_slots": 7,
    },
    SUPERFLEX_REPLACES_FLEX: {
        "name": "12-team PPR · Superflex",
        "description": "Superflex replaces the normal FLEX; seven bench spots.",
        "starters": {
            "QB": 1,
            "RB": 2,
            "WR": 2,
            "TE": 1,
            "SUPERFLEX": 1,
            "K": 1,
            "DST": 1,
        },
        "position_caps": {"QB": 3, "RB": 5, "WR": 7, "TE": 2, "K": 1, "DST": 1},
        "bench_slots": 7,
    },
    FLEX_AND_SUPERFLEX: {
        "name": "12-team PPR · FLEX + Superflex",
        "description": "Keeps the normal FLEX and adds Superflex; seven bench spots.",
        "starters": {
            "QB": 1,
            "RB": 2,
            "WR": 2,
            "TE": 1,
            "FLEX": 1,
            "SUPERFLEX": 1,
            "K": 1,
            "DST": 1,
        },
        "position_caps": {"QB": 3, "RB": 5, "WR": 8, "TE": 2, "K": 1, "DST": 1},
        "bench_slots": 7,
    },
}


@dataclass(frozen=True)
class LeagueConfig:
    profile_id: str = STANDARD_PPR
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

    @property
    def flex_slots(self) -> int:
        return int(self.starters.get("FLEX", 0))

    @property
    def superflex_slots(self) -> int:
        return int(self.starters.get("SUPERFLEX", 0))

    @property
    def is_superflex(self) -> bool:
        return self.superflex_slots > 0

    def position_starters(self, position: str) -> int:
        base = int(self.starters.get(position, 0))
        return base + self.superflex_slots if position == "QB" else base


def league_config_for_profile(
    profile_id: str,
    *,
    user_slot: int = 6,
    teams: int = 12,
) -> LeagueConfig:
    if profile_id not in LEAGUE_PROFILES:
        raise ValueError(f"unsupported league profile: {profile_id}")
    profile = LEAGUE_PROFILES[profile_id]
    starters = dict(profile["starters"])
    bench_slots = int(profile["bench_slots"])
    return LeagueConfig(
        profile_id=profile_id,
        teams=teams,
        user_slot=user_slot,
        roster_size=sum(int(value) for value in starters.values()) + bench_slots,
        starters=starters,
        position_caps=dict(profile["position_caps"]),
    )


def league_profiles_payload() -> list[dict[str, object]]:
    result = []
    for profile_id, profile in LEAGUE_PROFILES.items():
        config = league_config_for_profile(profile_id)
        result.append(
            {
                "id": profile_id,
                "name": profile["name"],
                "description": profile["description"],
                "roster_size": config.roster_size,
                "bench_slots": profile["bench_slots"],
                "starters": dict(config.starters),
                "position_caps": dict(config.position_caps),
            }
        )
    return result


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
