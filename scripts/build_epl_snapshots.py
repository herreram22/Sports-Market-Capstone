#!/usr/bin/env python3
"""Build all EPL snapshot rows from the frozen inventory, reusing cached API data."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import platform
import threading

import build_calibration_datasets as builder
import validate_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / 'data' / 'processed'
HORIZONS = ['15', '30', '45', '60', '75', '90']
VERSION = 'epl-v1'
_API_LOCKS = defaultdict(threading.Lock)
_LOCK_GUARD = threading.Lock()


class CachedAPI(pipeline.API):
    """Prevent concurrent workers from writing the same cached response."""
    def get(self, source, url, params):
        key = json.dumps([source, url, params], sort_keys=True)
        with _LOCK_GUARD:
            lock = _API_LOCKS[key]
        with lock:
            return super().get(source, url, params)


def csv_rows(path):
    with path.open(newline='', encoding='utf-8') as handle:
        yield from csv.DictReader(handle)


def load_inventory(path):
    """Read frozen market evidence; do not silently expand to newly listed fixtures."""
    rows = []
    for source in csv_rows(path):
        if source['league'] != 'Premier League':
            continue
        row = dict(source)
        for key in ('outcomes', 'token_ids', 'outcome_prices'):
            row[key] = json.loads(row[key])
        for key in ('orientation_valid', 'market_closed'):
            row[key] = row[key] == 'True'
        for key in ('yes_index', 'draw_market_count', 'api_settled_draw'):
            row[key] = int(row[key]) if row[key] else None
        rows.append(row)
    keys = [r['fixture_id'] for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('Inventory has duplicate fixture IDs; adjudicate before building')
    return sorted(rows, key=lambda r: (r['market_kickoff_utc'], r['fixture_id']))


def load_prices(path):
    groups = defaultdict(list)
    if not path.exists():
        return groups
    for row in csv_rows(path):
        if row['league'] != 'Premier League':
            continue
        row['probability'] = float(row['probability'])
        row['unix_timestamp_seconds'] = int(row['unix_timestamp_seconds'])
        groups[row['fixture_id']].append(row)
    return groups


def canonical_signature(row, args):
    return pipeline.digest(json.dumps({
        'version': VERSION, 'row': row, 'age': args.max_age_minutes,
        'clock_bracket': 5, 'wall_bracket': 10,
    }, sort_keys=True))


def validate_rows(rows, fixture_count):
    expected = {'pregame_30m', 'pregame_closing_5m', *('ingame_' + h + 'm' for h in HORIZONS)}
    groups = defaultdict(list)
    for row in rows:
        groups[row['fixture_id']].append(row)
        if row.get('forecast_probability') is not None:
            assert 0 <= row['forecast_probability'] <= 1
            assert pipeline.parse_iso(row['selected_observation_timestamp_utc']) <= pipeline.parse_iso(row['requested_timestamp_utc'])
            assert 0 <= row['observation_age_minutes'] <= row['max_lookback_minutes']
        if row.get('analysis_eligible'):
            assert row['final_draw_label'] in (0, 1)
            assert row['forecast_probability'] is not None
            assert row['final_draw_label'] == int(row['final_score_home'] == row['final_score_away'])
            assert row['token_mapping_status'] == 'matched'
            assert row['settlement_crosscheck_status'] == 'matched'
            if row['horizon_type'] == 'in_game_match_clock':
                assert row['in_progress_check'] == 'passed'
    assert len(groups) == fixture_count
    for group in groups.values():
        assert len(group) == 8
        assert {r['forecast_horizon'] for r in group} == expected
        assert len({r.get('final_draw_label') for r in group}) == 1
    return {'fixture_count': fixture_count, 'snapshot_rows': len(rows), 'checks_passed': True}


def process_fixture(row, args, old_prices):
    checkpoint = ROOT / 'data' / 'epl_checkpoints' / (canonical_signature(row, args) + '.json')
    if checkpoint.exists() and not args.retry_errors:
        return json.loads(checkpoint.read_text())
    if checkpoint.exists() and args.retry_errors:
        saved = json.loads(checkpoint.read_text())
        if not saved['errors']:
            return saved
    api = CachedAPI(delay=args.rate_delay)
    errors = []

    def attempt(label, fn, default):
        try:
            return fn()
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            errors.append({'stage': label, 'error': str(exc)})
            return default

    # Retain the frozen inventory but re-check its previously unresolved settlements.
    original_settlement = row.get('api_settled_draw')
    if original_settlement is None:
        market, ref = attempt('refresh_unresolved_market', lambda: api.get(
            'gamma_market', pipeline.GAMMA + '/markets/' + row['market_id'], {}), ({}, None))
        if str(market.get('id')) == row['market_id']:
            orient = pipeline.market_orientation(market)
            result, status = pipeline.settlement(market, orient)
            row = {**row, 'outcomes': orient['outcomes'], 'token_ids': orient['tokens'],
                   'outcome_prices': orient['outcome_prices'], 'yes_index': orient['yes_index'],
                   'yes_token_id': orient['yes_token_id'], 'orientation_valid': orient['orientation_valid'],
                   'api_settled_draw': result, 'settlement_status': status,
                   'uma_resolution_status': market.get('umaResolutionStatus'),
                   'market_closed': bool(market.get('closed')), 'source_raw_ref': ref}
    token = attempt('token_mapping', lambda: pipeline.audit_token_mapping(api, row), {})
    espn, espn_error = attempt('espn_result', lambda: pipeline.fetch_espn_result(api, row), (None, 'score_request_failed'))
    official = attempt('official_result', lambda: pipeline.fetch_pl_official(api, row), {})
    # Official season-wide matching must also agree on the date, not just teams.
    official_time = pipeline.parse_iso(official.get('kickoff_utc'))
    market_time = pipeline.parse_iso(row.get('market_kickoff_utc'))
    if official_time and market_time and abs((official_time-market_time).total_seconds()) > 900:
        official = {**official, 'status': 'kickoff_mismatch', 'score_home': None, 'score_away': None}
    facts = builder.fixture_result_record(row, token, espn, espn_error, official, 15)
    facts['inventory_settled_draw'] = original_settlement
    facts['sample_selection_rule'] = 'all fixtures in frozen EPL inventory; no sampling'
    # The legacy ESPN helper tolerates reversed teams. Explicitly flag that here.
    if espn and espn.get('score_match_status') == 'matched' and (
        pipeline.normalize_team(espn['home_team']) != pipeline.normalize_team(row['home_team']) or
        pipeline.normalize_team(espn['away_team']) != pipeline.normalize_team(row['away_team'])
    ):
        facts['fixture_result_eligible'] = False
        facts['fixture_result_exclusion_reason'] = ';'.join(filter(None, [facts.get('fixture_result_exclusion_reason'), 'ESPN_home_away_order_conflict']))
    if row['draw_market_count'] != 1 or not row['orientation_valid']:
        facts['fixture_result_eligible'] = False
        facts['fixture_result_exclusion_reason'] = ';'.join(filter(None, [facts.get('fixture_result_exclusion_reason'), 'draw_contract_identity_uncertain']))
    summary, summary_ref = (None, None)
    if espn and espn.get('score_match_status') == 'matched' and espn.get('completed'):
        summary, summary_ref = attempt('match_summary', lambda: pipeline.fetch_espn_summary(api, row['league'], str(espn['event_id'])), (None, None))
    facts['result_summary_raw_ref'] = summary_ref
    facts['result_verification_status'] = 'verified' if facts['fixture_result_eligible'] else 'uncertain_or_excluded'
    commentary = (summary or {}).get('commentary', [])
    for item in commentary:
        if 'first half begins' in str(item.get('text', '')).lower():
            actual = pipeline.parse_iso((item.get('play') or {}).get('wallclock'))
            facts['actual_kickoff_utc'] = actual.isoformat() if actual else None
            break
    requests = builder.build_requests(pipeline.parse_iso(facts['scheduled_kickoff_utc']), summary, [30, 5], 5, HORIZONS, 5, 10)
    targets = [pipeline.parse_iso(r.get('requested_timestamp_utc')) for r in requests]
    targets = [t for t in targets if t is not None]
    observations = old_prices.get(row['fixture_id'], [])
    observations = [o for o in observations if str(o['token_id']) == str(row['yes_token_id'])]
    # Reuse supplied histories when every requested cutoff has a sufficiently recent observation.
    if targets and any(pipeline.select_latest_before(observations, t, args.max_age_minutes)[0] is None for t in targets):
        fresh, _ = attempt('price_history', lambda: pipeline.collect_price_window(
            api, row, min(targets), max(targets), args.max_age_minutes), ([], None))
        by_time = {o['unix_timestamp_seconds']: o for o in observations}
        for observation in fresh:
            by_time.setdefault(observation['unix_timestamp_seconds'], observation)
        observations = sorted(by_time.values(), key=lambda o: o['unix_timestamp_seconds'])
    snapshots = []
    for request in requests:
        request['match_summary_raw_ref'] = summary_ref
        snap = pipeline.build_forecast_snapshot(facts, request, observations, args.max_age_minutes)
        snap['collection_errors'] = errors
        snapshots.append(snap)
    result = {'facts': facts, 'snapshots': snapshots, 'errors': errors}
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temp = checkpoint.with_suffix('.tmp')
    temp.write_text(json.dumps(result, indent=2) + '\n')
    temp.replace(checkpoint)
    return result


def output_row(row):
    keys = [
        'fixture_id', 'event_id', 'league', 'home_team', 'away_team', 'market_id', 'condition_id', 'draw_token_id',
        'scheduled_kickoff_utc', 'actual_kickoff_utc', 'forecast_horizon', 'horizon_type',
        'requested_match_minute', 'requested_horizon_minutes', 'requested_timestamp_utc',
        'selected_observation_timestamp_utc', 'forecast_probability', 'observation_age_minutes', 'max_lookback_minutes',
        'final_score_home', 'final_score_away', 'final_draw_label', 'api_settled_draw',
        'analysis_eligible', 'eligibility_reason', 'fixture_result_eligible', 'price_observation_status',
        'timing_status', 'timing_precision', 'clock_bracket_seconds', 'wallclock_bracket_seconds',
        'left_play_timestamp_utc', 'right_play_timestamp_utc', 'in_progress_check',
        'token_mapping_status', 'settlement_crosscheck_status', 'scheduled_kickoff_source', 'draw_label_source',
        'market_raw_ref', 'token_metadata_raw_ref', 'price_history_raw_ref', 'result_source_raw_ref',
        'result_summary_raw_ref', 'collection_errors',
    ]
    return {key: row.get(key) for key in keys}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', choices=['Premier League'], default='Premier League')
    parser.add_argument('--inventory', type=Path, default=PROCESSED / 'validation_evidence.csv')
    parser.add_argument('--historical-prices', type=Path, default=PROCESSED / 'historical_prices.csv')
    parser.add_argument('--limit', type=int, help='Chronological small-batch validation; omit to process all fixtures')
    parser.add_argument('--output-prefix', default='epl')
    parser.add_argument('--max-age-minutes', type=int, default=5)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--rate-delay', type=float, default=0.2)
    parser.add_argument('--retry-errors', action='store_true')
    args = parser.parse_args()
    if args.max_age_minutes < 1 or not 1 <= args.workers <= 4 or (args.limit is not None and args.limit < 1):
        parser.error('Positive age/limit and 1–4 workers required')
    if '/' in args.output_prefix or args.output_prefix in ('', '.', '..'):
        parser.error('Output prefix must be a filename prefix')
    inventory = load_inventory(args.inventory)
    total_inventory = len(inventory)
    if args.limit:
        inventory = inventory[:args.limit]
    old_prices = load_prices(args.historical_prices)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda row: process_fixture(row, args, old_prices), inventory):
            results.append(result)
            if len(results) % 10 == 0 or len(results) == len(inventory):
                print(f'Processed {len(results)}/{len(inventory)}; result eligible: {sum(r["facts"]["fixture_result_eligible"] for r in results)}', flush=True)
    rows = [s for r in results for s in r['snapshots']]
    checks = validate_rows(rows, len(inventory))
    pipeline.write_csv(PROCESSED / f'{args.output_prefix}_snapshots.csv', [output_row(r) for r in rows])
    pipeline.write_csv(PROCESSED / f'{args.output_prefix}_fixture_audit.csv', [r['facts'] for r in results])
    coverage = []
    for horizon in ['pregame_30m', 'pregame_closing_5m'] + ['ingame_' + h + 'm' for h in HORIZONS]:
        group = [r for r in rows if r['forecast_horizon'] == horizon]
        eligible = [r for r in group if r['analysis_eligible']]
        coverage.append({'horizon': horizon, 'fixtures': len(group), 'eligible': len(eligible),
                         'draws': sum(r['final_draw_label'] for r in eligible),
                         'non_draws': sum(1-r['final_draw_label'] for r in eligible),
                         'missing_price': sum(r['forecast_probability'] is None for r in group),
                         'excluded': len(group)-len(eligible)})
    pipeline.write_csv(PROCESSED / f'{args.output_prefix}_coverage.csv', coverage)
    eligible_sets = [{r['fixture_id'] for r in rows if r['analysis_eligible'] and r['forecast_horizon'] == c['horizon']} for c in coverage]
    reasons = Counter(r['eligibility_reason'] or 'eligible' for r in rows)
    errors = [{'fixture_id': r['facts']['fixture_id'], **e} for r in results for e in r['errors']]
    manifest = {
        'version': VERSION, 'generated_at_utc': pipeline.utc_now(), 'python': platform.python_version(),
        'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'inventory_sha256': hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
        'historical_prices_sha256': hashlib.sha256(args.historical_prices.read_bytes()).hexdigest() if args.historical_prices.exists() else None,
        'inventory_total': total_inventory, 'processed_fixtures': len(inventory),
        'max_observation_age_minutes': args.max_age_minutes, 'clock_bracket_minutes': 5, 'wallclock_bracket_minutes': 10,
        'common_fixtures_all_eight_horizons': len(set.intersection(*eligible_sets)),
        'raw_cache_note': 'Original relevant Drive caches reused; missing responses collected by the API helper. Paths in rows point to source envelopes with retrieval timestamps.',
        'validation': checks, 'exclusion_counts': dict(reasons), 'collection_errors': errors,
    }
    (PROCESSED / f'{args.output_prefix}_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    report = [f'# EPL snapshot coverage\n\nGenerated {manifest["generated_at_utc"]}.',
              f'\nFrozen inventory: {total_inventory} EPL fixtures. Processed: {len(inventory)}; rows: {len(rows)}.',
              f'\nResult-eligible fixtures: {sum(r["facts"]["fixture_result_eligible"] for r in results)}. Common eligible fixtures across all eight horizons: {manifest["common_fixtures_all_eight_horizons"]}.',
              '\n| Snapshot | Fixtures | Eligible | Draws | Non-draws | Missing price | Excluded |\n|---|---:|---:|---:|---:|---:|---:|']
    report += ['| ' + ' | '.join(str(c[k]) for k in ('horizon','fixtures','eligible','draws','non_draws','missing_price','excluded')) + ' |' for c in coverage]
    report += [f'\n## Selection and timing\n\nAll records in the frozen EPL inventory are accounted for; this is not a census of every EPL game. Probabilities use the latest API observation at or before the target, no more than {args.max_age_minutes} minutes old. Missing values are not zero. Scheduled kickoff anchors pregame times; actual match-clock times are reconstructed from commentary with at most 5-minute clock and 10-minute wallclock brackets. Minutes 45 and 90 are not halftime/final whistle. Interpolated timing remains approximate, and goals near cutoffs can make this uncertainty consequential.',
               '\n## Interpretation\n\nUse analysis_eligible=True and analyze each horizon separately. Match coverage changes between horizons; use a common-match subset for paired comparisons. Prices are API-reported draw-token values; trade/midpoint semantics are unspecified. Forecasting results do not establish executable trading profit. Original source files and five-league outputs are preserved.',
               '\n## Snapshot exclusion counts\n']
    report += [f'- {reason}: {n}' for reason,n in sorted(reasons.items())]
    report += [f'\nCollection errors: {len(errors)}. See the manifest for details. API errors can be retried with --retry-errors.']
    report_dir = ROOT / 'reports'; report_dir.mkdir(exist_ok=True)
    (report_dir / f'{args.output_prefix}_coverage_report.md').write_text('\n'.join(report) + '\n')
    print(json.dumps({'checks': checks, 'coverage': coverage, 'collection_error_count': len(errors)}, indent=2))


if __name__ == '__main__':
    main()
