import unittest
from datetime import datetime, timezone

import pandas as pd

from scripts.soccer_goal_response_pilot import (
    extract_goals,
    fresh_bracket,
    goal_bounds,
    infer_half_start_from_kickoff,
    match_candidates,
    normalize_team,
    result_markets_by_outcome,
    resolve_half_start,
)
from scripts.build_soccer_goal_event_study_data import (
    extract_goal_events,
    get_goal_bounds,
)


class SoccerGoalResponsePilotTests(unittest.TestCase):
    def test_normalizes_common_fixture_team_suffixes(self):
        self.assertEqual(normalize_team("AFC Bournemouth"), normalize_team("Bournemouth"))
        self.assertEqual(normalize_team("Liverpool FC"), normalize_team("Liverpool"))

    def test_match_crosswalk_requires_both_ordered_teams_and_date_or_kickoff(self):
        fixture = {
            "home_team": "Liverpool FC",
            "away_team": "AFC Bournemouth",
            "event_date": "2025-08-15",
            "kickoff_utc": "2025-08-15T19:00:00Z",
        }
        candidates = [
            {
                "id": "1",
                "home": {"name": "Liverpool"},
                "away": {"name": "AFC Bournemouth"},
                "status": {"utcTime": "2025-08-15T19:00:00Z"},
            },
            {
                "id": "2",
                "home": {"name": "AFC Bournemouth"},
                "away": {"name": "Liverpool"},
                "status": {"utcTime": "2025-08-15T19:00:00Z"},
            },
            {
                "id": "3",
                "home": {"name": "Liverpool"},
                "away": {"name": "AFC Bournemouth"},
                "status": {"utcTime": "2025-08-20T19:00:00Z"},
            },
        ]
        self.assertEqual([item["id"] for item in match_candidates(fixture, candidates)], ["1"])

    def test_maps_separate_binary_result_markets_to_home_draw_away(self):
        fixture = {
            "home_team": "Liverpool FC",
            "away_team": "West Ham United FC",
        }

        def market(question, market_id):
            return {
                "id": market_id,
                "question": question,
                "outcomes": '["Yes","No"]',
                "clobTokenIds": f'["yes-{market_id}","no-{market_id}"]',
                "outcomePrices": '["0.5","0.5"]',
            }

        event = {
            "markets": [
                market("Will Liverpool FC win on 2026-02-28?", "home"),
                market("Will Liverpool FC vs. West Ham United FC end in a draw?", "draw"),
                market("Will West Ham United FC win on 2026-02-28?", "away"),
            ]
        }
        selected, statuses = result_markets_by_outcome(event, fixture)
        self.assertEqual({key: value["id"] for key, value in selected.items()}, {
            "home": "home",
            "draw": "draw",
            "away": "away",
        })
        self.assertEqual(set(statuses.values()), {"matched"})

    def test_extracts_first_goal_scoring_side_and_stoppage_minute(self):
        details = {
            "content": {
                "matchFacts": {
                    "events": {
                        "events": [
                            {
                                "eventId": 10,
                                "type": "Goal",
                                "time": 45,
                                "timeStr": "45 + 2",
                                "overloadTime": 2,
                                "homeScore": 0,
                                "awayScore": 0,
                                "newScore": [0, 1],
                                "isHome": False,
                                "shotmapEvent": {"period": "FirstHalf"},
                            },
                            {
                                "eventId": 11,
                                "type": "Goal",
                                "time": 50,
                                "newScore": [1, 1],
                                "homeScore": 0,
                                "awayScore": 1,
                                "shotmapEvent": {"period": "SecondHalf"},
                            },
                        ]
                    }
                }
            }
        }
        goals = extract_goals(details)
        self.assertEqual(goals[0]["event"]["eventId"], 10)
        self.assertEqual(goals[0]["period"], "FirstHalf")
        self.assertEqual(goals[0]["side"], "away")
        self.assertEqual(goals[0]["added_minute"], 2)

    def test_naive_half_start_is_not_assumed_utc(self):
        utc, status = resolve_half_start("15.08.2025 21:01:10", None, None)
        self.assertIsNone(utc)
        self.assertEqual(status, "half_start_timezone_unverified")
        utc, status = resolve_half_start(
            "15.08.2025 21:01:10", None, "Europe/London"
        )
        self.assertEqual(status, "manual_timezone")
        self.assertEqual(utc, datetime(2025, 8, 15, 20, 1, 10, tzinfo=timezone.utc))

    def test_infers_only_a_unique_offset_that_matches_both_utc_kickoffs(self):
        utc, offset, status = infer_half_start_from_kickoff(
            "15.08.2025 21:01:10",
            "2025-08-15T19:00:00Z",
            "2025-08-15T19:00:00.000Z",
        )
        self.assertEqual(offset, 120)
        self.assertEqual(utc, datetime(2025, 8, 15, 19, 1, 10, tzinfo=timezone.utc))
        self.assertIn("timezone_name_unavailable", status)

    def test_does_not_infer_when_the_offset_is_not_unique(self):
        utc, offset, status = infer_half_start_from_kickoff(
            "15.08.2025 21:15:00",
            "2025-08-15T19:00:00Z",
            "2025-08-15T19:00:00.000Z",
        )
        self.assertIsNone(utc)
        self.assertIsNone(offset)
        self.assertEqual(status, "half_start_timezone_unverified_nonunique_utc_offset")

    def test_goal_minute_maps_to_a_one_minute_interval(self):
        start = datetime(2025, 8, 15, 19, 1, tzinfo=timezone.utc)
        lower, upper = goal_bounds(start, 37, 0)
        self.assertEqual((upper - lower).total_seconds(), 60)
        self.assertEqual(lower, datetime(2025, 8, 15, 19, 37, tzinfo=timezone.utc))

    def test_fresh_bracket_requires_observations_strictly_outside_interval(self):
        start = datetime(2025, 8, 15, 19, 0, tzinfo=timezone.utc)
        end = datetime(2025, 8, 15, 19, 1, tzinfo=timezone.utc)
        rows = pd.DataFrame({
            "timestamp": pd.to_datetime([
                start.isoformat(),
                end.isoformat(),
            ], utc=True),
            "probability": [0.2, 0.3],
        })
        self.assertEqual(fresh_bracket(rows, start, end, 5), (None, None))
        rows = pd.DataFrame({
            "timestamp": pd.to_datetime([
                (start - pd.Timedelta(seconds=30)).isoformat(),
                (end + pd.Timedelta(seconds=30)).isoformat(),
            ], utc=True),
            "probability": [0.2, 0.3],
        })
        before, after = fresh_bracket(rows, start, end, 5)
        self.assertEqual(before["probability"], 0.2)
        self.assertEqual(after["probability"], 0.3)


