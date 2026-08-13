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
