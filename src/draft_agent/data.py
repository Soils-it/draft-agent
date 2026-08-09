from __future__ import annotations

from .models import Player


def demo_players() -> list[Player]:
    """Return deterministic, explicitly fake projections for safe local testing."""
    players: list[Player] = []
    specs = {"QB": 36, "RB": 84, "WR": 100, "TE": 40, "K": 24, "DST": 32}
    position_offset = {"RB": 0, "WR": 4, "QB": 20, "TE": 35, "K": 145, "DST": 150}
    for position, count in specs.items():
        for rank in range(1, count + 1):
            adp = position_offset[position] + rank * ({"RB": 2.0, "WR": 1.75}.get(position, 3.8))
            decay = max(0.35, 1 - (rank - 1) / (count * 1.25))
            stats: dict[str, float]
            if position == "QB":
                stats = {
                    "passing_yards": 4300 * decay,
                    "passing_tds": 32 * decay,
                    "interceptions": 9 + rank * 0.12,
                    "rushing_yards": (620 if rank % 4 == 1 else 220) * decay,
                    "rushing_tds": (6 if rank % 4 == 1 else 2) * decay,
                }
            elif position == "RB":
                stats = {
                    "rushing_yards": 1250 * decay,
                    "rushing_tds": 10 * decay,
                    "receptions": 58 * decay,
                    "receiving_yards": 440 * decay,
                    "receiving_tds": 2.5 * decay,
                }
            elif position == "WR":
                stats = {
                    "receptions": 102 * decay,
                    "receiving_yards": 1320 * decay,
                    "receiving_tds": 9 * decay,
                }
            elif position == "TE":
                stats = {
                    "receptions": 85 * decay,
                    "receiving_yards": 940 * decay,
                    "receiving_tds": 7 * decay,
                }
            elif position == "K":
                stats = {
                    "pat_made": 38 * decay,
                    "fg_missed": 3,
                    "fg_0_39": 16 * decay,
                    "fg_40_49": 9 * decay,
                    "fg_50_59": 4 * decay,
                    "fg_60_plus": 0.5 * decay,
                }
            else:
                stats = {
                    "games": 17,
                    "sacks": 44 * decay,
                    "defensive_interceptions": 13 * decay,
                    "fumble_recoveries": 9 * decay,
                    "safeties": 1,
                    "blocks": 2,
                    "interception_return_tds": 1.5 * decay,
                    "fumble_return_tds": 1 * decay,
                    "points_allowed_per_game": 18 + rank * 0.48,
                    "yards_allowed_per_game": 285 + rank * 4.2,
                }
            players.append(
                Player(
                    player_id=f"demo-{position.lower()}-{rank:03d}",
                    name=f"Demo {position} {rank:02d}",
                    team=f"T{((rank - 1) % 32) + 1:02d}",
                    position=position,
                    adp=adp,
                    stats=stats,
                    upside=min(1.0, 0.42 + (rank % 7) * 0.075),
                    risk=min(0.85, 0.1 + (rank % 6) * 0.09),
                )
            )
    return players
