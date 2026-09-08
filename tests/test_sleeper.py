from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from draft_agent.config import (
    STANDARD_PPR,
    SUPERFLEX_REPLACES_FLEX,
    league_config_for_profile,
)
from draft_agent.engine import DraftEngine
from draft_agent.models import Player
from draft_agent.sleeper import SleeperApiClient, SleeperDraftBridge


def generated_players(count: int = 120) -> list[Player]:
    positions = ("QB", "RB", "WR", "TE", "K", "DST")
    return [
        Player(
            f"local-{rank}",
            f"Generated Player {rank}",
            f"T{rank % 32:02d}",
            positions[(rank - 1) % len(positions)],
            rank,
            external_ids={"sleeper": f"sl-{rank}", "espn": f"espn-{rank}"},
            projected_points_override=300 - rank / 2,
            signals={"consensus_rank": rank},
        )
        for rank in range(1, count + 1)
    ]


def metadata(draft_id: str = "draft-test", user_id: str = "user-16") -> dict:
    return {
        "draft_id": draft_id,
        "league_id": "league-test",
        "type": "snake",
        "status": "drafting",
        "settings": {
            "teams": 16,
            "rounds": 16,
            "slots_qb": 1,
            "slots_rb": 2,
            "slots_wr": 2,
            "slots_te": 1,
            "slots_flex": 1,
            "slots_sflex": 0,
            "slots_k": 1,
            "slots_def": 1,
            "slots_bn": 7,
        },
        "draft_order": {f"user-{slot}": slot for slot in range(1, 17)},
        "slot_to_roster_id": {
            str(slot): f"roster-{slot}" for slot in range(1, 17)
        },
    }


def picks(count: int, draft_id: str = "draft-test") -> list[dict]:
    result = []
    for pick_no in range(1, count + 1):
        draft_slot = SleeperDraftBridge._team_for_pick(pick_no)
        result.append(
            {
                "draft_id": draft_id,
                "pick_no": pick_no,
                "round": (pick_no - 1) // 16 + 1,
                "draft_slot": draft_slot,
                "roster_id": f"roster-{draft_slot}",
                "player_id": f"sl-{pick_no}",
            }
        )
    return result


class FakeClient:
    def __init__(self, draft_payload: object, picks_payload: object):
        self.draft_payload = draft_payload
        self.picks_payload = picks_payload
        self.calls: list[tuple[str, str]] = []

    def draft(self, draft_id: object) -> object:
        self.calls.append(("draft", str(draft_id)))
        return copy.deepcopy(self.draft_payload)

    def picks(self, draft_id: object) -> object:
        self.calls.append(("picks", str(draft_id)))
        return copy.deepcopy(self.picks_payload)


