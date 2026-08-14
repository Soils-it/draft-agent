from __future__ import annotations

import csv
import io
import json
import math
import os
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .data import demo_players
from .models import Player
from .scoring import projected_points


NFLVERSE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_reg_{season}.csv"
)
NFLVERSE_PLAYERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
)
NFLVERSE_CACHE_LIMIT = 15_000_000
NFLVERSE_SCHEDULES_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "schedules/games.csv"
)
NFLVERSE_SCHEDULES_TIMESTAMP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "schedules/timestamp.json"
)
NFLVERSE_SCHEDULES_LIMIT = 25_000_000
NFLVERSE_TIMESTAMP_LIMIT = 64_000
VEGAS_CACHE_LIMIT = 1_000_000
VEGAS_MAX_AGE_HOURS = 48
VEGAS_CACHE_SCHEMA = 1
VEGAS_ATTRIBUTION = "nflverse (nflverse-data)"
VEGAS_LICENSE = "CC BY 4.0"

CANONICAL_NFL_TEAMS = (
    "ARI",
    "ATL",
    "BAL",
    "BUF",
    "CAR",
    "CHI",
    "CIN",
    "CLE",
    "DAL",
    "DEN",
    "DET",
    "GB",
    "HOU",
    "IND",
    "JAX",
    "KC",
    "LV",
    "LAC",
    "LAR",
    "MIA",
    "MIN",
    "NE",
    "NO",
    "NYG",
    "NYJ",
    "PHI",
    "PIT",
    "SEA",
    "SF",
    "TB",
    "TEN",
    "WSH",
)
_CANONICAL_NFL_TEAM_SET = frozenset(CANONICAL_NFL_TEAMS)
_NFL_TEAM_ALIASES = {
    "JAC": "JAX",
    "JAX": "JAX",
    "LA": "LAR",
    "LAR": "LAR",
    "STL": "LAR",
    "WAS": "WSH",
    "WSH": "WSH",
    "OAK": "LV",
    "LV": "LV",
    "SD": "LAC",
    "LAC": "LAC",
}
VEGAS_SIGNAL_KEYS = frozenset(
    {
        "vegas_implied_points",
        "vegas_opponent_implied_points",
        "vegas_games",
        "vegas_league_implied_points",
        "vegas_league_opponent_implied_points",
    }
)


@dataclass(frozen=True)
class ProviderResult:
    players: list[Player]
    source: str
    season: int
    fetched_at: str
    cached: bool
    mapped_espn_ids: int


@dataclass(frozen=True)
class VegasTeamTotal:
    team: str
    games: int
    implied_points: float
    opponent_implied_points: float

    def as_dict(self) -> dict[str, object]:
        return {
            "team": self.team,
            "games": self.games,
            "implied_points": round(self.implied_points, 3),
            "opponent_implied_points": round(self.opponent_implied_points, 3),
        }


@dataclass(frozen=True)
class ParsedVegasSchedule:
    teams: dict[str, VegasTeamTotal]
    season: int
    coverage: int
    lined_games: int


@dataclass(frozen=True)
class VegasSnapshot:
    teams: dict[str, VegasTeamTotal]
    source_url: str
    timestamp_url: str
    attribution: str
    license: str
    fetched_at: str
    dataset_timestamp: str
    season: int
    coverage: int
    lined_games: int

    def is_fresh(self, now: datetime | None = None) -> bool:
        current = _utc_datetime(now or datetime.now(timezone.utc), "current time")
        fetched = _parse_iso_datetime(self.fetched_at, "Vegas fetch timestamp")
        age = current - fetched
        return -timedelta(minutes=5) <= age <= timedelta(hours=VEGAS_MAX_AGE_HOURS)

    def as_cache_payload(self) -> dict[str, object]:
        return {
            "schema_version": VEGAS_CACHE_SCHEMA,
            "metadata": {
                "source_url": self.source_url,
                "timestamp_url": self.timestamp_url,
                "attribution": self.attribution,
                "license": self.license,
                "fetched_at": self.fetched_at,
                "dataset_timestamp": self.dataset_timestamp,
                "season": self.season,
                "coverage": self.coverage,
                "lined_games": self.lined_games,
            },
            "teams": {
                team: total.as_dict() for team, total in sorted(self.teams.items())
            },
        }


@dataclass(frozen=True)
class VegasProviderResult:
    snapshot: VegasSnapshot | None
    status: str
    cached: bool
    error: str | None = None

    def usable(self, now: datetime | None = None) -> bool:
        return bool(
            self.snapshot
            and self.snapshot.coverage == len(CANONICAL_NFL_TEAMS)
            and self.snapshot.is_fresh(now)
        )


