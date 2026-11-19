import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_calibration_datasets as datasets  # noqa: E402
import validate_pipeline as pipeline  # noqa: E402


class CalibrationDatasetTests(unittest.TestCase):
    def test_pregame_horizon_parser_rejects_duplicate_values(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            datasets.parse_pregame_horizons("60,60")

    def test_age_sensitivity_counts_observation_rejected_by_baseline_tolerance(self):
        row = {
            "price_observation_status": "price_exceeds_maximum_age",
            "observation_age_minutes": 45,
            "fixture_result_eligible": True,
            "token_mapping_status": "matched",
            "settlement_crosscheck_status": "matched",
            "timing_precision": "recorded_play_clock",
        }
        self.assertEqual(datasets.age_sensitivity_counts([row], 30), (0, 0))
        self.assertEqual(datasets.age_sensitivity_counts([row], 60), (1, 1))

    def test_espn_clock_stoppage_time_is_added_to_match_clock(self):
        self.assertEqual(
            pipeline.parse_clock_seconds({
                "period": {"number": 2},
                "clock": {"value": 5400, "displayValue": "90'+2'"},
            }),
            (2, 5520),
        )

    def test_match_minute_cutoff_is_before_final_play_and_labelled_in_progress(self):
        commentary = [
            {
                "text": "First Half begins.",
                "play": {
                    "period": {"number": 1},
                    "clock": {"value": 0, "displayValue": ""},
                    "wallclock": "2025-01-01T12:00:00Z",
                },
            },
            {
                "text": "A play at 30 minutes.",
                "play": {
                    "period": {"number": 1},
                    "clock": {"value": 1800, "displayValue": "30'"},
                    "wallclock": "2025-01-01T12:30:00Z",
                },
            },
            {
                "text": "Second-half event.",
                "play": {
                    "period": {"number": 2},
                    "clock": {"value": 4500, "displayValue": "75'"},
                    "wallclock": "2025-01-01T13:15:00Z",
                },
            },
            {
                "text": "Ninety-minute approach.",
                "play": {
                    "period": {"number": 2},
                    "clock": {"value": 5370, "displayValue": "90'"},
                    "wallclock": "2025-01-01T13:29:00Z",
                },
            },
            {
                "text": "Late event.",
                "play": {
                    "period": {"number": 2},
                    "clock": {"value": 5520, "displayValue": "90'+2'"},
                    "wallclock": "2025-01-01T13:31:00Z",
                },
            },
        ]
        cutoffs = pipeline.match_clock_cutoffs(commentary, ["30", "90"], 10)
        by_horizon = {row["forecast_horizon"]: row for row in cutoffs}
        self.assertEqual(by_horizon["ingame_30m"]["timing_status"], "match_clock_aligned")
        self.assertEqual(by_horizon["ingame_30m"]["timing_precision"], "recorded_play_clock")
        self.assertLess(
            pipeline.parse_iso(by_horizon["ingame_90m"]["requested_timestamp_utc"]),
            pipeline.parse_iso("2025-01-01T13:31:00Z"),
        )
        self.assertEqual(by_horizon["ingame_90m"]["in_progress_check"], "passed")

    def test_halftime_proxy_is_explicitly_approximate(self):
        commentary = [{
            "text": "Last first-half play.",
            "play": {
                "period": {"number": 1},
                "clock": {"value": 2700, "displayValue": "45'"},
                "wallclock": "2025-01-01T12:46:00Z",
            },
        }, {
            "text": "Second-half start.",
            "play": {
                "period": {"number": 2},
                "clock": {"value": 2700, "displayValue": "45'"},
                "wallclock": "2025-01-01T13:00:00Z",
            },
        }]
        row = pipeline.match_clock_cutoffs(commentary, ["halftime"], 10)[0]
        self.assertEqual(row["timing_precision"], "approximate")
        self.assertEqual(row["timing_status"], "halftime_proxy_last_first_half_play")

    def test_halftime_proxy_price_is_retained_but_not_calibration_eligible(self):
        snapshot = pipeline.build_forecast_snapshot(
            {"fixture_result_eligible": True},
            {
                "forecast_horizon": "ingame_halftime",
                "horizon_type": "in_game_match_clock",
                "requested_timestamp_utc": "2025-01-01T12:46:00Z",
                "timing_precision": "approximate",
                "timing_status": "halftime_proxy_last_first_half_play",
                "in_progress_check": "passed",
            },
            [{
                "observation_timestamp_utc": "2025-01-01T12:45:00Z",
                "probability": 0.3,
            }],
            10,
        )
        self.assertEqual(snapshot["price_observation_status"], "selected")
        self.assertEqual(snapshot["forecast_probability"], 0.3)
        self.assertFalse(snapshot["analysis_eligible"])
        self.assertEqual(snapshot["eligibility_reason"], "halftime_wallclock_is_proxy_only")

    def test_espn_only_result_is_eligible_when_settlement_matches_yes_or_no(self):
        for home_score, away_score, settlement in ((1, 1, 1), (2, 1, 0), (0, 3, 0)):
            with self.subTest(result=(home_score, away_score), settlement=settlement):
                result = datasets.fixture_result_record(
                    {
                        "league": "La Liga",
                        "event_id": "event",
                        "fixture_id": "fixture",
                        "market_id": "market",
                        "condition_id": "condition",
                        "yes_token_id": "yes-token",
                        "home_team": "Home FC",
                        "away_team": "Away FC",
                        "market_kickoff_utc": "2025-01-01T12:00:00Z",
                        "api_settled_draw": settlement,
                        "fixture_match_status": "exact_match_key",
                    },
                    {"token_mapping_status": "matched", "settlement_crosscheck_status": "matched"},
                    {
                        "score_match_status": "matched",
                        "completed": True,
                        "score_home": home_score,
                        "score_away": away_score,
                        "event_date": "2025-01-01T12:00:00Z",
                        "source": "ESPN score feed",
                        "raw_ref": "score.json",
                    },
                    None,
                    {"status": "not_queried"},
                    15,
                )
                self.assertTrue(result["fixture_result_eligible"])
                self.assertEqual(result["final_draw_label"], settlement)
                self.assertEqual(result["draw_label_source"], "ESPN completed score feed")
                self.assertIn(result["final_match_outcome"], {"home_win", "draw", "away_win"})

    def test_settlement_conflict_remains_explicit_and_ineligible(self):
        result = datasets.fixture_result_record(
            {
                "league": "La Liga",
                "event_id": "event",
                "fixture_id": "fixture",
                "market_id": "market",
                "condition_id": "condition",
                "yes_token_id": "yes-token",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "market_kickoff_utc": "2025-01-01T12:00:00Z",
                "api_settled_draw": 1,
                "fixture_match_status": "exact_match_key",
            },
            {"token_mapping_status": "matched", "settlement_crosscheck_status": "matched"},
            {
                "score_match_status": "matched",
                "completed": True,
                "score_home": 2,
                "score_away": 1,
                "event_date": "2025-01-01T12:00:00Z",
                "raw_ref": "score.json",
            },
            None,
            {"status": "not_queried"},
            15,
        )
        self.assertFalse(result["fixture_result_eligible"])
        self.assertIn("Polymarket_draw_settlement_conflicts_with_score", result["fixture_result_exclusion_reason"])

    def test_common_horizon_comparison_uses_only_complete_fixture_pairs(self):
        forecasts = []
        for fixture_id, outcome, early, closing in (
            ("fixture-a", 1, 0.2, 0.3),
            ("fixture-b", 0, 0.1, 0.15),
            ("fixture-c", 1, 0.4, None),
        ):
            forecasts.append({
                "fixture_id": fixture_id,
                "forecast_horizon": "pregame_1h",
                "horizon_type": "pregame",
                "analysis_eligible": True,
                "forecast_probability": early,
                "verified_draw": outcome,
            })
            if closing is not None:
                forecasts.append({
                    "fixture_id": fixture_id,
                    "forecast_horizon": "pregame_closing_5m",
                    "horizon_type": "pregame",
                    "analysis_eligible": True,
                    "forecast_probability": closing,
                    "verified_draw": outcome,
                })
        comparison = pipeline.common_horizon_comparison(
            forecasts,
            ["pregame_1h", "pregame_closing_5m"],
            "pregame_closing_5m",
            100,
            "unit-test",
        )
        self.assertEqual(comparison[0]["common_fixture_count"], 2)
        self.assertEqual(comparison[1]["paired_brier_difference_vs_closing"], 0)


if __name__ == "__main__":
    unittest.main()
