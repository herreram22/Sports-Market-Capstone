# EPL snapshot coverage

Generated 2026-11-21T18:41:38.265003+00:00.

Frozen inventory: 490 EPL fixtures. Processed: 490; rows: 3920.

Result-eligible fixtures: 486. Common eligible fixtures across all eight horizons: 261.

| Snapshot | Fixtures | Eligible | Draws | Non-draws | Missing price | Excluded |
|---|---:|---:|---:|---:|---:|---:|
| pregame_30m | 490 | 486 | 133 | 353 | 3 | 4 |
| pregame_closing_5m | 490 | 486 | 133 | 353 | 3 | 4 |
| ingame_15m | 490 | 407 | 113 | 294 | 83 | 83 |
| ingame_30m | 490 | 422 | 113 | 309 | 68 | 68 |
| ingame_45m | 490 | 392 | 108 | 284 | 98 | 98 |
| ingame_60m | 490 | 451 | 117 | 334 | 39 | 39 |
| ingame_75m | 490 | 467 | 130 | 337 | 23 | 23 |
| ingame_90m | 490 | 443 | 118 | 325 | 47 | 47 |

## Selection and timing

All records in the frozen EPL inventory are accounted for; this is not a census of every EPL game. Probabilities use the latest API observation at or before the target, no more than 5 minutes old. Missing values are not zero. Scheduled kickoff anchors pregame times; actual match-clock times are reconstructed from commentary with at most 5-minute clock and 10-minute wallclock brackets. Minutes 45 and 90 are not halftime/final whistle. Interpolated timing remains approximate, and goals near cutoffs can make this uncertainty consequential.

## Interpretation

Use analysis_eligible=True and analyze each horizon separately. Match coverage changes between horizons; use a common-match subset for paired comparisons. Prices are API-reported draw-token values; trade/midpoint semantics are unspecified. Forecasting results do not establish executable trading profit. Original source files and five-league outputs are preserved.

## Snapshot exclusion counts

- clock_bracket_too_wide: 282
- completed_sporting_result_unavailable;unresolved_or_nonbinary_draw_contract;CLOB_settlement_not_confirmed: 32
- eligible: 3554
- price_exceeds_maximum_age: 3
- target_not_bracketed_by_commentary: 49

Collection errors: 0. See the manifest for details. API errors can be retried with --retry-errors.
