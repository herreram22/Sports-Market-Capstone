# Sports-Market-Capstone

## EPL analysis dataset

The EPL-specific builder processes all 490 fixture records in the frozen inventory and writes eight rows per fixture: 30 and 5 minutes before kickoff, then match minutes 15, 30, 45, 60, 75, and 90. Read [the dataset guide](reports/epl_dataset_guide.md) and [coverage report](reports/epl_coverage_report.md) before analysis. Filter `analysis_eligible=True`; missing values and exclusions remain in the file.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-epl-lock.txt
.venv/bin/python -B scripts/build_epl_snapshots.py
.venv/bin/python -B scripts/verify_epl_dataset.py
.venv/bin/python -B -m unittest discover -s tests -v
```

The final handoff is `data/processed/epl_snapshots.csv`. The builder reuses supplied historical prices and raw caches, fetches missing responses, and resumes completed fixtures from ignored checkpoints. Five-league outputs below are preserved. The original source archive is linked in the guide.

### EPL calibration notebook

[`notebooks/epl_snapshot_analysis.ipynb`](notebooks/epl_snapshot_analysis.ipynb) checks how well Polymarket draw prices match actual EPL draw rates at each forecast horizon. It:

1. Loads `epl_snapshots.csv` with pandas, orders the eight horizons chronologically, and reports row, eligibility, and price coverage per horizon.
2. Keeps only `analysis_eligible=True` rows. Each fixture appears at most once per horizon, so every horizon is analysed separately rather than pooling a match's eight rows.
3. Places each observation in a probability bin by its quoted draw price. Bins include their lower edge and exclude their upper edge; a price of exactly 1.0 goes in the last bin.
4. For each set of bins, draws two 2×4 grids of charts, one panel per horizon:
   - **Distribution:** number of matches in each bin.
   - **Calibration:** the share of each bin's matches that ended in a draw, plotted against the bin's mean market probability, with the y = x line for perfect calibration. Error bars are 95% Wilson intervals, each point is labelled with its match count, and bins with fewer than 20 matches are hollow and left off the connecting line.
5. Reports per-bin tables (matches, draws, mean market probability, actual draw rate, interval, gap) and per-horizon Brier score and expected calibration error (ECE).

It runs two binnings:

| Analysis | Bin edges (%) |
|---|---|
| 10-point bins | 0, 10, 20, …, 90, 100 |
| Custom bins | 0, 5, 15, 25, 40, 50, 65, 75, 85, 100 |

A final table compares them. Brier score is the same under both because it uses each match's own price; ECE depends on the binning.

The logic lives in reusable functions in the notebook's toolkit cell. To try other bins, pass a new list of edges from 0 to 1:

```python
results = run_calibration_analysis(eligible, [0, 0.2, 0.3, 0.5, 1.0], "my bins")
results["calibration"]  # also "counts", "summary", "binned"
```

Run it with an environment that has `requirements.txt` installed (pandas and matplotlib). Calibration plots show calibration only; how well prices separate draws from non-draws is better read from the distribution charts and the Brier score. Sample sizes differ by horizon (392–486 matches), so compare horizons with that coverage in mind.


## Reproducible calibration datasets

The initial notebook's **2,334** figure is a count of listings with draw markets, not verified unique fixtures. The calibration-data builder below reuses the Polymarket ingestion, market-orientation, and ESPN result helpers in `scripts/validate_pipeline.py`. It creates connected fixture, historical-price, and forecast-snapshot tables. ESPN completed scores can qualify without an official competition feed; an official feed is an optional cross-check. Sporting-score draw labels and Polymarket Yes/No settlement labels remain separate.

```bash
python3 -m pip install -r requirements.txt
python3 scripts/build_calibration_datasets.py
python3 -m unittest discover -s tests -v
```

The default reproducible sample is up to 50 resolved, unambiguous draw-contract fixtures per league, selected by a stable hash seed. It requests pregame horizons at 24h, 6h, 1h, 30m, and 5m before scheduled kickoff; 5m is the project's operational closing-forecast cutoff. In-game horizons are 15, 30, halftime, 60, 75, and 90 match-clock minutes where ESPN commentary supports timing. The default maximum observation age is six hours pregame and ten minutes in-game. Match-clock interpolation is limited by default to 5-minute clock brackets and 10-minute wallclock brackets. Options include `--sample-per-league`, `--seed`, `--pregame-horizons`, `--closing-horizon`, `--ingame-horizons`, `--max-lookback-minutes`, `--max-in-game-age-minutes`, `--max-clock-bracket-minutes`, `--max-wallclock-bracket-minutes`, `--kickoff-tolerance-minutes`, `--rate-delay`, and `--bin-edges`. The 90-minute snapshot is not the final whistle; it is emitted only when commentary brackets the requested match clock and a later timestamped play supports that the match was still in progress. Halftime uses the last timestamped first-half play as an approximate proxy because the inspected ESPN half-end marker did not include a wallclock; this proxy is retained for audit but excluded from calibration.

All API responses are cached with request parameters, retrieval time, source, status, and response metadata. Retries are bounded to four attempts with exponential backoff; HTTP 429 and 5xx responses are retried, honoring `Retry-After` up to ten seconds. `--refresh` preserves earlier snapshots while taking new ones. Pagination pages and individual API requests are cached, so interrupted runs resume from cached responses. Use `--max-pages` only for smoke tests, not coverage counts.

### Persisted outputs

- `data/processed/fixtures.csv`: fixture keys, league, home/away teams, API and independent scheduled kickoff, actual kickoff where available, final scores, home/draw/away outcome, binary draw label, source references, and eligibility status.
- `data/processed/historical_prices.csv`: API timestamped historical prices with fixture, market, condition, and draw-token identifiers. `price_observations.csv` is retained as a compatibility copy.
- `data/processed/forecast_snapshots.csv`: one row per selected fixture and configured pregame/in-game horizon, including requested cutoff, selected observation, age, probability, final draw label, and eligibility/missing reason. `forecasts.csv` is a compatibility copy.
- `data/processed/snapshot_coverage.csv`: counts by league and forecast horizon, including missing prices, unresolved outcomes, uncertain timing, and fixture exclusions.
- `data/processed/forecast_age_sensitivity.csv`: forecast availability at tighter maximum observation-age thresholds.
- `data/processed/in_game_timing.csv`: ESPN clock-to-wallclock alignment evidence and uncertainty for in-game horizons.
- `data/processed/horizon_comparison.csv`: common-fixture pregame Brier-score comparisons with paired bootstrap intervals resampling whole fixtures.
- `data/processed/fixture_result_audit.csv`: sampled fixture identity, ESPN score, optional official cross-check, Polymarket settlement comparison, and exclusion reasons.
- `data/processed/outcome_discrepancies.csv`: unresolved scores and explicit score/settlement or cross-provider conflicts for review.
- `data/processed/calibration.csv`, `calibration_metrics.csv`, and `reliability.png`: exploratory per-horizon draw calibration with bin counts, draw/non-draw counts, Wilson intervals, and Brier/log loss.
- `reports/calibration_data_report.md`: sample definition, coverage, supported horizons, calibration summaries, and limitations.
- `data/processed/listings.csv`: fetched event listings. A listing is not a unique fixture.
- `data/processed/fixture_candidates.csv`: same-team/date-nearby cases with secondary-score-feed evidence; these are not automatically merged.
- `data/processed/validation_evidence.csv`: draw-market identity, token/outcome alignment, rules, API settlement, and raw evidence references.
- `data/processed/token_orientation_audit.csv`: cross-checks Gamma outcome/token arrays against named CLOB outcomes and resolved token values for sampled markets.
- `data/processed/validation_checks.json`: row uniqueness, future-observation leakage, probability, and selected-orientation assertions.
- `data/processed/price_api_experiments.json` and `reports/validation_report.md`: earlier API-semantics and listing audit; rebuild that audit with `python3 scripts/validate_pipeline.py`.

Raw API files are intentionally git-ignored; processed CSV/JSON, the plot, and generated reports are retained in the workspace. Each evidence row points to its cached raw response. Gamma market/event IDs are distinct identifier namespaces. `/prices-history` values are retained as API-reported historical prices; the available API documentation does not identify their underlying trade/midpoint/bucket semantics, so observation age is not proven price staleness.

Fixture matching requires league, ordered normalized teams, and UTC kickoff date when available. It does not merge different dates as reschedules; those candidates require review. ESPN is accepted as the score source when teams match unambiguously, the event is complete, and scores are available. Where the Premier League feed is accessible it is used only to detect conflicts, not as a qualification requirement. Fixtures with unresolved identity, result, kickoff, token orientation, or settlement disagreement remain in the output but are excluded from calibration. The sample is restricted to resolved full-match draw contracts and should not be treated as a census of all fixtures.