class SoccerGoalEventStudyDataTests(unittest.TestCase):
    def test_retains_unaligned_goals_and_credits_own_goal_to_receiving_side(self):
        details = {
            "content": {
                "matchFacts": {
                    "events": {
                        "events": [
                            {
                                "eventId": 0,
                                "type": "Goal",
                                "time": None,
                                "timeStr": None,
                                "isHome": True,
                                "ownGoal": True,
                                "homeScore": 0,
                                "awayScore": 0,
                                "newScore": [0, 1],
                                "shotmapEvent": {"period": "FirstHalf", "isOwnGoal": True},
                            }
                        ]
                    }
                }
            }
        }
        goals, feed_present = extract_goal_events(details)
        self.assertTrue(feed_present)
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["event_id"], 0)
        self.assertEqual(goals[0]["side"], "away")
        self.assertIsNone(goals[0]["minute"])

    def test_maps_second_half_display_minute_from_second_half_clock_anchor(self):
        lower, upper, anchor_name, anchor, quality, independently_verified = get_goal_bounds(
            "SecondHalf",
            48,
            0,
            {"secondHalfStarted": "15.08.2025 20:00:00"},
            0,
            None,
        )
        self.assertEqual(anchor_name, "secondHalfStarted")
        self.assertEqual(lower, datetime(2025, 8, 15, 20, 2, tzinfo=timezone.utc))
        self.assertEqual(upper, datetime(2025, 8, 15, 20, 3, tzinfo=timezone.utc))
        self.assertEqual(quality, "kickoff_crosschecked_offset_inferred")
        self.assertFalse(independently_verified)

    def test_leaves_second_half_bounds_blank_when_anchor_conflicts_with_first_half(self):
        lower, upper, _, _, quality, _ = get_goal_bounds(
            "SecondHalf",
            48,
            0,
            {
                "firstHalfStarted": "15.08.2025 20:00:00",
                "firstHalfEnded": "15.08.2025 20:45:00",
                "secondHalfStarted": "15.08.2025 20:30:00",
            },
            0,
            None,
        )
        self.assertIsNone(lower)
        self.assertIsNone(upper)
        self.assertEqual(quality, "second_half_anchor_conflicts_with_first_half_start")


if __name__ == "__main__":
    unittest.main()