def canonical_nfl_team(value: object) -> str | None:
    """Return an exact canonical NFL abbreviation without fuzzy guessing."""
    team = str(value or "").strip().upper()
    team = _NFL_TEAM_ALIASES.get(team, team)
    return team if team in _CANONICAL_NFL_TEAM_SET else None


def _utc_datetime(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc


def _parse_iso_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    return _utc_datetime(parsed, label)


def parse_nflverse_dataset_timestamp(content: str) -> str:
    if len(content.encode("utf-8")) > NFLVERSE_TIMESTAMP_LIMIT:
        raise ValueError("nflverse schedules timestamp exceeded its safety limit")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("nflverse schedules timestamp was not valid JSON") from exc
    candidate: object = payload
    if isinstance(payload, dict):
        candidate = next(
            (
                payload[key]
                for key in ("timestamp", "last_updated", "updated_at", "published_at")
                if key in payload
            ),
            None,
        )
    parsed = _parse_iso_datetime(candidate, "nflverse schedules dataset timestamp")
    return parsed.isoformat()


def _nfl_season_for_date(as_of: date) -> int:
    # January and February belong to the NFL season that began the prior year.
    return as_of.year - 1 if as_of.month <= 2 else as_of.year


def _schedule_number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _missing_schedule_line(value: object) -> bool:
    return str(value or "").strip().upper() in {"", "NA", "N/A", "NULL"}


def parse_nflverse_schedules_csv(
    content: str,
    *,
    as_of: date | None = None,
) -> ParsedVegasSchedule:
    """Validate and aggregate future regular-season nflverse game lines."""
    if len(content.encode("utf-8")) > NFLVERSE_SCHEDULES_LIMIT:
        raise ValueError("nflverse schedules response exceeded its safety limit")
    as_of = as_of or datetime.now(timezone.utc).date()
    season = _nfl_season_for_date(as_of)
    reader = csv.DictReader(io.StringIO(content))
    required = {
        "game_id",
        "season",
        "game_type",
        "gameday",
        "home_team",
        "away_team",
        "spread_line",
        "total_line",
    }
    fields = set(reader.fieldnames or [])
    missing_columns = sorted(required - fields)
    if missing_columns:
        raise ValueError(
            "nflverse schedules missing required columns: " + ", ".join(missing_columns)
        )

    seen_games: set[str] = set()
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    lined_games = 0
    valid_game_types = {"PRE", "REG", "POST", "WC", "DIV", "CON", "SB"}
    for row_number, row in enumerate(reader, 2):
        try:
            row_season = int(str(row.get("season") or "").strip())
        except ValueError as exc:
            raise ValueError(f"nflverse schedules row {row_number} has an invalid season") from exc
        if row_season != season:
            continue

        game_id = str(row.get("game_id") or "").strip()
        if not game_id:
            raise ValueError(f"nflverse schedules row {row_number} is missing game_id")
        if game_id in seen_games:
            raise ValueError(f"nflverse schedules contains duplicate game {game_id}")
        seen_games.add(game_id)

        game_type = str(row.get("game_type") or "").strip().upper()
        if game_type not in valid_game_types:
            raise ValueError(
                f"nflverse schedules row {row_number} has invalid game_type {game_type or '(blank)'}"
            )
        try:
            gameday = date.fromisoformat(str(row.get("gameday") or "").strip())
        except ValueError as exc:
            raise ValueError(f"nflverse schedules row {row_number} has an invalid gameday") from exc

        home = canonical_nfl_team(row.get("home_team"))
        away = canonical_nfl_team(row.get("away_team"))
        if home is None or away is None:
            raise ValueError(f"nflverse schedules row {row_number} has an unknown NFL team")
        if home == away:
            raise ValueError(f"nflverse schedules row {row_number} repeats the same NFL team")
        if game_type != "REG" or gameday < as_of:
            continue

        raw_spread = row.get("spread_line")
        raw_total = row.get("total_line")
        if _missing_schedule_line(raw_spread) or _missing_schedule_line(raw_total):
            continue
        spread = _schedule_number(raw_spread, f"spread_line on row {row_number}")
        total = _schedule_number(raw_total, f"total_line on row {row_number}")
        if not -40 <= spread <= 40:
            raise ValueError(f"spread_line on row {row_number} is outside the plausible range")
        if not 20 <= total <= 100:
            raise ValueError(f"total_line on row {row_number} is outside the plausible range")
        home_implied = (total + spread) / 2
        away_implied = (total - spread) / 2
        if not 0 <= home_implied <= 80 or not 0 <= away_implied <= 80:
            raise ValueError(f"implied points on row {row_number} are outside the plausible range")

        totals[home][0] += home_implied
        totals[home][1] += away_implied
        totals[home][2] += 1
        totals[away][0] += away_implied
        totals[away][1] += home_implied
        totals[away][2] += 1
        lined_games += 1

    missing_teams = sorted(_CANONICAL_NFL_TEAM_SET - totals.keys())
    if missing_teams:
        raise ValueError(
            "nflverse schedules lacks lined-game coverage for all 32 teams; missing: "
            + ", ".join(missing_teams)
        )
    teams = {
        team: VegasTeamTotal(
            team=team,
            games=int(values[2]),
            implied_points=values[0] / values[2],
            opponent_implied_points=values[1] / values[2],
        )
        for team, values in totals.items()
    }
    return ParsedVegasSchedule(
        teams=teams,
        season=season,
        coverage=len(teams),
        lined_games=lined_games,
    )


def apply_vegas_context(
    players: list[Player],
    snapshot: VegasSnapshot | None,
) -> tuple[list[Player], int]:
    """Replace Vegas fields using only an explicitly usable team snapshot."""
    own_center = (
        sum(item.implied_points for item in snapshot.teams.values()) / snapshot.coverage
        if snapshot and snapshot.coverage
        else 0.0
    )
    opponent_center = (
        sum(item.opponent_implied_points for item in snapshot.teams.values())
        / snapshot.coverage
        if snapshot and snapshot.coverage
        else 0.0
    )
    enriched: list[Player] = []
    matched = 0
    for player in players:
        signals = {
            key: value
            for key, value in player.signals.items()
            if key not in VEGAS_SIGNAL_KEYS
        }
        team = canonical_nfl_team(player.team)
        total = snapshot.teams.get(team) if snapshot and team else None
        if total is not None:
            matched += 1
            signals.update(
                {
                    "vegas_implied_points": total.implied_points,
                    "vegas_opponent_implied_points": total.opponent_implied_points,
                    "vegas_games": float(total.games),
                    "vegas_league_implied_points": own_center,
                    "vegas_league_opponent_implied_points": opponent_center,
                }
            )
        enriched.append(replace(player, signals=signals))
    return enriched, matched


class NflverseVegasProvider:
    """Offline-first, atomic cache for nflverse schedule-derived team context."""

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or Path(".cache/vegas")
        self.cache_path = self.cache_dir / "snapshot.json"

    @staticmethod
    def _download(url: str, limit: int) -> str:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "espn-fantasy-draft-agent/0.2"},
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw_length = response.headers.get("Content-Length", "0")
                try:
                    declared_length = int(raw_length or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("nflverse response had an invalid Content-Length") from exc
                if declared_length < 0 or declared_length > limit:
                    raise ValueError("nflverse response exceeded its download safety limit")
                payload = response.read(limit + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError(f"could not load nflverse Vegas data: {exc}") from exc
        if len(payload) > limit:
            raise ValueError("nflverse response exceeded its download safety limit")
        try:
            return payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("nflverse response was not valid UTF-8") from exc

    @staticmethod
    def _snapshot_from_payload(payload: object) -> VegasSnapshot:
        if not isinstance(payload, dict) or payload.get("schema_version") != VEGAS_CACHE_SCHEMA:
            raise ValueError("cached Vegas snapshot has an unsupported schema")
        metadata = payload.get("metadata")
        raw_teams = payload.get("teams")
        if not isinstance(metadata, dict) or not isinstance(raw_teams, dict):
            raise ValueError("cached Vegas snapshot is incomplete")
        if metadata.get("source_url") != NFLVERSE_SCHEDULES_URL:
            raise ValueError("cached Vegas snapshot has an unexpected source URL")
        if metadata.get("timestamp_url") != NFLVERSE_SCHEDULES_TIMESTAMP_URL:
            raise ValueError("cached Vegas snapshot has an unexpected timestamp URL")
        if metadata.get("attribution") != VEGAS_ATTRIBUTION or metadata.get("license") != VEGAS_LICENSE:
            raise ValueError("cached Vegas snapshot has invalid attribution metadata")
        fetched = _parse_iso_datetime(metadata.get("fetched_at"), "cached Vegas fetch timestamp")
        dataset = _parse_iso_datetime(
            metadata.get("dataset_timestamp"), "cached Vegas dataset timestamp"
        )
        try:
            season = int(metadata.get("season"))
            coverage = int(metadata.get("coverage"))
            lined_games = int(metadata.get("lined_games"))
        except (TypeError, ValueError) as exc:
            raise ValueError("cached Vegas snapshot metadata is invalid") from exc
        if not 1900 <= season <= datetime.now(timezone.utc).year + 1:
            raise ValueError("cached Vegas snapshot season is outside the supported range")
        if coverage != len(CANONICAL_NFL_TEAMS) or lined_games <= 0:
            raise ValueError("cached Vegas snapshot coverage is incomplete")
        if set(raw_teams) != _CANONICAL_NFL_TEAM_SET:
            raise ValueError("cached Vegas snapshot does not contain all 32 canonical teams")

        teams: dict[str, VegasTeamTotal] = {}
        for team, item in raw_teams.items():
            if not isinstance(item, dict) or item.get("team") != team:
                raise ValueError(f"cached Vegas team entry {team} is invalid")
            try:
                games = int(item.get("games"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"cached Vegas team entry {team} has invalid games") from exc
            implied = _schedule_number(
                item.get("implied_points"), f"cached Vegas implied points for {team}"
            )
            opponent = _schedule_number(
                item.get("opponent_implied_points"),
                f"cached Vegas opponent implied points for {team}",
            )
            if not 1 <= games <= 25 or not 0 <= implied <= 80 or not 0 <= opponent <= 80:
                raise ValueError(f"cached Vegas team entry {team} is outside plausible ranges")
            teams[team] = VegasTeamTotal(team, games, implied, opponent)
        if sum(item.games for item in teams.values()) != lined_games * 2:
            raise ValueError("cached Vegas lined-game count is inconsistent")
        return VegasSnapshot(
            teams=teams,
            source_url=NFLVERSE_SCHEDULES_URL,
            timestamp_url=NFLVERSE_SCHEDULES_TIMESTAMP_URL,
            attribution=VEGAS_ATTRIBUTION,
            license=VEGAS_LICENSE,
            fetched_at=fetched.isoformat(),
            dataset_timestamp=dataset.isoformat(),
            season=season,
            coverage=coverage,
            lined_games=lined_games,
        )

    def _write_snapshot(self, snapshot: VegasSnapshot) -> None:
        # Serialize and validate before touching the last-known-good cache.
        content = json.dumps(snapshot.as_cache_payload(), indent=2, sort_keys=True)
        self._snapshot_from_payload(json.loads(content))
        if len(content.encode("utf-8")) > VEGAS_CACHE_LIMIT:
            raise ValueError("Vegas snapshot exceeded its cache safety limit")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.cache_dir,
                prefix="snapshot-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(self.cache_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def load_cached(self, *, now: datetime | None = None) -> VegasProviderResult | None:
        """Restore a validated snapshot without performing network access."""
        if not self.cache_path.is_file():
            return None
        try:
            if self.cache_path.stat().st_size > VEGAS_CACHE_LIMIT:
                raise ValueError("cached Vegas snapshot exceeded its safety limit")
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("cached Vegas snapshot was not valid JSON") from exc
        snapshot = self._snapshot_from_payload(payload)
        fresh = snapshot.is_fresh(now)
        return VegasProviderResult(
            snapshot=snapshot,
            status="cached" if fresh else "stale",
            cached=True,
            error=None,
        )

    def refresh(
        self,
        *,
        as_of: date | None = None,
        now: datetime | None = None,
    ) -> VegasProviderResult:
        current = _utc_datetime(now or datetime.now(timezone.utc), "current time")
        try:
            schedules = self._download(NFLVERSE_SCHEDULES_URL, NFLVERSE_SCHEDULES_LIMIT)
            timestamp = self._download(
                NFLVERSE_SCHEDULES_TIMESTAMP_URL, NFLVERSE_TIMESTAMP_LIMIT
            )
            dataset_timestamp = parse_nflverse_dataset_timestamp(timestamp)
            parsed = parse_nflverse_schedules_csv(
                schedules,
                as_of=as_of or current.date(),
            )
            snapshot = VegasSnapshot(
                teams=parsed.teams,
                source_url=NFLVERSE_SCHEDULES_URL,
                timestamp_url=NFLVERSE_SCHEDULES_TIMESTAMP_URL,
                attribution=VEGAS_ATTRIBUTION,
                license=VEGAS_LICENSE,
                fetched_at=current.isoformat(),
                dataset_timestamp=dataset_timestamp,
                season=parsed.season,
                coverage=parsed.coverage,
                lined_games=parsed.lined_games,
            )
            self._write_snapshot(snapshot)
            return VegasProviderResult(snapshot, "refreshed", False)
        except Exception as exc:
            error = f"Vegas refresh failed: {exc}"
            try:
                cached = self.load_cached(now=current)
            except Exception:
                cached = None
            if cached is None:
                return VegasProviderResult(None, "unavailable", False, error)
            if cached.usable(current):
                return replace(cached, status="fallback", error=error)
            return replace(cached, status="stale", error=error)


def _number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def espn_ids_from_players_csv(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(content)):
        gsis_id = row.get("gsis_id") or ""
        espn_id = row.get("espn_id") or ""
        if gsis_id and espn_id:
            result[gsis_id] = espn_id
    return result


def players_from_nflverse_csv(
    content: str, season: int, espn_ids: dict[str, str] | None = None
) -> list[Player]:
    """Create a conservative baseline from prior regular-season totals.

    This is intentionally not labeled as a current-season expert projection.
    Per-game production is extended to 17 games, capped, and regressed by 8%.
    """
    rows = list(csv.DictReader(io.StringIO(content)))
    espn_ids = espn_ids or {}
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
                external_ids={"espn": espn_ids[player_id]} if player_id in espn_ids else {},
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

    def _load_url(self, url: str, cache_file: Path, refresh: bool) -> tuple[str, bool]:
        cached = cache_file.exists() and not refresh
        if cached:
            try:
                content = self._read_cached(cache_file)
            except OSError as exc:
                raise ValueError("could not read cached nflverse data") from exc
        else:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "espn-fantasy-draft-agent/0.1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    if int(response.headers.get("Content-Length", "0")) > NFLVERSE_CACHE_LIMIT:
                        raise ValueError("nflverse response exceeded the 15 MB safety limit")
                    payload = response.read(NFLVERSE_CACHE_LIMIT + 1)
            except (OSError, urllib.error.URLError) as exc:
                raise ValueError(f"could not load nflverse data: {exc}") from exc
            if len(payload) > NFLVERSE_CACHE_LIMIT:
                raise ValueError("nflverse response exceeded the 15 MB safety limit")
            content = payload.decode("utf-8-sig")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(content, encoding="utf-8")
        return content, cached

    @staticmethod
    def _fetched_at(paths: list[Path]) -> str:
        oldest = min(path.stat().st_mtime for path in paths)
        return datetime.fromtimestamp(oldest, timezone.utc).isoformat()

    @staticmethod
    def _read_cached(path: Path) -> str:
        if path.stat().st_size > NFLVERSE_CACHE_LIMIT:
            raise ValueError("cached nflverse data exceeded the 15 MB safety limit")
        return path.read_text(encoding="utf-8")

    def _result(
        self,
        stats_content: str,
        players_content: str,
        season: int,
        cached: bool,
        paths: list[Path],
    ) -> ProviderResult:
        espn_ids = espn_ids_from_players_csv(players_content)
        players = players_from_nflverse_csv(stats_content, season, espn_ids)
        return ProviderResult(
            players=players,
            source="nflverse historical baseline",
            season=season,
            fetched_at=self._fetched_at(paths),
            cached=cached,
            mapped_espn_ids=sum(bool(player.external_ids.get("espn")) for player in players),
        )

    def load_cached(self, season: int | None = None) -> ProviderResult | None:
        """Restore a complete validated cache without ever attempting the network."""
        players_path = self.cache_dir / "players.csv"
        if not players_path.is_file():
            return None
        if season is None:
            seasons = []
            for path in self.cache_dir.glob("stats_player_reg_*.csv"):
                try:
                    candidate = int(path.stem.removeprefix("stats_player_reg_"))
                except ValueError:
                    continue
                if 1999 <= candidate <= datetime.now(timezone.utc).year:
                    seasons.append(candidate)
            if not seasons:
                return None
            season = max(seasons)
        if not 1999 <= season <= datetime.now(timezone.utc).year:
            raise ValueError("cached nflverse season is outside the supported range")
        stats_path = self.cache_dir / f"stats_player_reg_{season}.csv"
        if not stats_path.is_file():
            return None
        paths = [stats_path, players_path]
        return self._result(
            self._read_cached(stats_path),
            self._read_cached(players_path),
            season,
            True,
            paths,
        )

    def load(self, season: int, refresh: bool = False) -> ProviderResult:
        stats_path = self.cache_dir / f"stats_player_reg_{season}.csv"
        players_path = self.cache_dir / "players.csv"
        stats_content, stats_cached = self._load_url(
            NFLVERSE_URL.format(season=season),
            stats_path,
            refresh,
        )
        players_content, players_cached = self._load_url(
            NFLVERSE_PLAYERS_URL,
            players_path,
            refresh,
        )
        return self._result(
            stats_content,
            players_content,
            season,
            stats_cached and players_cached,
            [stats_path, players_path],
        )
