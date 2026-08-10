from __future__ import annotations

import csv
import io
import json
import math
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Player


CONSENSUS_URL = (
    "https://raw.githubusercontent.com/dynastyprocess/data/master/files/"
    "db_fpecr_latest.csv"
)
PLAYER_IDS_URL = (
    "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
)
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_TREND_URL = (
    "https://api.sleeper.app/v1/players/nfl/trending/{kind}"
    "?lookback_hours=24&limit=100"
)


@dataclass
class SignalRecord:
    name: str
    team: str
    position: str
    external_ids: dict[str, str] = field(default_factory=dict)
    values: dict[str, float] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalResult:
    records: list[SignalRecord]
    fetched_at: str
    cached: bool
    sources: dict[str, str]


def normalize_name(value: str) -> str:
    """Normalize common provider differences without fuzzy guessing."""
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9 ]", " ", plain.lower()).split()
    while words and words[-1] in {"jr", "sr", "ii", "iii", "iv", "v"}:
        words.pop()
    return " ".join(words)


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_id_crosswalk(content: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(content)):
        fantasypros = str(row.get("fantasypros_id") or "").strip()
        if not fantasypros or fantasypros.upper() == "NA":
            continue
        ids = {}
        for source, column in (
            ("espn", "espn_id"),
            ("sleeper", "sleeper_id"),
            ("gsis", "gsis_id"),
        ):
            value = str(row.get(column) or "").strip()
            if value and value.upper() != "NA":
                ids[source] = value
        result[fantasypros] = ids
    return result


def parse_consensus_csv(
    content: str, crosswalk: dict[str, dict[str, str]] | None = None
) -> list[SignalRecord]:
    crosswalk = crosswalk or {}
    records: list[SignalRecord] = []
    for row in csv.DictReader(io.StringIO(content)):
        if row.get("page_type") != "redraft-overall":
            continue
        name = str(row.get("player") or "").strip()
        position = str(row.get("pos") or "").upper().replace("D/ST", "DST")
        rank = _float(row.get("ecr"))
        if not name or position not in {"QB", "RB", "WR", "TE", "K", "DST"} or rank is None:
            continue
        values = {"consensus_rank": rank}
        for source, column in (
            ("consensus_sd", "sd"),
            ("consensus_best", "best"),
            ("consensus_worst", "worst"),
            ("rank_delta", "rank_delta"),
            ("bye_week", "bye"),
        ):
            value = _float(row.get(column))
            if value is not None:
                values[source] = value
        records.append(
            SignalRecord(
                name=name,
                team=str(row.get("team") or row.get("tm") or "FA").upper(),
                position=position,
                external_ids=dict(crosswalk.get(str(row.get("id") or "").strip(), {})),
                values=values,
                context={"consensus_date": str(row.get("scrape_date") or "")},
            )
        )
    if len(records) < 100:
        raise ValueError("consensus feed did not contain enough redraft players")
    return records


def merge_sleeper_data(
    records: list[SignalRecord],
    players_payload: dict[str, Any],
    adds: list[dict[str, Any]],
    drops: list[dict[str, Any]],
) -> None:
    by_sleeper = {
        record.external_ids["sleeper"]: record
        for record in records
        if record.external_ids.get("sleeper")
    }
    add_counts = {str(item.get("player_id")): _float(item.get("count")) or 0 for item in adds}
    drop_counts = {str(item.get("player_id")): _float(item.get("count")) or 0 for item in drops}
    for sleeper_id, record in by_sleeper.items():
        item = players_payload.get(sleeper_id)
        if not isinstance(item, dict):
            continue
        if item.get("espn_id") and not record.external_ids.get("espn"):
            record.external_ids["espn"] = str(item["espn_id"])
        depth = _float(item.get("depth_chart_order"))
        if depth is not None:
            record.values["depth_chart_order"] = depth
        for target, key in (
            ("years_exp", "years_exp"),
            ("age", "age"),
            ("news_updated_epoch", "news_updated"),
        ):
            value = _float(item.get(key))
            if value is not None:
                record.values[target] = value
        record.values["trend_adds_24h"] = add_counts.get(sleeper_id, 0)
        record.values["trend_drops_24h"] = drop_counts.get(sleeper_id, 0)
        for target, key in (
            ("injury_status", "injury_status"),
            ("injury_body_part", "injury_body_part"),
            ("practice", "practice_participation"),
            ("practice_description", "practice_description"),
            ("depth_chart_position", "depth_chart_position"),
            ("nfl_status", "status"),
        ):
            value = item.get(key)
            if value:
                record.context[target] = str(value)


