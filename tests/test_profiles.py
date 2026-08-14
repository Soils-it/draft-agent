from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from draft_agent import server
from draft_agent.config import (
    FLEX_AND_SUPERFLEX,
    STANDARD_PPR,
    SUPERFLEX_REPLACES_FLEX,
    LeagueConfig,
    league_config_for_profile,
)
from draft_agent.data import demo_players
from draft_agent.engine import DraftEngine
from draft_agent.espn import EspnDraftBridge
from draft_agent.models import Player
from draft_agent.session import DraftSession
from draft_agent.simulation import simulate_turn_value


class LeagueProfileTests(unittest.TestCase):
    @staticmethod
    def _te_foundation(round_number: int) -> list[Player]:
        roster = [
            Player(
                "qb-1",
                "QB One",
                "AAA",
                "QB",
                1,
                signals={"superflex_rank": 1, "espn_position_rank": 1},
            ),
            Player(
                "qb-2",
                "QB Two",
                "BBB",
                "QB",
                12,
                signals={"superflex_rank": 12, "espn_position_rank": 8},
            ),
            Player("rb-1", "RB One", "CCC", "RB", 20),
            Player("wr-1", "WR One", "DDD", "WR", 25),
        ]
        if round_number >= 6:
            roster.extend(
                [
                    Player("rb-2", "RB Two", "EEE", "RB", 40),
                    Player("wr-2", "WR Two", "FFF", "WR", 45),
                ]
            )
        return roster

    @staticmethod
    def _strong_superflex_qbs() -> list[Player]:
        return [
            Player(
                "qb-1",
                "Elite QB",
                "AAA",
                "QB",
                1,
                projected_points_override=390,
                signals={"superflex_rank": 1, "espn_position_rank": 1},
            ),
            Player(
                "qb-2",
                "Strong QB Two",
                "BBB",
                "QB",
                12,
                projected_points_override=330,
                signals={"superflex_rank": 12, "espn_position_rank": 8},
            ),
        ]

    def test_profile_rosters_cover_both_superflex_layouts(self):
        standard = league_config_for_profile(STANDARD_PPR)
        superflex = league_config_for_profile(SUPERFLEX_REPLACES_FLEX)
        flex_and_superflex = league_config_for_profile(FLEX_AND_SUPERFLEX)

        self.assertEqual(standard.roster_size, 16)
        self.assertEqual(standard.flex_slots, 1)
        self.assertEqual(standard.superflex_slots, 0)
        self.assertEqual(standard.position_starters("QB"), 1)

        self.assertEqual(superflex.roster_size, 16)
        self.assertEqual(superflex.flex_slots, 0)
        self.assertEqual(superflex.superflex_slots, 1)
        self.assertEqual(superflex.position_starters("QB"), 2)

        self.assertEqual(flex_and_superflex.roster_size, 17)
        self.assertEqual(flex_and_superflex.flex_slots, 1)
        self.assertEqual(flex_and_superflex.superflex_slots, 1)
        self.assertEqual(flex_and_superflex.position_starters("QB"), 2)

    def test_standard_profile_preserves_existing_rankings(self):
        players = demo_players()
        direct = DraftEngine(LeagueConfig(user_slot=6), simulation_samples=20)
        selected = DraftEngine(
            league_config_for_profile(STANDARD_PPR, user_slot=6),
            simulation_samples=20,
        )
        self.assertEqual(
            direct.rank(players, [], 6, 19, 10),
            selected.rank(players, [], 6, 19, 10),
        )

    def test_standard_round_five_fixture_and_deadlines_match_origin_main(self):
        engine = DraftEngine(
            league_config_for_profile(STANDARD_PPR), simulation_samples=20
        )
        engine.weights.update({key: 0 for key in engine.weights.__dict__})
        engine.weights.update({"market_quality": 1})
        roster = [
            Player("rb-1", "RB One", "AAA", "RB", 10),
            Player("rb-2", "RB Two", "BBB", "RB", 20),
            Player("wr-1", "WR One", "CCC", "WR", 12),
            Player("wr-2", "WR Two", "DDD", "WR", 24),
        ]
        candidates = [
            Player(
                "tier-te",
                "Tier TE",
                "EEE",
                "TE",
                50,
                projected_points_override=180,
                signals={"espn_position_rank": 5},
            ),
            Player(
                "strong-wr",
                "Strong WR",
                "FFF",
                "WR",
                45,
                projected_points_override=220,
                signals={"espn_position_rank": 25},
            ),
        ]

        ranked = engine.rank(candidates, roster, 49, 68, 2)

        self.assertEqual(
            [(item["id"], item["draft_score"]) for item in ranked],
            [("strong-wr", 1.0), ("tier-te", 0.6592)],
        )
        self.assertTrue(all(not item["te_tier_triggered"] for item in ranked))

        qb1 = Player("qb-1", "QB One", "AAA", "QB", 60)
        qb2 = Player("qb-2", "QB Two", "BBB", "QB", 100)
        te1 = Player("te-1", "TE One", "CCC", "TE", 80)
        te2 = Player("te-2", "TE Two", "DDD", "TE", 120)
        self.assertEqual(engine._roster_need(qb2, [qb1], 12, 140), -1)
        self.assertEqual(engine._roster_need(te1, roster, 3, 30), -1)
        self.assertGreaterEqual(engine._roster_need(te1, roster, 4, 40), 0)
        self.assertEqual(engine._roster_need(te2, [te1], 12, 140), -1)
        self.assertIn("QB", engine._required_positions(roster, 10))
        self.assertNotIn("TE", engine._required_positions([*roster, qb1], 12))
        self.assertIn("TE", engine._required_positions([*roster, qb1], 13))

    def test_superflex_uses_position_qb_market_and_deeper_replacement(self):
        elite_qb = Player(
            "qb-1",
            "Elite QB",
            "AAA",
            "QB",
            40,
            projected_points_override=390,
            signals={"consensus_rank": 40, "espn_position_rank": 1},
        )
        qb_24 = Player(
            "qb-24",
            "QB Twenty Four",
            "BBB",
            "QB",
            180,
            projected_points_override=260,
            signals={"consensus_rank": 180, "espn_position_rank": 24},
        )
        superflex_wr = Player(
            "wr-sf",
            "Superflex WR",
            "CCC",
            "WR",
            10,
            projected_points_override=280,
            signals={"consensus_rank": 10, "superflex_rank": 25},
        )
        standard = DraftEngine(league_config_for_profile(STANDARD_PPR))
        superflex = DraftEngine(
            league_config_for_profile(SUPERFLEX_REPLACES_FLEX)
        )

        self.assertEqual(standard._draft_market_rank(elite_qb), 40)
        self.assertEqual(superflex._draft_market_rank(elite_qb), 1)
        self.assertEqual(superflex._draft_market_rank(qb_24), 93)
        self.assertEqual(standard._draft_market_rank(superflex_wr), 10)
        self.assertEqual(superflex._draft_market_rank(superflex_wr), 25)
        self.assertEqual(standard.replacement_rank["QB"], 12)
        self.assertEqual(superflex.replacement_rank["QB"], 30)

    def test_superflex_requires_two_qbs_but_standard_still_blocks_early_qb2(self):
        qb1 = Player("qb-1", "QB One", "AAA", "QB", 12)
        qb2 = Player("qb-2", "QB Two", "BBB", "QB", 28)
        standard = DraftEngine(league_config_for_profile(STANDARD_PPR))
        superflex = DraftEngine(
            league_config_for_profile(SUPERFLEX_REPLACES_FLEX)
        )

        self.assertEqual(standard._roster_need(qb2, [qb1], 5, 50), -1)
        self.assertEqual(superflex._roster_need(qb2, [qb1], 5, 50), 1)
        self.assertIn("QB", superflex._required_positions([], 3))
        self.assertIn("QB", superflex._required_positions([qb1], 7))
        self.assertNotIn("QB", superflex._required_positions([qb1, qb2], 7))

    def test_round_seven_superflex_qb2_deadline_survives_a_depleted_room(self):
        config = league_config_for_profile(SUPERFLEX_REPLACES_FLEX)
        engine = DraftEngine(config, simulation_samples=20)
        roster = [
            Player("qb-1", "QB One", "AAA", "QB", 10),
            Player("rb-1", "RB One", "BBB", "RB", 12),
            Player("rb-2", "RB Two", "CCC", "RB", 24),
            Player("wr-1", "WR One", "DDD", "WR", 15),
            Player("wr-2", "WR Two", "EEE", "WR", 30),
            Player("te-1", "TE One", "FFF", "TE", 50),
        ]
        late_qb = Player(
            "qb-30",
            "Last Starting QB",
            "GGG",
            "QB",
            180,
            projected_points_override=230,
            signals={"espn_position_rank": 30},
        )
        fair_wr = Player(
            "wr-value",
            "Fair WR",
            "HHH",
            "WR",
            73,
            projected_points_override=260,
        )

        ranked = engine.rank([late_qb, fair_wr], roster, 73, 84, 1)
        self.assertEqual(ranked[0]["id"], "qb-30")

    def test_superflex_te_tier_round_and_rank_boundaries(self):
        round_thresholds = {
            4: None,
            5: 5,
            6: 5,
            7: 8,
            8: 10,
            9: 12,
            10: None,
        }
        for profile_id in (SUPERFLEX_REPLACES_FLEX, FLEX_AND_SUPERFLEX):
            engine = DraftEngine(
                league_config_for_profile(profile_id), simulation_samples=20
            )
            for round_number, threshold in round_thresholds.items():
                with self.subTest(
                    profile=profile_id,
                    round_number=round_number,
                    threshold=threshold,
                ):
                    current_pick = (round_number - 1) * 12 + 1
                    te = Player(
                        "tier-te",
                        "Tier Tight End",
                        "GGG",
                        "TE",
                        current_pick,
                        signals={
                            "superflex_rank": current_pick,
                            "espn_position_rank": threshold or 1,
                        },
                    )
                    triggered = engine._triggered_te_tier(
                        [te],
                        self._te_foundation(round_number),
                        round_number,
                        current_pick,
                        engine._market_reach_limit(round_number),
                    )
                    self.assertEqual(triggered, {"tier-te"} if threshold else set())

                    if threshold is not None:
                        outside_tier = Player(
                            "outside-tier-te",
                            "Outside Tier Tight End",
                            "HHH",
                            "TE",
                            current_pick,
                            signals={
                                "superflex_rank": current_pick,
                                "espn_position_rank": threshold + 1,
                            },
                        )
                        self.assertEqual(
                            engine._triggered_te_tier(
                                [outside_tier],
                                self._te_foundation(round_number),
                                round_number,
                                current_pick,
                                engine._market_reach_limit(round_number),
                            ),
                            set(),
                        )

    def test_superflex_te_tier_requires_foundation_and_market_reach(self):
        for profile_id in (SUPERFLEX_REPLACES_FLEX, FLEX_AND_SUPERFLEX):
            engine = DraftEngine(
                league_config_for_profile(profile_id), simulation_samples=20
            )
            round_five_pick = 49
            round_five_te = Player(
                "round-five-te",
                "Round Five TE",
                "GGG",
                "TE",
                round_five_pick,
                signals={
                    "superflex_rank": round_five_pick,
                    "espn_position_rank": 5,
                },
            )
            foundation = self._te_foundation(5)
            reach_limit = engine._market_reach_limit(5)
            self.assertEqual(
                engine._triggered_te_tier(
                    [round_five_te], foundation, 5, round_five_pick, reach_limit
                ),
                {"round-five-te"},
            )
            for missing_position in ("QB", "RB", "WR"):
                with self.subTest(profile=profile_id, missing=missing_position):
                    incomplete = list(foundation)
                    incomplete.remove(
                        next(
                            player
                            for player in reversed(incomplete)
                            if player.position == missing_position
                        )
                    )
                    self.assertEqual(
                        engine._triggered_te_tier(
                            [round_five_te],
                            incomplete,
                            5,
                            round_five_pick,
                            reach_limit,
                        ),
                        set(),
                    )

            round_six_pick = 61
            round_six_te = Player(
                "round-six-te",
                "Round Six TE",
                "HHH",
                "TE",
                round_six_pick,
                signals={
                    "superflex_rank": round_six_pick,
                    "espn_position_rank": 5,
                },
            )
            full_foundation = self._te_foundation(6)
            round_six_limit = engine._market_reach_limit(6)
            self.assertEqual(
                engine._triggered_te_tier(
                    [round_six_te],
                    full_foundation,
                    6,
                    round_six_pick,
                    round_six_limit,
                ),
                {"round-six-te"},
            )
            for missing_id in ("rb-2", "wr-2"):
                with self.subTest(profile=profile_id, missing=missing_id):
                    incomplete = [
                        player
                        for player in full_foundation
                        if player.player_id != missing_id
                    ]
                    self.assertEqual(
                        engine._triggered_te_tier(
                            [round_six_te],
                            incomplete,
                            6,
                            round_six_pick,
                            round_six_limit,
                        ),
                        set(),
                    )

            reach_te = Player(
                "reach-te",
                "Reach Tight End",
                "III",
                "TE",
                round_six_pick,
                signals={
                    "superflex_rank": round_six_pick + round_six_limit + 1,
                    "espn_position_rank": 5,
                },
            )
            self.assertEqual(
                engine._triggered_te_tier(
                    [reach_te],
                    full_foundation,
                    6,
                    round_six_pick,
                    round_six_limit,
                ),
                set(),
            )

    def test_superflex_te_tier_forces_only_usable_tier_options(self):
        unusable_contexts = (
            {"injury_status": "Out"},
            {"injury_status": "Doubtful"},
            {"injury_status": "IR"},
            {"nfl_status": "PUP"},
            {"nfl_status": "inactive"},
        )
        current_pick = 73
        for profile_id in (SUPERFLEX_REPLACES_FLEX, FLEX_AND_SUPERFLEX):
            engine = DraftEngine(
                league_config_for_profile(profile_id), simulation_samples=20
            )
            roster = self._te_foundation(7)
            healthy_te = Player(
                "healthy-te",
                "Healthy Tight End",
                "GGG",
                "TE",
                current_pick,
                signals={
                    "superflex_rank": current_pick,
                    "espn_position_rank": 8,
                },
            )
            questionable_te = Player(
                "questionable-te",
                "Questionable Tight End",
                "HHH",
                "TE",
                current_pick,
                signals={
                    "superflex_rank": current_pick,
                    "espn_position_rank": 8,
                },
                context={"injury_status": "Questionable"},
            )
            self.assertEqual(
                engine._triggered_te_tier(
                    [healthy_te, questionable_te],
                    roster,
                    7,
                    current_pick,
                    engine._market_reach_limit(7),
                ),
                {"healthy-te", "questionable-te"},
            )

            for index, context in enumerate(unusable_contexts):
                with self.subTest(profile=profile_id, context=context):
                    unusable_te = Player(
                        f"unusable-te-{index}",
                        f"Unusable Tight End {index}",
                        "III",
                        "TE",
                        current_pick,
                        signals={
                            "superflex_rank": current_pick,
                            "espn_position_rank": 1,
                        },
                        context=context,
                    )
                    bench_wr = Player(
                        "bench-wr",
                        "Bench Receiver",
                        "JJJ",
                        "WR",
                        current_pick,
                        signals={"superflex_rank": current_pick},
                    )
                    ranked = engine.rank(
                        [unusable_te, bench_wr],
                        roster,
                        current_pick,
                        84,
                        2,
                    )
                    self.assertNotEqual(
                        [item["id"] for item in ranked], [unusable_te.player_id]
                    )
                    self.assertTrue(
                        all(not item["te_tier_triggered"] for item in ranked)
                    )

            out_te = Player(
                "out-te",
                "Out Tight End",
                "KKK",
                "TE",
                current_pick,
                signals={
                    "superflex_rank": current_pick,
                    "espn_position_rank": 1,
                },
                context={"injury_status": "Out"},
            )
            ranked = engine.rank(
                [out_te, healthy_te], roster, current_pick, 84, 2
            )
            self.assertEqual([item["id"] for item in ranked], ["healthy-te"])
            self.assertTrue(ranked[0]["te_tier_triggered"])

    def test_te_tier_hard_filter_and_explanation_are_superflex_only(self):
        current_pick = 73
        tier_te = Player(
            "tier-te",
            "Tier Tight End",
            "GGG",
            "TE",
            current_pick,
            signals={"superflex_rank": current_pick, "espn_position_rank": 8},
        )
        bench_wr = Player(
            "bench-wr",
            "Bench Receiver",
            "HHH",
            "WR",
            current_pick,
            signals={"superflex_rank": current_pick},
        )
        for profile_id in (SUPERFLEX_REPLACES_FLEX, FLEX_AND_SUPERFLEX):
            engine = DraftEngine(
                league_config_for_profile(profile_id), simulation_samples=20
            )
            ranked = engine.rank(
                [bench_wr, tier_te],
                self._te_foundation(7),
                current_pick,
                84,
                2,
            )
            self.assertEqual([item["id"] for item in ranked], ["tier-te"])
            self.assertTrue(ranked[0]["te_tier_triggered"])

        standard = DraftEngine(
            league_config_for_profile(STANDARD_PPR), simulation_samples=20
        )
        standard_roster = [
            player
            for player in self._te_foundation(7)
            if player.position != "QB"
        ]
        ranked = standard.rank(
            [bench_wr, tier_te], standard_roster, current_pick, 84, 2
        )
        self.assertEqual({item["id"] for item in ranked}, {"bench-wr", "tier-te"})
        self.assertTrue(all(not item["te_tier_triggered"] for item in ranked))

    def test_qb3_starter_detection_prefers_explicit_depth_chart_data(self):
        engine = DraftEngine(
            league_config_for_profile(SUPERFLEX_REPLACES_FLEX),
            simulation_samples=20,
        )

        def candidate(
            player_id: str,
            signals: dict[str, float],
            context: dict[str, str] | None = None,
        ) -> Player:
            return Player(
                player_id,
                player_id,
                "CCC",
                "QB",
                100,
                signals=signals,
                context=context or {},
            )

        self.assertTrue(
            engine._is_starting_qb(
                candidate(
                    "depth-one",
                    {"depth_chart_order": 1, "espn_position_rank": 40},
                )
            )
        )
        self.assertFalse(
            engine._is_starting_qb(
                candidate(
                    "explicit-backup",
                    {"depth_chart_order": 2, "espn_position_rank": 28},
                )
            )
        )
        self.assertTrue(
            engine._is_starting_qb(candidate("fallback-32", {"espn_position_rank": 32}))
        )
        self.assertFalse(
            engine._is_starting_qb(candidate("fallback-33", {"espn_position_rank": 33}))
        )
        self.assertFalse(
            engine._is_starting_qb(
                candidate(
                    "inactive",
                    {"depth_chart_order": 1, "espn_position_rank": 1},
                    {"nfl_status": "inactive"},
                )
            )
        )
        self.assertFalse(
            engine._is_starting_qb(
                candidate(
                    "ir",
                    {"depth_chart_order": 1, "espn_position_rank": 1},
                    {"nfl_status": "IR"},
                )
            )
        )

    def test_qb3_shortcut_is_bounded_to_rounds_nine_through_eleven(self):
        for profile_id in (SUPERFLEX_REPLACES_FLEX, FLEX_AND_SUPERFLEX):
            engine = DraftEngine(
                league_config_for_profile(profile_id), simulation_samples=20
            )
            roster = self._strong_superflex_qbs()
            starter_qb3 = Player(
                "qb-3",
                "Starting QB Three",
                "CCC",
                "QB",
                100,
                projected_points_override=250,
                signals={
                    "superflex_rank": 70,
                    "espn_position_rank": 35,
                    "depth_chart_order": 1,
                },
            )
            for round_number, expected in ((8, -1.0), (9, 0.45), (11, 0.45), (12, -1.0)):
                with self.subTest(profile=profile_id, round_number=round_number):
                    current_pick = (round_number - 1) * 12 + 1
                    self.assertEqual(
                        engine._roster_need(
                            starter_qb3, roster, round_number, current_pick
                        ),
                        expected,
                    )

    def test_qb3_backup_veto_fallback_and_post_window_policy(self):
        for profile_id in (SUPERFLEX_REPLACES_FLEX, FLEX_AND_SUPERFLEX):
            engine = DraftEngine(
                league_config_for_profile(profile_id), simulation_samples=20
            )
            roster = self._strong_superflex_qbs()

            def qb3(
                player_id: str,
                *,
                depth_order: int | None,
                position_rank: int,
                market_rank: int = 70,
                points: int = 250,
                context: dict[str, str] | None = None,
            ) -> Player:
                signals = {
                    "superflex_rank": market_rank,
                    "espn_position_rank": position_rank,
                }
                if depth_order is not None:
                    signals["depth_chart_order"] = depth_order
                return Player(
                    player_id,
                    player_id,
                    "CCC",
                    "QB",
                    100,
                    projected_points_override=points,
                    signals=signals,
                    context=context or {},
                )

            self.assertEqual(
                engine._roster_need(
                    qb3("explicit-backup", depth_order=2, position_rank=28),
                    roster,
                    9,
                    97,
                ),
                -1,
            )
            self.assertEqual(
                engine._roster_need(
                    qb3("fallback-starter", depth_order=None, position_rank=32),
                    roster,
                    9,
                    97,
                ),
                0.45,
            )
            self.assertEqual(
                engine._roster_need(
                    qb3("fallback-backup", depth_order=None, position_rank=33),
                    roster,
                    9,
                    97,
                ),
                -1,
            )
            self.assertEqual(
                engine._roster_need(
                    qb3(
                        "inactive-starter",
                        depth_order=1,
                        position_rank=1,
                        context={"nfl_status": "inactive"},
                    ),
                    roster,
                    9,
                    97,
                ),
                -1,
            )

            injured_roster = [
                roster[0],
                Player(
                    "qb-2-injured",
                    "Injured QB Two",
                    "BBB",
                    "QB",
                    12,
                    projected_points_override=330,
                    signals={"superflex_rank": 12, "espn_position_rank": 8},
                    context={"injury_status": "Questionable"},
                ),
            ]
            self.assertEqual(
                engine._roster_need(
                    qb3("injury-cover", depth_order=2, position_rank=40),
                    injured_roster,
                    12,
                    133,
                ),
                0.45,
            )
            self.assertEqual(
                engine._roster_need(
                    qb3(
                        "material-upgrade",
                        depth_order=2,
                        position_rank=28,
                        market_rank=3,
                        points=350,
                    ),
                    roster,
                    12,
                    133,
                ),
                0.45,
            )

    def test_superflex_profiles_build_legal_rosters_across_the_snake(self):
        for profile_id in (SUPERFLEX_REPLACES_FLEX, FLEX_AND_SUPERFLEX):
            for slot in (1, 6, 12):
                with self.subTest(profile=profile_id, slot=slot):
                    config = league_config_for_profile(profile_id, user_slot=slot)
                    session = DraftSession(demo_players(), config)
                    session.engine.simulation_samples = 20
                    while not session.is_complete:
                        recommendations = session.recommendations()
                        self.assertTrue(recommendations)
                        session.make_user_pick(str(recommendations[0]["id"]), "test")
                    roster = session.user_roster()
                    counts = Counter(player.position for player in roster)
                    self.assertEqual(len(roster), config.roster_size)
                    self.assertGreaterEqual(counts["QB"], 2)
                    self.assertGreaterEqual(counts["RB"], 2)
                    self.assertGreaterEqual(counts["WR"], 2)
                    for position in ("TE", "K", "DST"):
                        self.assertGreaterEqual(counts[position], 1)
                    for position, cap in config.position_caps.items():
                        self.assertLessEqual(counts[position], cap)

    def test_profile_specific_market_is_used_by_turn_simulation(self):
        players = [
            Player(
                str(rank),
                f"Player {rank}",
                "TST",
                "QB",
                rank,
                projected_points_override=300 - rank,
            )
            for rank in range(1, 41)
        ]
        points = {
            player.player_id: float(player.projected_points_override or 0)
            for player in players
        }
        reversed_market = {
            player.player_id: 41 - float(player.player_id) for player in players
        }
        result = simulate_turn_value(
            players,
            points,
            1,
            12,
            samples=400,
            candidate_limit=40,
            market_ranks=reversed_market,
        )
        self.assertLess(
            result["40"].survival_probability,
            result["1"].survival_probability,
        )


