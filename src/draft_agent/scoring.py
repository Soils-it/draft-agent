from __future__ import annotations

from .config import POINTS_ALLOWED_BONUS, SCORING_RATES, YARDS_ALLOWED_BONUS
from .models import Player


def _range_score(value: float, ranges: tuple[tuple[float, float, float], ...]) -> float:
    # Actual weekly points/yards allowed are whole numbers. Projection feeds often
    # provide fractional per-game averages, so normalize them before selecting an
    # ESPN scoring bucket and avoid artificial gaps such as 27.1 to 27.9.
    value = round(value)
    for lower, upper, score in ranges:
        if lower <= value <= upper:
            return score
    raise ValueError(f"value {value} was not covered by scoring ranges")


def projected_points(player: Player) -> float:
    """Calculate projected points from season-long stat projections."""
    if player.projected_points_override is not None:
        return round(player.projected_points_override, 3)
    points = sum(player.stats.get(key, 0.0) * rate for key, rate in SCORING_RATES.items())
    if player.position == "DST":
        games = max(player.stats.get("games", 17), 1)
        points += games * _range_score(
            player.stats.get("points_allowed_per_game", 24), POINTS_ALLOWED_BONUS
        )
        points += games * _range_score(
            player.stats.get("yards_allowed_per_game", 325), YARDS_ALLOWED_BONUS
        )
    return round(points, 3)
