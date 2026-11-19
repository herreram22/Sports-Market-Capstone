import sys
import unittest
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from validate_pipeline import (  # noqa: E402
    draw_market_reasons,
    calibration_rows,
    compare_token_orientation,
    fixture_keys,
    market_orientation,
    parse_iso,
    normalize_team,
    select_latest_before,
    settlement,
    split_event_title,
    team_names,
)


class ValidationPipelineTests(unittest.TestCase):
    def test_timezone_aware_timestamps_convert_to_utc(self):
        value = parse_iso("2025-02-08 15:00:00-05:00")
        self.assertEqual(value.isoformat(), "2025-02-08T20:00:00+00:00")
        self.assertEqual(value.tzinfo, timezone.utc)

    def test_naive_timestamp_is_not_assumed_utc(self):
        self.assertIsNone(parse_iso("2025-02-08 20:00:00"))

    def test_market_token_orientation_follows_yes_label_position(self):
        result = market_orientation({
            "outcomes": '["No", "Yes"]',
            "clobTokenIds": '["token-no", "token-yes"]',
            "outcomePrices": '["0", "1"]',
        })
        self.assertTrue(result["orientation_valid"])
        self.assertEqual(result["yes_index"], 1)
        self.assertEqual(result["yes_token_id"], "token-yes")

    def test_market_orientation_flags_length_mismatch(self):
        result = market_orientation({
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["only-one"]',
            "outcomePrices": '["0.5", "0.5"]',
        })
        self.assertFalse(result["orientation_valid"])
        self.assertIn("outcome_token_length_mismatch", result["orientation_issues"])
        self.assertIsNone(result["yes_token_id"])

    def test_gamma_array_pairing_matches_clob_named_outcomes(self):
        result = compare_token_orientation(
            ["No", "Yes"],
            ["token-no", "token-yes"],
            [
                {"outcome": "Yes", "token_id": "token-yes"},
                {"outcome": "No", "token_id": "token-no"},
            ],
        )
        self.assertEqual(result["status"], "matched")

    def test_draw_market_classification_excludes_period_and_penalties(self):
        self.assertTrue(draw_market_reasons({
            "question": "Will the match between A and B end in a draw?",
            "groupItemTitle": "Draw",
        }))
        self.assertFalse(draw_market_reasons({
            "question": "Will the match end in a draw at half time?",
            "groupItemTitle": "Draw",
        }))
        self.assertFalse(draw_market_reasons({
            "question": "Will the match end in a draw after penalties?",
            "groupItemTitle": "Draw",
        }))

    def test_title_fallback_extracts_ordered_teams(self):
        self.assertEqual(
            split_event_title("La Liga: Real Madrid vs. Atletico Madrid"),
            ("Real Madrid", "Atletico Madrid"),
        )

    def test_forecast_selection_uses_latest_not_future_observation(self):
        target = parse_iso("2025-02-08T19:00:00Z")
        observations = [
            {"observation_timestamp_utc": "2025-02-08T18:20:00Z", "probability": 0.2},
            {"observation_timestamp_utc": "2025-02-08T18:59:00Z", "probability": 0.3},
            {"observation_timestamp_utc": "2025-02-08T19:00:01Z", "probability": 0.9},
        ]
        selected, age = select_latest_before(observations, target, 5)
        self.assertEqual(selected["probability"], 0.3)
        self.assertEqual(age, 1)

    def test_forecast_selection_preserves_missing_when_age_exceeds_tolerance(self):
        target = parse_iso("2025-02-08T19:00:00Z")
        selected, age = select_latest_before(
            [{"observation_timestamp_utc": "2025-02-08T18:00:00Z"}],
            target,
            30,
        )
        self.assertIsNone(selected)
        self.assertEqual(age, 60)

    def test_api_settlement_requires_resolved_binary_complementary_prices(self):
        market = {
            "closed": True,
            "umaResolutionStatus": "resolved",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["y", "n"]',
            "outcomePrices": '["1", "0"]',
        }
        self.assertEqual(settlement(market, market_orientation(market)), (1, "resolved_binary_api_prices"))
        market["umaResolutionStatus"] = "proposed"
        self.assertEqual(settlement(market, market_orientation(market))[0], None)

    def test_duplicate_listings_share_only_exact_fixture_key(self):
        def listing(event_id):
            return {
                "league": "Premier League",
                "raw_ref": "raw.json",
                "event": {
                    "id": event_id,
                    "title": "Arsenal vs. Everton",
                    "eventDate": "2025-03-01",
                    "startTime": "2025-03-01T15:00:00Z",
                    "markets": [{
                        "question": "Will the match end in a draw?",
                        "groupItemTitle": "Draw",
                        "gameStartTime": "2025-03-01T15:00:00Z",
                    }],
                },
            }

        details, _ = fixture_keys([listing("one"), listing("two")])
        first = details[("Premier League", "one")]
        second = details[("Premier League", "two")]
        self.assertEqual(first["fixture_id"], second["fixture_id"])
        self.assertEqual(first["fixture_match_status"], "duplicate_listing_exact_key")

    def test_explicit_aliases_canonicalize_known_team_name_variants(self):
        self.assertEqual(normalize_team("Real Racing Club"), normalize_team("Racing Santander"))
        self.assertEqual(normalize_team("Club Atlético de Madrid"), normalize_team("Atlético Madrid"))

    def test_unordered_team_array_does_not_override_title_home_away_order(self):
        home, away, source = team_names({
            "title": "Nice vs. Saint-Etienne",
            "teams": [{"name": "AS Saint-Étienne"}, {"name": "OGC Nice"}],
        })
        self.assertEqual((home, away), ("Nice", "Saint-Etienne"))
        self.assertEqual(source, "event.title_order_structured_teams_corroborated")

    def test_configurable_bins_use_left_closed_right_open_boundaries(self):
        forecasts = [
            {"analysis_eligible": True, "forecast_probability": 0.5, "verified_draw": 1, "forecast_horizon": "pregame_1h", "league": "Premier League"},
        ]
        table, _ = calibration_rows(forecasts, [0.0, 0.5, 1.0])
        self.assertEqual(table[0]["bin_left"], 0.5)
        self.assertEqual(table[0]["bin_right"], 1.0)


if __name__ == "__main__":
    unittest.main()