def apply_signals(players: list[Player], records: list[SignalRecord]) -> tuple[list[Player], int]:
    by_espn = {
        record.external_ids["espn"]: record
        for record in records
        if record.external_ids.get("espn")
    }
    by_key: dict[tuple[str, str, str], list[SignalRecord]] = {}
    by_name_position: dict[tuple[str, str], list[SignalRecord]] = {}
    for record in records:
        key = (normalize_name(record.name), record.position, record.team)
        by_key.setdefault(key, []).append(record)
        by_name_position.setdefault(key[:2], []).append(record)

    enriched: list[Player] = []
    matched = 0
    for player in players:
        record = by_espn.get(player.external_ids.get("espn", ""))
        if record is None:
            exact = by_key.get((normalize_name(player.name), player.position, player.team), [])
            record = exact[0] if len(exact) == 1 else None
        if record is None:
            candidates = by_name_position.get((normalize_name(player.name), player.position), [])
            record = candidates[0] if len(candidates) == 1 else None
        if record is None:
            enriched.append(player)
            continue
        matched += 1
        enriched.append(
            replace(
                player,
                external_ids={**record.external_ids, **player.external_ids},
                signals={**player.signals, **record.values},
                context={**player.context, **record.context},
            )
        )
    return enriched, matched


class FreeSignalProvider:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or Path(".cache/signals")

    def _load(self, url: str, filename: str, refresh: bool, limit: int) -> tuple[str, bool]:
        path = self.cache_dir / filename
        if path.exists() and not refresh:
            return path.read_text(encoding="utf-8"), True
        request = urllib.request.Request(url, headers={"User-Agent": "espn-fantasy-draft-agent/0.2"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                if int(response.headers.get("Content-Length", "0")) > limit:
                    raise ValueError(f"{filename} exceeded its download safety limit")
                payload = response.read(limit + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError(f"could not load {filename}: {exc}") from exc
        if len(payload) > limit:
            raise ValueError(f"{filename} exceeded its download safety limit")
        content = payload.decode("utf-8-sig")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return content, False

    def load(self, refresh: bool = False) -> SignalResult:
        ids, ids_cached = self._load(PLAYER_IDS_URL, "player_ids.csv", refresh, 5_000_000)
        consensus, consensus_cached = self._load(
            CONSENSUS_URL, "consensus.csv", refresh, 3_000_000
        )
        sleeper, sleeper_cached = self._load(
            SLEEPER_PLAYERS_URL, "sleeper_players.json", refresh, 20_000_000
        )
        adds, adds_cached = self._load(
            SLEEPER_TREND_URL.format(kind="add"), "trending_add.json", refresh, 500_000
        )
        drops, drops_cached = self._load(
            SLEEPER_TREND_URL.format(kind="drop"), "trending_drop.json", refresh, 500_000
        )
        records = parse_consensus_csv(consensus, parse_id_crosswalk(ids))
        merge_sleeper_data(records, json.loads(sleeper), json.loads(adds), json.loads(drops))
        return SignalResult(
            records=records,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            cached=all((ids_cached, consensus_cached, sleeper_cached, adds_cached, drops_cached)),
            sources={
                "consensus": "FantasyPros ECR via DynastyProcess open data",
                "identity": "DynastyProcess player ID crosswalk",
                "availability": "Sleeper player, injury, depth chart, and trend API",
            },
        )