class LeagueProfileIntegrationTests(unittest.TestCase):
    def test_espn_catalog_assigns_stable_position_ranks_without_faking_signals(self):
        historical = [
            Player("one", "QB One", "AAA", "QB", 5, external_ids={"espn": "1"}),
            Player("two", "WR One", "BBB", "WR", 2, external_ids={"espn": "2"}),
            Player("three", "QB Two", "CCC", "QB", 30, external_ids={"espn": "3"}),
            Player("four", "RB One", "DDD", "RB", 1, external_ids={"espn": "4"}),
        ]
        payload = {
            "player_catalog": [
                {
                    "id": player.external_ids["espn"],
                    "name": player.name,
                    "team": player.team,
                    "position": player.position,
                    "rank": player.adp,
                    "projected_points": 250,
                }
                for player in historical
            ]
        }
        catalog, historical_rate, signal_rate = EspnDraftBridge._catalog(
            payload, historical
        )
        by_id = {player.external_ids["espn"]: player for player in catalog}
        self.assertEqual(by_id["1"].signals["espn_position_rank"], 1)
        self.assertEqual(by_id["3"].signals["espn_position_rank"], 2)
        self.assertEqual(by_id["2"].signals["espn_position_rank"], 1)
        self.assertEqual(historical_rate, 1)
        self.assertEqual(signal_rate, 0)

    def test_selected_profile_persists_and_preserves_loaded_players(self):
        original_session = server.SESSION
        original_override = server.OVERRIDE_SECONDS
        original_samples = server.SIMULATION_SAMPLES
        original_path = server.RUNTIME_SETTINGS_PATH
        original_bridge_state = server.ESPN_BRIDGE.state
        try:
            with TemporaryDirectory() as directory:
                server.RUNTIME_SETTINGS_PATH = Path(directory) / "settings.json"
                server.SESSION = DraftSession(
                    demo_players(), league_config_for_profile(STANDARD_PPR)
                )
                server.OVERRIDE_SECONDS = 20
                server.SIMULATION_SAMPLES = 200
                player_ids = set(server.SESSION.players)

                server.apply_settings(
                    {
                        "league_profile": FLEX_AND_SUPERFLEX,
                        "user_slot": 12,
                        "override_seconds": 5,
                        "simulation_samples": 50,
                    }
                )

                self.assertEqual(server.SESSION.config.profile_id, FLEX_AND_SUPERFLEX)
                self.assertEqual(server.SESSION.config.roster_size, 17)
                self.assertEqual(set(server.SESSION.players), player_ids)
                self.assertEqual(
                    json.loads(server.RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))[
                        "league_profile"
                    ],
                    FLEX_AND_SUPERFLEX,
                )
                saved = server._load_runtime_settings()
                self.assertIsNotNone(saved)
                self.assertEqual(saved["league_profile"], FLEX_AND_SUPERFLEX)

                state = server._state_payload()
                self.assertEqual(state["settings"]["roster_size"], 17)
                self.assertEqual(len(state["league_profiles"]), 3)
        finally:
            server.SESSION = original_session
            server.OVERRIDE_SECONDS = original_override
            server.SIMULATION_SAMPLES = original_samples
            server.RUNTIME_SETTINGS_PATH = original_path
            server.ESPN_BRIDGE.state = original_bridge_state


if __name__ == "__main__":
    unittest.main()
