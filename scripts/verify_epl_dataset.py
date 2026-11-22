#!/usr/bin/env python3
"""Independent read-only checks of the saved EPL CSV against original responses."""
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify():
    rows = list(csv.DictReader((ROOT/'data/processed/epl_snapshots.csv').open()))
    fixtures = list(csv.DictReader((ROOT/'data/processed/fixtures.csv').open()))
    expected = {r['fixture_id'] for r in fixtures if r['league']=='Premier League'}
    assert {r['fixture_id'] for r in rows} == expected
    assert len(rows)==8*len(expected)
    assert len({(r['fixture_id'],r['forecast_horizon']) for r in rows})==len(rows)
    cache = {}
    def payload(ref):
        assert ref, 'missing raw reference'
        if ref not in cache:
            cache[ref] = json.loads((ROOT/ref).read_text())['payload']
        return cache[ref]
    probabilities = 0
    score_checks = set()
    references = set()
    for row in rows:
        for key,value in row.items():
            if key.endswith('_raw_ref') and value:
                assert (ROOT/value).is_file(), value
                references.add(value)
        if row['forecast_probability']:
            cutoff=datetime.fromisoformat(row['requested_timestamp_utc']).timestamp()
            selected=datetime.fromisoformat(row['selected_observation_timestamp_utc']).timestamp()
            raw=payload(row['price_history_raw_ref'])
            history=raw.get('history',[])
            before=[r for r in history if r['t']<=cutoff]
            latest=max(before,key=lambda r:r['t'])
            assert selected==latest['t']
            assert float(row['forecast_probability'])==float(latest['p'])
            assert 0<=cutoff-selected<=float(row['max_lookback_minutes'])*60
            probabilities+=1
        if row['analysis_eligible']=='True':
            assert row['forecast_probability']!=''
            assert int(row['final_draw_label'])==int(int(row['final_score_home'])==int(row['final_score_away']))
            assert row['final_draw_label']==row['api_settled_draw']
            if row['fixture_id'] not in score_checks:
                data=payload(row['result_source_raw_ref'])
                if row['draw_label_source']=='ESPN completed score feed':
                    matches=[]
                    for event in data.get('events',[]):
                        teams=(event.get('competitions') or [{}])[0].get('competitors',[])
                        home=next((t for t in teams if t.get('homeAway')=='home'),None)
                        away=next((t for t in teams if t.get('homeAway')=='away'),None)
                        if not home or not away:continue
                        # The summary header independently identifies the matched event.
                        summary_ref=row['result_summary_raw_ref']
                        if summary_ref and str(event['id'])==str(payload(summary_ref)['header']['id']):
                            assert event['status']['type']['completed'] is True
                            assert int(home['score'])==int(row['final_score_home'])
                            assert int(away['score'])==int(row['final_score_away'])
                            matches.append(event['id'])
                    assert len(matches)==1
                score_checks.add(row['fixture_id'])
    eligible=[r for r in rows if r['analysis_eligible']=='True']
    common=set.intersection(*[{r['fixture_id'] for r in eligible if r['forecast_horizon']==h} for h in {r['forecast_horizon'] for r in rows}])
    result={'inventory_fixtures':len(expected),'rows':len(rows),'eligible_rows':len(eligible),
            'raw_price_observations_checked':probabilities,'verified_score_fixtures':len(score_checks),
            'raw_references_present':len(references),'common_fixtures_all_horizons':len(common),
            'exclusion_counts':dict(Counter(r['eligibility_reason'] for r in rows if r['analysis_eligible']!='True')),
            'passed':True}
    print(json.dumps(result,indent=2))
    return result


if __name__=='__main__':verify()
