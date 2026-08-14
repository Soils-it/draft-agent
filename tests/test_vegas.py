from __future__ import annotations

import csv
import io
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from draft_agent.config import (
    FLEX_AND_SUPERFLEX,
    STANDARD_PPR,
    SUPERFLEX_REPLACES_FLEX,
    league_config_for_profile,
)
from draft_agent.engine import DraftEngine, StrategyWeights
from draft_agent.espn import EspnDraftBridge
from draft_agent.models import Player
from draft_agent.providers import (
    CANONICAL_NFL_TEAMS,
    NFLVERSE_SCHEDULES_LIMIT,
    NFLVERSE_SCHEDULES_TIMESTAMP_URL,
    NFLVERSE_SCHEDULES_URL,
    VEGAS_ATTRIBUTION,
    VEGAS_LICENSE,
    NflverseVegasProvider,
    VegasSnapshot,
    apply_vegas_context,
    parse_nflverse_dataset_timestamp,
    parse_nflverse_schedules_csv,
)
from draft_agent.signals import SignalRecord


SCHEDULE_FIELDS = (
    "game_id",
    "season",
    "game_type",
    "gameday",
    "home_team",
    "away_team",
    "spread_line",
    "total_line",
)
AS_OF = date(2026, 8, 1)
NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def schedule_rows() -> list[dict[str, object]]:
    rows = []
    aliases = {"LAR": "LA", "WSH": "WAS", "JAX": "JAC"}
    for index in range(0, len(CANONICAL_NFL_TEAMS), 2):
        home = CANONICAL_NFL_TEAMS[index]
        away = CANONICAL_NFL_TEAMS[index + 1]
        rows.append(
            {
                "game_id": f"2026_{index // 2 + 1:02d}",
                "season": 2026,
                "game_type": "REG",
                "gameday": f"2026-09-{index // 2 + 1:02d}",
                "home_team": aliases.get(home, home),
                "away_team": aliases.get(away, away),
                "spread_line": 3 if index == 0 else (-4 if index == 2 else 0),
                "total_line": 47 if index == 0 else 44,
            }
        )
    return rows


def schedules_csv(
    rows: list[dict[str, object]] | None = None,
    fields: tuple[str, ...] = SCHEDULE_FIELDS,
) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows if rows is not None else schedule_rows())
    return output.getvalue()


