# EPL snapshot dataset

`data/processed/epl_snapshots.csv` is the analysis handoff. Each row represents one fixture at one requested forecast time. It covers the frozen Premier League inventory in `data/processed/validation_evidence.csv`, including records that fail validation. This inventory is a set of Polymarket-listed fixtures, not a complete census of all Premier League games.

## Start here for calibration

1. Read identifiers as strings so long token IDs are not rounded. Read probabilities as numbers; empty fields are missing, not zero. The CSV writes booleans as `True` and `False`.
2. Select rows with `analysis_eligible` equal to `True`.
3. Select **one** `forecast_horizon`, bin `forecast_probability`, and compare its bin mean against the mean of `final_draw_label`. Include the number of distinct fixtures and uncertainty intervals in each bin.
4. Do not pool all eight rows per match as independent observations. When comparing horizons, either report changing sample coverage or use fixtures eligible at each compared horizon. Fixture-level resampling preserves repeated observations in uncertainty calculations.

No bins are assigned in the source dataset; bin edges are an analysis decision. The final score and draw label are outcomes, not inputs to any future predictive model.

## Snapshot definitions

| Label | Target |
|---|---|
| `pregame_30m` | 30 minutes before independent scheduled kickoff |
| `pregame_closing_5m` | 5 minutes before independent scheduled kickoff; an operational cutoff, not exchange close |
| `ingame_15m` | Match clock 15:00 |
| `ingame_30m` | Match clock 30:00 |
| `ingame_45m` | Match clock 45:00 in the first half, not halftime |
| `ingame_60m` | Match clock 60:00 in the second half |
| `ingame_75m` | Match clock 75:00 in the second half |
| `ingame_90m` | Match clock 90:00 in the second half, not the final whistle |

Match-clock cutoffs are mapped to UTC using ESPN commentary. Interpolation uses two plays within the same half, at most five match minutes and ten wallclock minutes apart. A later timestamped play must show the game was still in progress. Clock alignment is retrospective and can remain uncertain around goals; `timing_precision` identifies estimates. Missing timing is not replaced by kickoff plus a fixed number of elapsed minutes.

At each cutoff the builder selects the latest historical observation at or before that timestamp. The default maximum observation age is **five minutes**, for both pregame and in-game snapshots. This is stricter than the older five-league pipeline's six-hour/ten-minute limits. Values are Polymarket API-reported draw-token prices on a 0–1 scale, not proven executable quotes. No three-outcome normalization is applied. API observation age is not evidence of the age of the underlying trade.

## Columns

| Columns | Meaning |
|---|---|
| `fixture_id` | Stable internal fixture key; combine with `forecast_horizon` for the row key |
| `event_id`, `market_id`, `condition_id`, `draw_token_id` | Polymarket identifiers; distinct namespaces; preserve as strings |
| `league`, `home_team`, `away_team` | Fixture identity from the frozen inventory |
| `scheduled_kickoff_utc`, `scheduled_kickoff_source` | Scheduled time and its source; eligible results require independent kickoff verification |
| `actual_kickoff_utc` | Actual start only when a timestamped first-half-begins commentary record exists; otherwise missing |
| `forecast_horizon`, `horizon_type` | Snapshot label and pregame/in-game category |
| `requested_match_minute` | In-game minute; missing for pregame rows |
| `requested_horizon_minutes` | Minutes **before** kickoff for pregame rows; missing in-game |
| `requested_timestamp_utc` | Target UTC timestamp; may be missing when in-game alignment fails |
| `selected_observation_timestamp_utc` | Timestamp of the selected historical price |
| `forecast_probability` | Quoted probability of a final full-match draw, 0–1; empty if unusable/missing |
| `observation_age_minutes`, `max_lookback_minutes` | Selected observation's age and the permitted maximum |
| `final_score_home`, `final_score_away` | Completed sporting score; missing when not verified |
| `final_draw_label` | Sporting result: draw=1, non-draw=0; missing means unknown |
| `api_settled_draw` | Separate Polymarket binary settlement cross-check |
| `analysis_eligible` | Use only `True` rows for initial calibration |
| `eligibility_reason` | Main exclusion reason(s); empty for eligible rows |
| `fixture_result_eligible` | Fixture passes identity, kickoff, score, token, and settlement checks; a snapshot can still fail |
| `price_observation_status` | Price selected, missing, too old, invalid, or timing not usable |
| `timing_status`, `timing_precision` | Clock alignment outcome and whether timing was recorded or interpolated |
| `clock_bracket_seconds`, `wallclock_bracket_seconds` | Distance between the commentary plays used for timing |
| `left_play_timestamp_utc`, `right_play_timestamp_utc` | Commentary timestamps bracketing an in-game cutoff |
| `in_progress_check` | Whether a later timestamped play supports that the game was ongoing |
| `token_mapping_status`, `settlement_crosscheck_status` | Agreement between Polymarket Gamma and named CLOB outcomes/settlements |
| `draw_label_source` | Independent sporting-result source |
| `market_raw_ref`, `token_metadata_raw_ref` | Local raw-response paths for contract and token evidence |
| `price_history_raw_ref` | Raw response containing the selected price |
| `result_source_raw_ref`, `result_summary_raw_ref` | Raw score and commentary evidence |
| `collection_errors` | JSON list of API-stage errors; optional-source errors can coexist with an otherwise eligible row |

Missing fields and exclusions are retained so coverage can be audited. A populated probability is not, by itself, permission to use the row: the outcome or fixture checks may have failed.

## Related outputs

- `epl_fixture_audit.csv`: one record per fixture, including result sources and exclusion reasons.
- `epl_coverage.csv`: eligible matches, draws/non-draws, exclusions, and missing prices at each horizon.
- `epl_manifest.json`: input hashes, generation timestamp, settings, checks, and collection errors.
- `reports/epl_coverage_report.md`: readable coverage and methodological limitations.
- `epl_pilot_*`: retained ten-match smoke-test outputs, not the full analysis dataset.

## Reproduce or resume

Run from the repository root. Python 3.12 was used for this build.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-epl-lock.txt
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -B scripts/build_epl_snapshots.py --limit 10 --output-prefix epl_pilot
.venv/bin/python -B scripts/build_epl_snapshots.py
```

The default run processes the entire frozen EPL inventory, without a sample limit. It reuses `data/processed/historical_prices.csv` when present and sufficient, then requests missing data. Raw responses live in ignored `data/raw/`. Existing source files and five-league outputs are not overwritten.

Completed fixture results are saved under ignored `data/epl_checkpoints/`. Re-running resumes those results. Use `--retry-errors` to revisit checkpointed fixtures with API errors. This retries requests; it does not automatically refresh successful historical cached responses. Preserve the cache for exact replay. A future update of unresolved fixtures or changed source data needs an explicit refresh/versioning decision.

Defaults: `--league 'Premier League'`, `--max-age-minutes 5`, `--workers 3`, `--rate-delay 0.2`. Changing the age threshold gives separate checkpoint signatures; use a distinct `--output-prefix` to retain sensitivity results. The original inventory includes future/unresolved fixtures as of its collection date; those remain excluded unless independently resolved and validated.

## Sharing

Commit the EPL CSVs, manifest, script, tests, and documentation to Git. Keep `.venv/`, `data/raw/`, `data/epl_checkpoints/`, and the large historical-price files out of Git. Pablo's source archive is available at https://drive.google.com/drive/folders/1QIaHTODkMnJ_pRf2CfQikcjNwutsEkxT . Save that archive with the report to support future reproduction if remote APIs change.