class SleeperDraftBridgeTests(unittest.TestCase):
    def setUp(self):
        self.players = generated_players()
        self.config = league_config_for_profile(
            STANDARD_PPR, teams=16, user_slot=16
        )
        self.engine = DraftEngine(self.config, simulation_samples=20)

    def synchronize(self, draft_payload=None, picks_payload=None):
        client = FakeClient(
            metadata() if draft_payload is None else draft_payload,
            picks(16) if picks_payload is None else picks_payload,
        )
        bridge = SleeperDraftBridge(client)  # type: ignore[arg-type]
        state = bridge.synchronize(
            "draft-test", "user-16", self.players, self.engine, self.config
        )
        return bridge, client, state

    def test_connected_state_derives_slot_roster_and_direct_mapping(self):
        bridge, client, state = self.synchronize()

        self.assertEqual(client.calls, [("draft", "draft-test"), ("picks", "draft-test")])
        self.assertTrue(state["connected"])
        self.assertEqual(state["provider"], "sleeper")
        self.assertEqual(state["mode"], "shadow")
        self.assertFalse(state["can_submit"])
        self.assertEqual(state["draft_id"], "draft-test")
        self.assertEqual(state["league_id"], "league-test")
        self.assertEqual(state["teams"], 16)
        self.assertEqual(state["user_slot"], 16)
        self.assertEqual(state["current_pick"], 17)
        self.assertEqual(state["decision_pick"], 17)
        self.assertEqual([item["sleeper_id"] for item in state["roster"]], ["sl-16"])
        self.assertEqual(state["mapping_rate"], 1.0)
        self.assertTrue(state["recommendations"])
        self.assertTrue(state["readiness"]["ready"])
        self.assertNotIn("submit", bridge.__class__.__dict__)

    def test_snake_boundaries_next_picks_and_completion(self):
        self.assertEqual(
            [SleeperDraftBridge._team_for_pick(item) for item in (1, 16, 17, 32)],
            [1, 16, 16, 1],
        )
        self.assertEqual(SleeperDraftBridge._next_user_pick(1, 1), 1)
        self.assertEqual(SleeperDraftBridge._next_user_pick(1, 8), 8)
        self.assertEqual(SleeperDraftBridge._next_user_pick(1, 16), 16)
        self.assertEqual(SleeperDraftBridge._next_user_pick(256, 1), 256)
        self.assertIsNone(SleeperDraftBridge._next_user_pick(257, 1))

        all_players = generated_players(256)
        bridge = SleeperDraftBridge(FakeClient(metadata(), picks(256)))  # type: ignore[arg-type]
        state = bridge.synchronize(
            "draft-test", "user-16", all_players,
            DraftEngine(self.config, simulation_samples=20), self.config,
        )
        self.assertTrue(state["complete"])
        self.assertEqual(state["current_pick"], 257)
        self.assertIsNone(state["decision_pick"])
        self.assertEqual(state["recommendations"], [])
        self.assertEqual(len(state["roster"]), 16)

    def test_rejects_invalid_metadata_before_ranking(self):
        mutations = {
            "non-snake": lambda value: value.update(type="linear"),
            "wrong teams": lambda value: value["settings"].update(teams=12),
            "wrong rounds": lambda value: value["settings"].update(rounds=17),
            "superflex": lambda value: value["settings"].update(slots_sflex=1),
            "IDP slot": lambda value: value["settings"].update(slots_lb=1),
            "unknown user": lambda value: value["draft_order"].pop("user-16"),
            "bad roster map": lambda value: value["slot_to_roster_id"].update(
                {"16": "roster-15"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = metadata()
                mutate(payload)
                client = FakeClient(payload, picks(16))
                bridge = SleeperDraftBridge(client)  # type: ignore[arg-type]
                with self.assertRaisesRegex(ValueError, "Sleeper sync failed"):
                    bridge.synchronize(
                        "draft-test", "user-16", self.players, self.engine, self.config
                    )
                self.assertFalse(bridge.state["connected"])
                self.assertEqual(bridge.state["provider"], "sleeper")
                self.assertEqual(bridge.state["recommendations"], [])

    def test_rejects_duplicate_noncontiguous_and_inconsistent_picks(self):
        variants = {}
        duplicate = picks(2)
        duplicate[1]["pick_no"] = 1
        variants["duplicate"] = duplicate
        gap = picks(3)
        gap.pop(1)
        variants["contiguous"] = gap
        wrong_slot = picks(1)
        wrong_slot[0]["draft_slot"] = 2
        variants["snake order"] = wrong_slot
        duplicate_player = picks(2)
        duplicate_player[1]["player_id"] = duplicate_player[0]["player_id"]
        variants["duplicate player"] = duplicate_player
        unknown_player = picks(1)
        unknown_player[0]["player_id"] = "unknown-player"
        variants["direct local mapping"] = unknown_player

        for expected, payload in variants.items():
            with self.subTest(expected=expected):
                bridge = SleeperDraftBridge(FakeClient(metadata(), payload))  # type: ignore[arg-type]
                with self.assertRaisesRegex(ValueError, expected):
                    bridge.synchronize(
                        "draft-test", "user-16", self.players, self.engine, self.config
                    )
                self.assertFalse(bridge.state["readiness"]["ready"])

    def test_mapping_threshold_duplicate_crosswalk_and_config_are_fail_closed(self):
        cases = [
            generated_players(99),
            [
                Player(
                    player.player_id,
                    player.name,
                    player.team,
                    player.position,
                    player.adp,
                    external_ids=(player.external_ids if index < 90 else {}),
                    projected_points_override=player.projected_points_override,
                    signals=player.signals,
                )
                for index, player in enumerate(self.players)
            ],
            [
                *self.players,
                Player(
                    "duplicate-local", "Duplicate", "ZZZ", "WR", 200,
                    external_ids={"sleeper": "sl-1"},
                ),
            ],
        ]
        for candidate_players in cases:
            with self.subTest(count=len(candidate_players)):
                bridge = SleeperDraftBridge(FakeClient(metadata(), []))  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    bridge.synchronize(
                        "draft-test", "user-16", candidate_players,
                        self.engine, self.config,
                    )
                self.assertFalse(bridge.state["connected"])

        superflex = league_config_for_profile(
            SUPERFLEX_REPLACES_FLEX, teams=16, user_slot=16
        )
        bridge = SleeperDraftBridge(FakeClient(metadata(), []))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "normal 1-QB"):
            bridge.synchronize(
                "draft-test", "user-16", self.players,
                DraftEngine(superflex), superflex,
            )

        wrong_slot = league_config_for_profile(STANDARD_PPR, teams=16, user_slot=1)
        bridge = SleeperDraftBridge(FakeClient(metadata(), []))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "does not match configured draft slot"):
            bridge.synchronize(
                "draft-test", "user-16", self.players,
                DraftEngine(wrong_slot), wrong_slot,
            )

    def test_pick_history_is_immutable_and_regressions_fail_closed(self):
        client = FakeClient(metadata(), picks(2))
        bridge = SleeperDraftBridge(client)  # type: ignore[arg-type]
        bridge.synchronize(
            "draft-test", "user-16", self.players, self.engine, self.config
        )
        client.picks_payload = picks(1)
        with self.assertRaisesRegex(ValueError, "regressed"):
            bridge.synchronize(
                "draft-test", "user-16", self.players, self.engine, self.config
            )
        self.assertFalse(bridge.state["connected"])

        client.picks_payload = picks(2)
        client.picks_payload[0]["player_id"] = "sl-20"
        with self.assertRaisesRegex(ValueError, "mutated"):
            bridge.synchronize(
                "draft-test", "user-16", self.players, self.engine, self.config
            )

    def test_network_errors_and_identifiers_are_actionable_and_fail_closed(self):
        class BrokenClient:
            def draft(self, draft_id):
                raise OSError("generated timeout")

            def picks(self, draft_id):  # pragma: no cover - must never be reached
                raise AssertionError("picks called after metadata failure")

        bridge = SleeperDraftBridge(BrokenClient())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "generated timeout"):
            bridge.synchronize(
                "draft-test", "user-16", self.players, self.engine, self.config
            )
        self.assertFalse(bridge.state["connected"])
        for identifier in ("", "../draft", "has space", "x" * 101):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    SleeperApiClient.validate_identifier(identifier, "draft_id")

    def test_api_client_uses_fixed_https_get_timeout_and_size_bound(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def getcode(self):
                return 200

            def read(self, amount):
                self.amount = amount
                return b'{"draft_id":"draft-test"}'

        class Opener:
            def __init__(self):
                self.response = Response()

            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return self.response

        opener = Opener()
        with patch("draft_agent.sleeper.build_opener", return_value=opener):
            result = SleeperApiClient(timeout_seconds=2.5).draft("draft-test")
        self.assertEqual(result, {"draft_id": "draft-test"})
        self.assertEqual(opener.request.full_url, "https://api.sleeper.app/v1/draft/draft-test")
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertEqual(opener.timeout, 2.5)
        self.assertEqual(opener.response.amount, 256_001)
        self.assertNotIn("Authorization", opener.request.headers)


if __name__ == "__main__":
    unittest.main()