class FakeResponse:
    def __init__(self, content: str, content_length: int | None = None):
        self.payload = content.encode("utf-8")
        self.headers = {
            "Content-Length": str(
                len(self.payload) if content_length is None else content_length
            )
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


def valid_snapshot(fetched_at: datetime = NOW) -> VegasSnapshot:
    parsed = parse_nflverse_schedules_csv(schedules_csv(), as_of=AS_OF)
    return VegasSnapshot(
        teams=parsed.teams,
        source_url=NFLVERSE_SCHEDULES_URL,
        timestamp_url=NFLVERSE_SCHEDULES_TIMESTAMP_URL,
        attribution=VEGAS_ATTRIBUTION,
        license=VEGAS_LICENSE,
        fetched_at=fetched_at.isoformat(),
        dataset_timestamp="2026-08-01T10:00:00+00:00",
        season=parsed.season,
        coverage=parsed.coverage,
        lined_games=parsed.lined_games,
    )


class VegasScheduleParsingTests(unittest.TestCase):
    def test_home_favorite_and_underdog_formulas_aliases_and_coverage(self):
        parsed = parse_nflverse_schedules_csv(schedules_csv(), as_of=AS_OF)

        self.assertEqual(parsed.coverage, 32)
        self.assertEqual(set(parsed.teams), set(CANONICAL_NFL_TEAMS))
        self.assertEqual(parsed.teams["ARI"].implied_points, 25)
        self.assertEqual(parsed.teams["ATL"].implied_points, 22)
        self.assertEqual(parsed.teams["BAL"].implied_points, 20)
        self.assertEqual(parsed.teams["BUF"].implied_points, 24)
        self.assertIn("LAR", parsed.teams)
        self.assertIn("WSH", parsed.teams)
        self.assertIn("JAX", parsed.teams)

    def test_filters_past_non_regular_and_other_season_and_aggregates_games(self):
        rows = schedule_rows()
        rows.extend(
            [
                {
                    **rows[0],
                    "game_id": "past",
                    "gameday": "2026-07-31",
                    "spread_line": 20,
                    "total_line": 80,
                },
                {
                    **rows[0],
                    "game_id": "post",
                    "game_type": "POST",
                    "gameday": "2026-12-30",
                },
                {
                    **rows[0],
                    "game_id": "other-season",
                    "season": 2025,
                    "gameday": "2026-09-01",
                },
                {
                    **rows[0],
                    "game_id": "missing-line",
                    "gameday": "2026-10-01",
                    "spread_line": "",
                },
                {
                    **rows[0],
                    "game_id": "second-lined",
                    "gameday": "2026-10-08",
                    "spread_line": -1,
                    "total_line": 45,
                },
            ]
        )

        parsed = parse_nflverse_schedules_csv(schedules_csv(rows), as_of=AS_OF)
        self.assertEqual(parsed.lined_games, 17)
        self.assertEqual(parsed.teams["ARI"].games, 2)
        self.assertEqual(parsed.teams["ATL"].games, 2)
        self.assertEqual(parsed.teams["ARI"].implied_points, (25 + 22) / 2)
        self.assertEqual(parsed.teams["ATL"].opponent_implied_points, (25 + 22) / 2)

    def test_rejects_structural_and_value_errors(self):
        cases = []

        missing_column_fields = tuple(
            field for field in SCHEDULE_FIELDS if field != "total_line"
        )
        cases.append(
            (
                "missing columns",
                schedules_csv(fields=missing_column_fields),
                "missing required columns",
            )
        )

        mutations = (
            ("bad date", "gameday", "not-a-date", "invalid gameday"),
            ("bad game type", "game_type", "UNKNOWN", "invalid game_type"),
            ("bad number", "spread_line", "number", "must be numeric"),
            ("nan", "total_line", "NaN", "must be finite"),
            ("infinity", "spread_line", "Infinity", "must be finite"),
            ("spread range", "spread_line", 41, "plausible range"),
            ("total range", "total_line", 101, "plausible range"),
            ("unknown team", "home_team", "XXX", "unknown NFL team"),
        )
        for label, key, value, message in mutations:
            rows = schedule_rows()
            rows[0][key] = value
            cases.append((label, schedules_csv(rows), message))

        duplicate = schedule_rows()
        duplicate[1]["game_id"] = duplicate[0]["game_id"]
        cases.append(("duplicate", schedules_csv(duplicate), "duplicate game"))
        cases.append(
            (
                "partial coverage",
                schedules_csv(schedule_rows()[:-1]),
                "coverage for all 32 teams",
            )
        )

        for label, content, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, message):
                    parse_nflverse_schedules_csv(content, as_of=AS_OF)

    def test_timestamp_validation(self):
        self.assertEqual(
            parse_nflverse_dataset_timestamp(
                json.dumps({"timestamp": "2026-08-01T10:00:00Z"})
            ),
            "2026-08-01T10:00:00+00:00",
        )
        for content in ("not json", "{}", '{"timestamp":"not-a-date"}'):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    parse_nflverse_dataset_timestamp(content)


