from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .data import demo_players
from .models import Player
from .scoring import projected_points


NFLVERSE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_reg_{season}.csv"
)


@dataclass(frozen=True)
class ProviderResult:
    players: list[Player]
    source: str
    season: int
    fetched_at: str
    cached: bool


def _number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def players_from_nflverse_csv(content: str, season: int) -> list[Player]:
    """Create a conservative baseline from prior regular-season totals.

    This is intentionally not labeled as a current-season expert projection.
    Per-game production is extended to 17 games, capped, and regressed by 8%.
    """
    rows = list(csv.DictReader(io.StringIO(content)))
    candidates: list[Player] = []
    for row in rows:
        position = (row.get("position") or "").upper()
        if position not in {"QB", "RB", "WR", "TE", "K"}:
            continue
        games = _number(row, "games")
        if games < 4:
            continue
        scale = min(17 / games, 1.5) * 0.92
        stats = {
            "passing_yards": _number(row, "passing_yards") * scale,
            "passing_tds": _number(row, "passing_tds") * scale,
            "interceptions": _number(row, "passing_interceptions") * scale,
            "passing_2pt": _number(row, "passing_2pt_conversions") * scale,
            "rushing_yards": _number(row, "rushing_yards") * scale,
            "rushing_tds": _number(row, "rushing_tds") * scale,
            "rushing_2pt": _number(row, "rushing_2pt_conversions") * scale,
            "receptions": _number(row, "receptions") * scale,
            "receiving_yards": _number(row, "receiving_yards") * scale,
            "receiving_tds": _number(row, "receiving_tds") * scale,
            "receiving_2pt": _number(row, "receiving_2pt_conversions") * scale,
            "pat_made": _number(row, "pat_made") * scale,
            "fg_missed": _number(row, "fg_missed") * scale,
            "fg_0_39": (
                _number(row, "fg_made_0_19")
                + _number(row, "fg_made_20_29")
                + _number(row, "fg_made_30_39")
            )
            * scale,
            "fg_40_49": _number(row, "fg_made_40_49") * scale,
            "fg_50_59": _number(row, "fg_made_50_59") * scale,
            "fg_60_plus": _number(row, "fg_made_60_") * scale,
        }
        player_id = row.get("player_id") or ""
        name = row.get("player_display_name") or row.get("player_name") or ""
        if not player_id or not name:
            continue
        candidates.append(
            Player(
                player_id=f"nflverse-{player_id}",
                name=name,
                team=row.get("recent_team") or "FA",
                position=position,
                adp=999,
                stats=stats,
                upside=0.5,
                risk=min(0.8, 0.12 + max(0, 17 - games) / 17 * 0.6),
                status=f"{season} BASELINE",
            )
        )

    by_position: dict[str, list[Player]] = defaultdict(list)
    for player in candidates:
        by_position[player.position].append(player)
    limits = {"QB": 36, "RB": 84, "WR": 100, "TE": 40, "K": 24}
    offsets = {"RB": 0, "WR": 4, "QB": 20, "TE": 35, "K": 145}
    steps = {"RB": 2.0, "WR": 1.75, "QB": 3.8, "TE": 3.8, "K": 3.8}
    ranked: list[Player] = []
    for position, group in by_position.items():
        group.sort(key=projected_points, reverse=True)
        for rank, player in enumerate(group[: limits[position]], 1):
            ranked.append(
                Player(
                    **{
                        **player.__dict__,
                        "adp": offsets[position] + rank * steps[position],
                    }
                )
            )
    # nflverse's player file does not contain team-level D/ST projections yet.
    ranked.extend(player for player in demo_players() if player.position == "DST")
    if len(ranked) < 150:
        raise ValueError("nflverse data did not contain enough draftable players")
    return ranked


class NflverseProvider:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or Path(".cache/nflverse")

    def load(self, season: int, refresh: bool = False) -> ProviderResult:
        cache_file = self.cache_dir / f"stats_player_reg_{season}.csv"
        cached = cache_file.exists() and not refresh
        if cached:
            content = cache_file.read_text(encoding="utf-8")
        else:
            request = urllib.request.Request(
                NFLVERSE_URL.format(season=season),
                headers={"User-Agent": "espn-fantasy-draft-agent/0.1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    if int(response.headers.get("Content-Length", "0")) > 15_000_000:
                        raise ValueError("nflverse response exceeded the 15 MB safety limit")
                    payload = response.read(15_000_001)
            except (OSError, urllib.error.URLError) as exc:
                raise ValueError(f"could not load nflverse data: {exc}") from exc
            if len(payload) > 15_000_000:
                raise ValueError("nflverse response exceeded the 15 MB safety limit")
            content = payload.decode("utf-8-sig")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(content, encoding="utf-8")
        return ProviderResult(
            players=players_from_nflverse_csv(content, season),
            source="nflverse historical baseline",
            season=season,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            cached=cached,
        )
