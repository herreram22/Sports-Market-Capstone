# Validation pilot report

Generated 2026-11-12T20:07:56.480934+00:00. This report is rebuilt by `python3 scripts/validate_pipeline.py`.

## Coverage

The first count is live series-event listings retrieved, not confirmed unique fixtures. Fixture IDs below are conservative exact keys built from league, normalized ordered team names, and UTC kickoff date; nearby same-team dates are flagged, not merged, because they may be rescheduled fixtures or separate meetings.

| League | Retrieved listings | Exact fixture keys with draw listing | API-binary resolved draw markets | Analysis-eligible forecasts |
|---|---:|---:|---:|---:|
| La Liga | 2397 | 511 | 500 | 0 |
| Ligue 1 | 1875 | 406 | 397 | 0 |
| Bundesliga | 1586 | 397 | 388 | 0 |
| Serie A | 2248 | 499 | 489 | 0 |
| Premier League | 2156 | 490 | 480 | 5 |

### Listing-level validation flags

| League | Ambiguous draw-market listings | Orientation failures | Missing/unresolved API settlement | Missing timezone-aware kickoff | Nearby same-team date candidates |
|---|---:|---:|---:|---:|---:|
| La Liga | 0 | 0 | 11 | 0 | 2 |
| Ligue 1 | 0 | 0 | 9 | 0 | 2 |
| Bundesliga | 0 | 0 | 9 | 0 | 0 |
| Serie A | 0 | 0 | 10 | 0 | 0 |
| Premier League | 0 | 0 | 10 | 0 | 0 |

Total draw listings: 2303. API-binary resolved draw markets: 2254. Eligible independent forecasts: 5. Conservative exact fixture keys: 2303. Possible nearby reschedule/duplicate pairs flagged, not merged: 2 (same ESPN event IDs across both date queries: 0). The historical notebook's 2,334 remains a listing count and is not overwritten by this run.

| Audit gap | Status | Evidence and limitation |
|---|---|---|
| Listing-to-fixture identity | unresolved | Conservative league/team/date keys are available; nearby pairs are not merged without authoritative reschedule evidence. |
| Outcome market and token orientation | verified for pilot | Gamma arrays match named CLOB outcome-token mappings and resolved winners/prices for 25 of 25 pilot markets; the full listing universe is not cross-checked. |
| Kickoff, teams, and sporting outcome | verified for eligible subset; blocked elsewhere | Official Premier League feed verifies 5 pilot results; other leagues have secondary-only or missing result validation. |
| Contract settlement rules | partially verified | Listing-specific rule text and API resolution metadata are preserved, but unspecific edge cases and external settlement authority remain unresolved. |
| Historical price meaning | unresolved | Docs and bounded/overlap experiments establish API behavior but not whether prices are trades, midpoints, or timestamped original events. |
| One-hour forecast selection | verified for pilot | Latest observation at/before cutoff selected with no future leakage; interpretation of observation age remains limited by undocumented sampling semantics. |

### Forecast-pilot exclusions and age sensitivity

| League | Exclusion reason | Rows |
|---|---|---:|
| Bundesliga | independent score source is not competition-authoritative | 5 |
| La Liga | independent score source is not competition-authoritative | 5 |
| Ligue 1 | independent score source is not competition-authoritative | 4 |
| Ligue 1 | independent sporting outcome not verified | 1 |
| Premier League | eligible | 5 |
| Serie A | independent score source is not competition-authoritative | 3 |
| Serie A | independent sporting outcome not verified | 2 |

Coverage at one-hour-before-kickoff by maximum observation age, among the fixed pilot sample:

| Maximum age (minutes) | Forecast observations available |
|---:|---:|
| 30 | 25 |
| 60 | 25 |
| 180 | 25 |
| 360 | 25 |

This is observation-age sensitivity only; eligibility still requires independent outcome validation.

Pilot result-source classes: official_competition_feed=5, secondary_only=17, unverified=3. Market-settlement comparisons (True/False/None): None=3, True=22. Secondary/official result comparisons (True/False/None): None=20, True=5. Secondary-source agreement is useful cross-checking but does not satisfy the authoritative-result eligibility rule.

Gamma/CLOB token orientation check in the 25-market pilot: {'matched': 25}. Settlement-value cross-check: {'matched': 25}. The CLOB `tokens[].outcome`, `token_id`, `price`, and `winner` records independently verify the array pairing for these sampled markets, not all listings.

## Traceable pilot examples

| League | Event / market | Requested cutoff UTC | Selected observation UTC | Probability | API draw settlement | Independent source class | Eligibility | Raw market response |
|---|---|---|---|---:|---:|---|---|---|
| La Liga | 852196 / 3599362 | 2026-08-28T16:00:00+00:00 | 2026-08-28T15:59:13+00:00 | 0.265 | 0 | secondary_only | excluded | `data/raw/gamma_events/a7f0b56b914ae1981fbf.json` |
| Ligue 1 | 37592 / 576248 | 2025-08-17T14:15:00+00:00 | 2025-08-17T14:14:02+00:00 | 0.295 | 0 | secondary_only | excluded | `data/raw/gamma_events/e2e2bf0b3bcca6cea69a.json` |
| Bundesliga | 65442 / 650112 | 2025-11-08T13:30:00+00:00 | 2025-11-08T13:29:04+00:00 | 0.185 | 0 | secondary_only | excluded | `data/raw/gamma_events/b5c79ac3e29d9a3b3811.json` |
| Serie A | 88115 / 697917 | 2025-12-06T18:45:00+00:00 | 2025-12-06T18:44:06+00:00 | 0.26 | 0 | secondary_only | excluded | `data/raw/gamma_events/ac12f8b401a9b20bd266.json` |
| Premier League | 44330 / 597254 | 2025-09-27T18:00:00+00:00 | 2025-09-27T17:59:04+00:00 | 0.225 | 1 | official_competition_feed | eligible_exploratory | `data/raw/gamma_events/8aa7ab863cc8b624e1ab.json` |

