import json
import unittest

from draft_agent.models import Player
from draft_agent.signals import (
    SignalRecord,
    apply_signals,
    merge_sleeper_data,
    normalize_name,
    parse_consensus_csv,
    parse_id_crosswalk,
)


class SignalTests(unittest.TestCase):
    def test_normalizes_accents_punctuation_and_suffix(self):
        self.assertEqual(normalize_name("D'Andre Swift Jr."), "d andre swift")
        self.assertEqual(normalize_name("Amon-Ra St. Brown"), "amon ra st brown")

    def test_parses_redraft_consensus_and_crosswalk(self):
        ids = parse_id_crosswalk(
            "fantasypros_id,espn_id,sleeper_id,gsis_id\n123,456,789,00-1\n"
        )
        header = "page_type,player,id,pos,team,ecr,sd,best,worst,rank_delta,bye,scrape_date\n"
        rows = [
            f"redraft-overall,Player {number},123,WR,DET,{number},4,1,20,2,8,2026-08-01\n"
            for number in range(1, 101)
        ]
        records = parse_consensus_csv(header + "".join(rows), ids)
        self.assertEqual(len(records), 100)
        self.assertEqual(records[0].external_ids["espn"], "456")
        self.assertEqual(records[0].values["consensus_sd"], 4)

    def test_merges_sleeper_context_and_trends(self):
        record = SignalRecord("Example", "DET", "WR", {"sleeper": "789"})
        merge_sleeper_data(
            [record],
            {
                "789": {
                    "injury_status": "Questionable",
                    "depth_chart_order": 1,
                    "depth_chart_position": "LWR",
                    "years_exp": 0,
                    "age": 22,
                    "practice_participation": "Full",
                    "practice_description": "Full participant",
                    "espn_id": 456,
                }
            },
            [{"player_id": "789", "count": 25}],
            [{"player_id": "789", "count": 3}],
        )
        self.assertEqual(record.context["injury_status"], "Questionable")
        self.assertEqual(record.values["trend_adds_24h"], 25)
        self.assertEqual(record.values["years_exp"], 0)
        self.assertEqual(record.values["depth_chart_order"], 1)
        self.assertEqual(record.context["practice"], "Full")
        self.assertEqual(record.context["depth_chart_position"], "LWR")
        self.assertEqual(record.external_ids["espn"], "456")

    def test_rejects_non_object_sleeper_trend_entries(self):
        cases = (
            ("add", [1], []),
            ("drop", [], [None]),
        )
        for kind, adds, drops in cases:
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"Sleeper {kind} trend entry 0 must be an object",
                ):
                    merge_sleeper_data([], {}, adds, drops)

    def test_matches_espn_id_before_name_and_safe_name_fallback(self):
        players = [
            Player("one", "Changed Name", "DET", "WR", 10, external_ids={"espn": "456"}),
            Player("two", "D'Andre Swift Jr.", "CHI", "RB", 20),
        ]
        records = [
            SignalRecord("Original Name", "DET", "WR", {"espn": "456"}, {"consensus_rank": 7}),
            SignalRecord("D Andre Swift", "CHI", "RB", {}, {"consensus_rank": 19}),
        ]
        enriched, matched = apply_signals(players, records)
        self.assertEqual(matched, 2)
        self.assertEqual(enriched[0].signals["consensus_rank"], 7)
        self.assertEqual(enriched[1].signals["consensus_rank"], 19)

    def test_ambiguous_name_fallback_does_not_guess(self):
        player = Player("one", "Chris Smith", "FA", "WR", 99)
        records = [
            SignalRecord("Chris Smith", "AAA", "WR", values={"consensus_rank": 1}),
            SignalRecord("Chris Smith", "BBB", "WR", values={"consensus_rank": 2}),
        ]
        enriched, matched = apply_signals([player], records)
        self.assertEqual(matched, 0)
        self.assertEqual(enriched[0].signals, {})


if __name__ == "__main__":
    unittest.main()
