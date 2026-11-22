import csv
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_epl_snapshots as epl
import build_calibration_datasets as builder
import validate_pipeline as pipeline


class EPLTests(unittest.TestCase):
    def test_requested_schedule_has_eight_rows_without_commentary(self):
        requests = builder.build_requests(datetime(2026, 1, 1, 15, tzinfo=timezone.utc), None, [30,5], 5, epl.HORIZONS, 5, 10)
        self.assertEqual(len(requests), 8)
        self.assertEqual(requests[0]['requested_timestamp_utc'], '2026-01-01T14:30:00+00:00')
        self.assertEqual(requests[1]['requested_timestamp_utc'], '2026-01-01T14:55:00+00:00')
        self.assertEqual([r['requested_match_minute'] for r in requests[2:]], [15,30,45,60,75,90])
        self.assertTrue(all('requested_timestamp_utc' not in r for r in requests[2:]))

    def test_zero_probability_is_preserved_and_missing_is_not_zero(self):
        target = '2026-01-01T15:00:00+00:00'
        facts = {'fixture_result_eligible': True}
        request = {'requested_timestamp_utc': target, 'timing_status': 'scheduled_kickoff_relative'}
        obs = [{'observation_timestamp_utc': target, 'probability': 0.0}]
        row = pipeline.build_forecast_snapshot(facts, request, obs, 5)
        self.assertEqual(row['forecast_probability'], 0)
        self.assertTrue(row['analysis_eligible'])
        missing = pipeline.build_forecast_snapshot(facts, request, [], 5)
        self.assertIsNone(missing['forecast_probability'])
        self.assertFalse(missing['analysis_eligible'])

    def test_age_boundary_and_future_price(self):
        target = datetime(2026,1,1,15,tzinfo=timezone.utc)
        obs = [{'observation_timestamp_utc':'2026-01-01T14:55:00+00:00','probability':0.2},
               {'observation_timestamp_utc':'2026-01-01T15:00:01+00:00','probability':0.9}]
        selected,age = pipeline.select_latest_before(obs,target,5)
        self.assertEqual(selected['probability'],0.2)
        self.assertEqual(age,5)
        self.assertIsNone(pipeline.select_latest_before(obs,target,4)[0])

    def test_minute_45_does_not_use_second_half_clock(self):
        def play(period, minute, wall):
            return {'play':{'period':{'number':period},'clock':{'value':minute*60},'wallclock':wall}}
        plays=[play(1,44,'2026-01-01T15:44:00+00:00'),play(1,46,'2026-01-01T15:46:00+00:00'),play(2,46,'2026-01-01T16:02:00+00:00')]
        row=pipeline.match_clock_cutoffs(plays,['45'],5,10)[0]
        self.assertEqual(row['requested_timestamp_utc'],'2026-01-01T15:45:00+00:00')
        self.assertEqual(row['in_progress_check'],'passed')

    def test_duplicate_fixture_inventory_rejected(self):
        row={'league':'Premier League','fixture_id':'same','market_kickoff_utc':'2026-01-01T15:00:00+00:00',
             'outcomes':'["Yes","No"]','token_ids':'["a","b"]','outcome_prices':'["1","0"]',
             'orientation_valid':'True','market_closed':'True','yes_index':'0','draw_market_count':'1','api_settled_draw':'1'}
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'inventory.csv'
            with file.open('w') as out:
                writer=csv.DictWriter(out,fieldnames=list(row));writer.writeheader();writer.writerows([row,row])
            with self.assertRaises(ValueError):epl.load_inventory(file)

    def test_validation_requires_all_horizons_even_when_missing(self):
        rows=[{'fixture_id':'f','forecast_horizon':h,'analysis_eligible':False,'forecast_probability':None}
              for h in ['pregame_30m','pregame_closing_5m']+['ingame_'+h+'m' for h in epl.HORIZONS]]
        self.assertTrue(epl.validate_rows(rows,1)['checks_passed'])
        with self.assertRaises(AssertionError):epl.validate_rows(rows[:-1],1)


if __name__=='__main__':unittest.main()