class VegasProviderCacheTests(unittest.TestCase):
    def _refresh(self, provider: NflverseVegasProvider, schedule: str | None = None):
        responses = [
            FakeResponse(schedule or schedules_csv()),
            FakeResponse('{"timestamp":"2026-08-01T10:00:00Z"}'),
        ]
        with patch(
            "draft_agent.providers.urllib.request.urlopen", side_effect=responses
        ):
            return provider.refresh(as_of=AS_OF, now=NOW)

    def test_valid_refresh_records_provenance_and_cache_load_is_offline(self):
        with TemporaryDirectory() as directory:
            provider = NflverseVegasProvider(Path(directory))
            refreshed = self._refresh(provider)
            self.assertEqual(refreshed.status, "refreshed")
            self.assertTrue(refreshed.usable(NOW))
            self.assertEqual(refreshed.snapshot.coverage, 32)
            self.assertEqual(refreshed.snapshot.source_url, NFLVERSE_SCHEDULES_URL)
            self.assertEqual(refreshed.snapshot.license, "CC BY 4.0")

            with patch(
                "draft_agent.providers.urllib.request.urlopen",
                side_effect=AssertionError("network used"),
            ) as urlopen:
                cached = provider.load_cached(now=NOW + timedelta(hours=1))
            urlopen.assert_not_called()
            self.assertEqual(cached.status, "cached")
            self.assertTrue(cached.cached)
            self.assertEqual(cached.snapshot.teams["ARI"].games, 1)

    def test_failed_refresh_preserves_last_known_good_and_uses_fresh_fallback(self):
        with TemporaryDirectory() as directory:
            provider = NflverseVegasProvider(Path(directory))
            self._refresh(provider)
            original = provider.cache_path.read_bytes()

            partial = schedules_csv(schedule_rows()[:-1])
            result = self._refresh(provider, partial)
            self.assertEqual(result.status, "fallback")
            self.assertTrue(result.usable(NOW))
            self.assertIn("failed", result.error.lower())
            self.assertEqual(provider.cache_path.read_bytes(), original)

            responses = [FakeResponse(schedules_csv()), FakeResponse("not json")]
            with patch(
                "draft_agent.providers.urllib.request.urlopen", side_effect=responses
            ):
                timestamp_failure = provider.refresh(as_of=AS_OF, now=NOW)
            self.assertEqual(timestamp_failure.status, "fallback")
            self.assertEqual(provider.cache_path.read_bytes(), original)

    def test_stale_cache_is_visible_but_unusable_after_failed_refresh(self):
        with TemporaryDirectory() as directory:
            provider = NflverseVegasProvider(Path(directory))
            self._refresh(provider)
            stale_now = NOW + timedelta(hours=49)
            cached = provider.load_cached(now=stale_now)
            self.assertEqual(cached.status, "stale")
            self.assertIsNotNone(cached.snapshot)
            self.assertFalse(cached.usable(stale_now))

            with patch(
                "draft_agent.providers.urllib.request.urlopen",
                side_effect=OSError("offline fixture"),
            ):
                failed = provider.refresh(as_of=AS_OF, now=stale_now)
            self.assertEqual(failed.status, "stale")
            self.assertFalse(failed.usable(stale_now))
            self.assertIn("offline fixture", failed.error)

    def test_oversized_response_is_unavailable_and_does_not_create_cache(self):
        with TemporaryDirectory() as directory:
            provider = NflverseVegasProvider(Path(directory))
            response = FakeResponse("small", content_length=NFLVERSE_SCHEDULES_LIMIT + 1)
            with patch(
                "draft_agent.providers.urllib.request.urlopen", return_value=response
            ):
                result = provider.refresh(as_of=AS_OF, now=NOW)
            self.assertEqual(result.status, "unavailable")
            self.assertIn("safety limit", result.error)
            self.assertFalse(provider.cache_path.exists())