### Rule text in the selected pilot (mentions, not inferred rule outcomes)

| League | Pilot rows | 90 minutes/regulation | Stoppage time | Extra time | Penalties | Postponement | Cancellation | Void |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| La Liga | 5 | 4 | 4 | 0 | 0 | 5 | 5 | 0 |
| Ligue 1 | 5 | 3 | 3 | 0 | 0 | 5 | 5 | 0 |
| Bundesliga | 5 | 4 | 4 | 0 | 0 | 5 | 5 | 0 |
| Serie A | 5 | 3 | 3 | 0 | 0 | 5 | 5 | 0 |
| Premier League | 5 | 3 | 3 | 0 | 0 | 5 | 5 | 0 |

A zero means the sampled description did not state the topic; it does not mean the market permits or disallows it. Preserve full descriptions in `validation_evidence.csv` and `forecasts.csv`.

## Calibration (exploratory)

Only rows that pass the implemented independent-result eligibility checks enter this table. With small samples, intervals are Wilson 95% intervals for observed frequency, translated to a gap interval by subtracting mean forecast. These intervals do not account for sampling bias. Forecast bins use edges [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0], [left, right), with the final bin including 1. Log loss clips probabilities to [1e-6, 1-1e-6].

| League | Horizon | n | Brier | Log loss |
|---|---|---:|---:|---:|
| pooled | pregame_1h | 5 | 0.2594 | 0.7203 |
| Premier League | pregame_1h | 5 | 0.2594 | 0.7203 |

## Interpretation limits and source status

- Gamma series membership is used as the league assignment; legacy events lacking verified series metadata are not added by title heuristic.
- Home/away order uses the structured event-team `ordering` field only when both `home` and `away` are explicit. Otherwise the event title supplies order and structured team names can corroborate the unordered pair. Known ESPN naming variants use a small explicit alias map; this improves matching but does not make secondary-feed matches authoritative.
- Exact fixture matching uses competition, ordered canonical team names (including a small explicit alias map), and UTC calendar date. Different dates are never automatically merged. Same ordered teams within 21 days produce a review candidate. `fixture_candidates.csv` contains 2 nearby-date pairs: 0 share an ESPN event ID across both date queries, 2 match on only one queried date, and 0 match neither date. No candidate is confirmed as a duplicate/reschedule. Unrecognized aliases or longer reschedule gaps may remain. The old notebook's ambiguous Real Madrid–Athletic Club row had no market/event ID and was not recoverable as that exact observation; current live listing scan detected zero events with multiple full-match draw markets.
- `market.closed` alone is not settlement proof. The pipeline requires `umaResolutionStatus=resolved`, exactly aligned outcomes/prices, and complementary binary prices before recording an API settlement.
- Independent score validation uses ESPN for all five leagues, explicitly secondary and not league-authoritative. The official Premier League feed is additionally queried for Premier League rows. Other leagues need official competition data before they can qualify as authoritative.
- Official Premier League API times are UTC epoch milliseconds; ESPN provides ISO timestamps. Naive source timestamps are left unresolved. The official feed has a fixture `kickoff` value and preserves `provisionalKickoff`; it does not provide an actual whistle timestamp in the fields used here.
- Market descriptions provide listing-specific rules. In the 25-row pilot, postponement and cancellation are mentioned in every description; regulation/90 minutes and stoppage time appear in only some; extra time, penalties, and void are not stated in the sampled descriptions. The historical Real Madrid–Atletico market description also has a cancellation/delay cutoff; its complete text is preserved in `data/processed/price_api_experiments.json`. These wording differences are contract-specific, not league-wide rules.
- Current Polymarket docs describe `/prices-history` as historical price data, with `t` integer timestamps and `p` numeric values; they do not establish whether `p` is a trade, midpoint, or sampled-bucket statistic. No pagination parameter is documented. Observation timestamp is not proven to be an original trade time, so computed age is API-observation age only.
- The implementation runs a bounded/unbounded and overlapping-window comparison on the prior Real Madrid–Atletico market; raw responses and equality counts are in `data/processed/price_api_experiments.json`.
- Numeric event and market identifiers are not interchangeable: the live `/events/521526` and `/markets/521526` lookups returned different objects. The market lookup is the correct source for that prior draw contract.
- Official fixture-page checks: LaLiga results and Bundesliga matchday pages returned content but not a structured match-level historical record for this pilot; Lega Serie A's official fixtures/results page returned current matchday content; the guessed Ligue 1 `/en/fixtures` route returned 404. The official Premier League Football API supplied machine-readable historical fixtures and scores. Other leagues need a verified official historical-results connector.
- Historical docs/API links: https://docs.polymarket.com/api-reference/markets/get-prices-history ; https://docs.polymarket.com/market-data/overview ; https://docs.polymarket.com/concepts/resolution . Premier League source: https://footballapi.pulselive.com/football/fixtures.

## Decision before calibration

Do not interpret the pooled calibration table as representative unless the sample is large and selected without outcome-dependent filtering, and official final results and applicable market rules are independently checked. First adjudicate duplicate/rescheduled candidates, verify team/timestamp identity, confirm official result sources for all leagues, and decide whether API `p` is an acceptable forecast measure after its semantics are documented.