class VegasEnrichmentAndRankingTests(unittest.TestCase):
    def test_enriches_offense_kicker_and_dst_without_guessing_unknown_teams(self):
        snapshot = valid_snapshot()
        players = [
            Player("wr", "Receiver", "LA", "WR", 1),
            Player("k", "Kicker", "JAC", "K", 2),
            Player("dst", "Defense", "WAS", "DST", 3),
            Player(
                "unknown",
                "Unknown",
                "TST",
                "RB",
                4,
                signals={"vegas_implied_points": 99},
            ),
        ]
        enriched, matched = apply_vegas_context(players, snapshot)
        by_id = {player.player_id: player for player in enriched}

        self.assertEqual(matched, 3)
        self.assertGreater(by_id["wr"].signals["vegas_games"], 0)
        self.assertGreater(by_id["k"].signals["vegas_games"], 0)
        self.assertIn("vegas_opponent_implied_points", by_id["dst"].signals)
        self.assertNotIn("vegas_implied_points", by_id["unknown"].signals)

    def test_espn_catalog_enriches_current_players_without_historical_matches(self):
        historical = [
            Player(
                "historical",
                "Historical Back",
                "ARI",
                "RB",
                1,
                external_ids={"espn": "1"},
                projected_points_override=250,
            )
        ]
        records = [
            SignalRecord(
                "Historical Back",
                "ARI",
                "RB",
                external_ids={"espn": "1"},
                values={"consensus_rank": 1},
            ),
            SignalRecord(
                "Current Rookie",
                "ATL",
                "RB",
                external_ids={"espn": "2"},
                values={"consensus_rank": 2},
            ),
        ]
        catalog = [
            {
                "id": "1",
                "name": "Historical Back",
                "team": "ARI",
                "position": "RB",
                "rank": 1,
                "projected_points": 250,
            },
            {
                "id": "2",
                "name": "Current Rookie",
                "team": "ATL",
                "position": "RB",
                "rank": 2,
                "projected_points": 245,
            },
        ]
        config = league_config_for_profile(STANDARD_PPR, user_slot=1)
        bridge = EspnDraftBridge()
        bridge.minimum_catalog_size = 2
        state = bridge.ingest(
            {
                "league_id": "fake-league",
                "draft_id": "fake-draft",
                "overall_pick": 1,
                "on_clock": True,
                "is_mock": True,
                "user_slot": 1,
                "player_catalog": catalog,
                "available_player_ids": ["1", "2"],
                "roster_player_ids": [],
            },
            historical,
            DraftEngine(config, simulation_samples=20),
            config,
            records,
            {"ready": True, "reasons": [], "sources": {}},
            vegas_snapshot=valid_snapshot(),
        )
        by_id = {item["espn_id"]: item for item in state["recommendations"]}
        self.assertTrue(state["readiness"]["ready"])
        self.assertIn("vegas_implied_points", by_id["1"]["signals"])
        self.assertIn("vegas_implied_points", by_id["2"]["signals"])
        self.assertEqual(by_id["2"]["projected_points"], 245)

    @staticmethod
    def _signals(own: float, opponent: float, center: float = 24.0):
        return {
            "vegas_implied_points": own,
            "vegas_opponent_implied_points": opponent,
            "vegas_games": 1.0,
            "vegas_league_implied_points": center,
            "vegas_league_opponent_implied_points": center,
        }

    def test_signed_direction_clamp_and_missing_neutral(self):
        high = Player("high", "High", "BUF", "WR", 1, signals=self._signals(40, 24))
        low = Player("low", "Low", "NYJ", "WR", 2, signals=self._signals(8, 24))
        defense_low_opponent = Player(
            "dst-low", "Defense Low", "BUF", "DST", 3, signals=self._signals(24, 10)
        )
        defense_high_opponent = Player(
            "dst-high", "Defense High", "NYJ", "DST", 4, signals=self._signals(24, 38)
        )
        missing = Player("missing", "Missing", "BUF", "RB", 5)

        self.assertEqual(DraftEngine._vegas_environment(high), 1)
        self.assertEqual(DraftEngine._vegas_environment(low), -1)
        self.assertEqual(DraftEngine._vegas_environment(defense_low_opponent), 1)
        self.assertEqual(DraftEngine._vegas_environment(defense_high_opponent), -1)
        self.assertEqual(DraftEngine._vegas_environment(missing), 0)

    def test_no_data_score_and_order_identity_and_zero_reporting(self):
        players = [
            Player(
                f"rb-{index}",
                f"RB {index}",
                f"T{index}",
                "RB",
                10 + index,
                projected_points_override=250 - index,
            )
            for index in range(6)
        ]
        incomplete = [
            Player(
                **{
                    **player.__dict__,
                    "signals": {"vegas_implied_points": 30.0},
                }
            )
            for player in players
        ]
        baseline = DraftEngine(
            league_config_for_profile(STANDARD_PPR), simulation_samples=20
        ).rank(players, [], 1, 24, 6)
        neutral = DraftEngine(
            league_config_for_profile(STANDARD_PPR), simulation_samples=20
        ).rank(incomplete, [], 1, 24, 6)

        self.assertEqual(
            [(item["id"], item["draft_score"]) for item in neutral],
            [(item["id"], item["draft_score"]) for item in baseline],
        )
        self.assertTrue(
            all(item["components"]["vegas_environment"] == 0 for item in neutral)
        )
        self.assertTrue(
            all(item["contributions"]["vegas_environment"] == 0 for item in neutral)
        )

    def test_contribution_is_capped_in_standard_and_both_superflex_profiles(self):
        for profile in (
            STANDARD_PPR,
            SUPERFLEX_REPLACES_FLEX,
            FLEX_AND_SUPERFLEX,
        ):
            with self.subTest(profile=profile):
                high = Player(
                    "high",
                    "High Environment",
                    "BUF",
                    "RB",
                    10,
                    projected_points_override=250,
                    signals=self._signals(31, 24),
                )
                low = Player(
                    "low",
                    "Low Environment",
                    "NYJ",
                    "RB",
                    10,
                    projected_points_override=250,
                    signals=self._signals(17, 24),
                )
                engine = DraftEngine(
                    league_config_for_profile(profile), simulation_samples=20
                )
                engine.weights.vegas_environment = 1.0
                ranked = engine.rank([low, high], [], 1, 24, 2)
                by_id = {item["id"]: item for item in ranked}
                self.assertEqual(by_id["high"]["contributions"]["vegas_environment"], 0.03)
                self.assertEqual(by_id["low"]["contributions"]["vegas_environment"], -0.03)
                self.assertGreater(by_id["high"]["draft_score"], by_id["low"]["draft_score"])

        weights = StrategyWeights()
        weights.update({"vegas_environment": 1})
        self.assertEqual(weights.vegas_environment, 0.03)


if __name__ == "__main__":
    unittest.main()
