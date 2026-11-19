# Calibration data usability report

Generated 2026-11-12T20:42:49.625410+00:00. Rebuild with `python3 scripts/build_calibration_datasets.py`.

## Sample and definitions

- Retrieved listing count: 10262 total listings in the configured series; these are not fixtures.
- Deterministic selected sample: up to 50 resolved, unambiguous draw-contract fixtures per league, ordered by SHA-256 of seed `calibration-datasets-v1` plus event and market IDs; one fixture per conservative fixture key.
- Selected fixture sample date range (score-verified subset): 2025-08-17 to 2026-11-08.
- Sporting outcomes come from matched, completed ESPN scores (with the Premier League feed used only as an optional conflict check). Final outcome is home win/draw/away win; the binary draw label is 1 only for a score draw and 0 otherwise.
- Gamma/CLOB draw settlement is retained as a separate outcome label and cross-check, not silently substituted for the score-derived label. Both resolved Yes (1) and No (0) settlements are eligible when they agree with the matched score.
- A calibration snapshot is eligible only when fixture identity, timezone-aware kickoff, completed score, resolved settlement, Gamma/CLOB token orientation, settlement-vs-score checks, and forecast timing are supported and a valid historical draw price is available. The approximate halftime proxy is kept for audit but excluded.

| League | Selected fixtures | Home wins | Draws | Away wins | Unresolved scores | Result-eligible fixtures |
|---|---:|---:|---:|---:|---:|---:|
| La Liga | 50 | 12 | 7 | 6 | 25 | 25 |
| Ligue 1 | 50 | 5 | 8 | 5 | 32 | 18 |
| Bundesliga | 50 | 9 | 3 | 7 | 31 | 19 |
| Serie A | 50 | 10 | 3 | 8 | 29 | 21 |
| Premier League | 50 | 19 | 14 | 17 | 0 | 50 |

### Traceable closing-pregame examples

Each row links the forecast cutoff and selected API observation to the market, price-history, and independent result response caches. The final three values are score-derived draw label / Polymarket settlement label.

| League | Fixture and event/market IDs | Match | Requested cutoff UTC | Selected observation UTC | p(draw) | Score / labels | Cached evidence: market; price history; result |
|---|---|---|---|---|---:|---|---|
| La Liga | `fx-03d851e40668e03bb3bb`; 88976/699887 | Elche CF–Girona FC | 2025-12-07T12:55:00+00:00 | 2025-12-07T12:54:07+00:00 | 0.285 | 3-0 / 0 / 0 | `data/raw/gamma_events/75035f7831db0bfffb83.json`; `data/raw/clob_prices_history/43f67b55dae50f796f4e.json`; `data/raw/espn_scoreboard/6f38d8a302fee4718bad.json` |
| Ligue 1 | `fx-007dabec712730794ee3`; 190008/1275260 | Paris Saint-Germain FC–Olympique de Marseille | 2026-02-08T19:40:00+00:00 | 2026-02-08T19:39:14+00:00 | 0.175 | 5-0 / 0 / 0 | `data/raw/gamma_events/36392961003f87394279.json`; `data/raw/clob_prices_history/507ce30d76c9cd084dc8.json`; `data/raw/espn_scoreboard/49c44e77aa26a157ff7f.json` |
| Bundesliga | `fx-07c8bb5f13b36ee7b887`; 47902/607217 | VfB Stuttgart–1. FC Heidenheim 1846 | 2025-10-05T13:25:00+00:00 | 2025-10-05T13:24:03+00:00 | 0.185 | 1-0 / 0 / 0 | `data/raw/gamma_events/8160898699b004dc6fee.json`; `data/raw/clob_prices_history/8e4c4f4de2d40cdcfe7e.json`; `data/raw/espn_scoreboard/a73830773a5531a2855d.json` |
| Serie A | `fx-03744eb9ceacb3399a45`; 140078/1093269 | Pisa SC–Atalanta BC | 2026-01-16T19:40:00+00:00 | 2026-01-16T19:39:12+00:00 | 0.235 | 1-1 / 1 / 1 | `data/raw/gamma_events/fa2d9dab1f89771f5fae.json`; `data/raw/clob_prices_history/a906e1f5203958ff2862.json`; `data/raw/espn_scoreboard/58869db6aef15591c3c6.json` |
| Premier League | `fx-057f1a3be308557ea46f`; 142069/1102114 | Tottenham Hotspur FC–West Ham United FC | 2026-01-17T14:55:00+00:00 | 2026-01-17T14:54:11+00:00 | 0.26 | 1-2 / 0 / 0 | `data/raw/gamma_events/1107efb17aa5f5c14c0f.json`; `data/raw/clob_prices_history/4921fdc7e56c4668f6a7.json`; `data/raw/espn_scoreboard/dbac00162a8fe701ff89.json` |

Score-derived label source counts: {'unresolved': 117, 'ESPN completed score feed': 133}.

## Forecast coverage

Pregame cutoffs are [1440, 360, 60, 30, 5] minutes before the independent scheduled kickoff where available. The 5-minute cutoff is the project's operational closing pregame forecast, not an exchange-defined close. Maximum API-observation age is 360 minutes pregame and 10 minutes in-game.

In-game wallclock cutoffs are reconstructed from ESPN commentary play-clock and wallclock records. Within-period clock brackets are interpolated only when the bracket is no wider than 5 match-clock minutes and 10 wallclock minutes; forecasts use only Polymarket observations at or before that cutoff. Halftime uses the last timestamped first-half play as an approximate proxy because ESPN's explicit half-end entry had no wallclock in the inspected response; that proxy is retained but excluded from calibration. Approximate interpolated match-clock cutoffs are identified separately from missing/uncertain timing. A 90-minute cutoff is unavailable unless commentary brackets it and a later timestamped play establishes that the match was still underway; it is not the final whistle.

| League | Horizon | Selected fixtures | Price available | Analysis eligible | Missing price | Unmatched/uncertain fixture | Missing/uncertain timing | Approximate timing | Unresolved outcome |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| La Liga | ingame_15m | 50 | 21 | 21 | 0 | 25 | 29 | 19 | 25 |
| La Liga | ingame_30m | 50 | 24 | 24 | 0 | 25 | 26 | 24 | 25 |
| La Liga | ingame_60m | 50 | 21 | 21 | 0 | 25 | 29 | 21 | 25 |
| La Liga | ingame_75m | 50 | 24 | 24 | 0 | 25 | 26 | 24 | 25 |
| La Liga | ingame_90m | 50 | 25 | 25 | 0 | 25 | 25 | 24 | 25 |
| La Liga | ingame_halftime | 50 | 25 | 0 | 0 | 25 | 25 | 25 | 25 |
| La Liga | pregame_1h | 50 | 49 | 25 | 1 | 25 | 0 | 0 | 25 |
| La Liga | pregame_24h | 50 | 50 | 25 | 0 | 25 | 0 | 0 | 25 |
| La Liga | pregame_30m | 50 | 49 | 25 | 1 | 25 | 0 | 0 | 25 |
| La Liga | pregame_6h | 50 | 49 | 25 | 1 | 25 | 0 | 0 | 25 |
| La Liga | pregame_closing_5m | 50 | 49 | 25 | 1 | 25 | 0 | 0 | 25 |
| Ligue 1 | ingame_15m | 50 | 15 | 15 | 0 | 32 | 35 | 15 | 32 |
| Ligue 1 | ingame_30m | 50 | 18 | 18 | 0 | 32 | 32 | 18 | 32 |
| Ligue 1 | ingame_60m | 50 | 18 | 18 | 0 | 32 | 32 | 18 | 32 |
| Ligue 1 | ingame_75m | 50 | 17 | 17 | 0 | 32 | 33 | 17 | 32 |
| Ligue 1 | ingame_90m | 50 | 17 | 17 | 0 | 32 | 33 | 17 | 32 |
| Ligue 1 | ingame_halftime | 50 | 18 | 0 | 0 | 32 | 32 | 18 | 32 |
| Ligue 1 | pregame_1h | 50 | 50 | 18 | 0 | 32 | 0 | 0 | 32 |
| Ligue 1 | pregame_24h | 50 | 50 | 18 | 0 | 32 | 0 | 0 | 32 |
| Ligue 1 | pregame_30m | 50 | 50 | 18 | 0 | 32 | 0 | 0 | 32 |
| Ligue 1 | pregame_6h | 50 | 50 | 18 | 0 | 32 | 0 | 0 | 32 |
| Ligue 1 | pregame_closing_5m | 50 | 50 | 18 | 0 | 32 | 0 | 0 | 32 |
| Bundesliga | ingame_15m | 50 | 16 | 16 | 0 | 31 | 34 | 16 | 31 |
| Bundesliga | ingame_30m | 50 | 18 | 18 | 0 | 31 | 32 | 18 | 31 |
| Bundesliga | ingame_60m | 50 | 17 | 17 | 0 | 31 | 33 | 16 | 31 |
| Bundesliga | ingame_75m | 50 | 19 | 19 | 0 | 31 | 31 | 19 | 31 |
| Bundesliga | ingame_90m | 50 | 17 | 17 | 0 | 31 | 33 | 17 | 31 |
| Bundesliga | ingame_halftime | 50 | 19 | 0 | 0 | 31 | 31 | 19 | 31 |
| Bundesliga | pregame_1h | 50 | 50 | 19 | 0 | 31 | 0 | 0 | 31 |
| Bundesliga | pregame_24h | 50 | 50 | 19 | 0 | 31 | 0 | 0 | 31 |
| Bundesliga | pregame_30m | 50 | 50 | 19 | 0 | 31 | 0 | 0 | 31 |
| Bundesliga | pregame_6h | 50 | 50 | 19 | 0 | 31 | 0 | 0 | 31 |
| Bundesliga | pregame_closing_5m | 50 | 50 | 19 | 0 | 31 | 0 | 0 | 31 |
| Serie A | ingame_15m | 50 | 18 | 18 | 0 | 29 | 32 | 18 | 29 |
| Serie A | ingame_30m | 50 | 19 | 19 | 0 | 29 | 31 | 19 | 29 |
| Serie A | ingame_60m | 50 | 19 | 19 | 0 | 29 | 31 | 19 | 29 |
| Serie A | ingame_75m | 50 | 20 | 20 | 0 | 29 | 30 | 20 | 29 |
| Serie A | ingame_90m | 50 | 21 | 21 | 0 | 29 | 29 | 21 | 29 |
| Serie A | ingame_halftime | 50 | 21 | 0 | 0 | 29 | 29 | 21 | 29 |
| Serie A | pregame_1h | 50 | 50 | 21 | 0 | 29 | 0 | 0 | 29 |
| Serie A | pregame_24h | 50 | 50 | 21 | 0 | 29 | 0 | 0 | 29 |
| Serie A | pregame_30m | 50 | 50 | 21 | 0 | 29 | 0 | 0 | 29 |
| Serie A | pregame_6h | 50 | 50 | 21 | 0 | 29 | 0 | 0 | 29 |
| Serie A | pregame_closing_5m | 50 | 50 | 21 | 0 | 29 | 0 | 0 | 29 |
| Premier League | ingame_15m | 50 | 44 | 44 | 0 | 0 | 6 | 44 | 0 |
| Premier League | ingame_30m | 50 | 45 | 45 | 0 | 0 | 5 | 45 | 0 |
| Premier League | ingame_60m | 50 | 48 | 48 | 0 | 0 | 2 | 45 | 0 |
| Premier League | ingame_75m | 50 | 49 | 49 | 0 | 0 | 1 | 48 | 0 |
| Premier League | ingame_90m | 50 | 47 | 47 | 0 | 0 | 3 | 47 | 0 |
| Premier League | ingame_halftime | 50 | 50 | 0 | 0 | 0 | 0 | 50 | 0 |
| Premier League | pregame_1h | 50 | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| Premier League | pregame_24h | 50 | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| Premier League | pregame_30m | 50 | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| Premier League | pregame_6h | 50 | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| Premier League | pregame_closing_5m | 50 | 50 | 50 | 0 | 0 | 0 | 0 | 0 |

## Exploratory draw calibration

Pooled probability-bin summaries use one forecast per fixture per horizon. The reported 95% intervals are Wilson intervals for observed draw frequency; they do not account for sample selection or other systematic uncertainty. Sparse bins remain visible. Mean forecast in each bin is the calibration reference.

| League | Horizon | Probability bin | n | Draws | Non-draws | Mean quoted p | Draw frequency | Frequency 95% CI |
|---|---|---|---:|---:|---:|---:|---:|---|
| pooled | pregame_24h | 0.00–0.10 | 4 | 1 | 3 | 0.083 | 0.250 | [0.046, 0.699] |
| pooled | pregame_24h | 0.10–0.20 | 19 | 3 | 16 | 0.176 | 0.158 | [0.055, 0.376] |
| pooled | pregame_24h | 0.20–0.30 | 95 | 26 | 69 | 0.258 | 0.274 | [0.194, 0.371] |
| pooled | pregame_24h | 0.30–0.40 | 15 | 5 | 10 | 0.312 | 0.333 | [0.152, 0.583] |
| pooled | pregame_6h | 0.00–0.10 | 4 | 1 | 3 | 0.081 | 0.250 | [0.046, 0.699] |
| pooled | pregame_6h | 0.10–0.20 | 17 | 1 | 16 | 0.174 | 0.059 | [0.010, 0.270] |
| pooled | pregame_6h | 0.20–0.30 | 98 | 27 | 71 | 0.260 | 0.276 | [0.197, 0.371] |
| pooled | pregame_6h | 0.30–0.40 | 14 | 6 | 8 | 0.314 | 0.429 | [0.214, 0.674] |
| pooled | pregame_1h | 0.00–0.10 | 3 | 0 | 3 | 0.077 | 0.000 | [0.000, 0.561] |
| pooled | pregame_1h | 0.10–0.20 | 16 | 3 | 13 | 0.169 | 0.188 | [0.066, 0.430] |
| pooled | pregame_1h | 0.20–0.30 | 102 | 28 | 74 | 0.260 | 0.275 | [0.197, 0.368] |
| pooled | pregame_1h | 0.30–0.40 | 12 | 4 | 8 | 0.317 | 0.333 | [0.138, 0.609] |
| pooled | pregame_30m | 0.00–0.10 | 3 | 0 | 3 | 0.082 | 0.000 | [0.000, 0.561] |
| pooled | pregame_30m | 0.10–0.20 | 18 | 3 | 15 | 0.174 | 0.167 | [0.058, 0.392] |
| pooled | pregame_30m | 0.20–0.30 | 99 | 28 | 71 | 0.261 | 0.283 | [0.204, 0.378] |
| pooled | pregame_30m | 0.30–0.40 | 13 | 4 | 9 | 0.318 | 0.308 | [0.127, 0.576] |
| pooled | pregame_closing_5m | 0.00–0.10 | 3 | 0 | 3 | 0.083 | 0.000 | [0.000, 0.561] |
| pooled | pregame_closing_5m | 0.10–0.20 | 17 | 3 | 14 | 0.171 | 0.176 | [0.062, 0.410] |
| pooled | pregame_closing_5m | 0.20–0.30 | 97 | 27 | 70 | 0.260 | 0.278 | [0.199, 0.375] |
| pooled | pregame_closing_5m | 0.30–0.40 | 16 | 5 | 11 | 0.317 | 0.312 | [0.142, 0.556] |
| pooled | ingame_15m | 0.00–0.10 | 6 | 0 | 6 | 0.057 | 0.000 | [0.000, 0.390] |
| pooled | ingame_15m | 0.10–0.20 | 16 | 2 | 14 | 0.149 | 0.125 | [0.035, 0.360] |
| pooled | ingame_15m | 0.20–0.30 | 71 | 22 | 49 | 0.254 | 0.310 | [0.214, 0.425] |
| pooled | ingame_15m | 0.30–0.40 | 21 | 7 | 14 | 0.316 | 0.333 | [0.172, 0.546] |
| pooled | ingame_30m | 0.00–0.10 | 9 | 0 | 9 | 0.056 | 0.000 | [0.000, 0.299] |
| pooled | ingame_30m | 0.10–0.20 | 21 | 4 | 17 | 0.148 | 0.190 | [0.077, 0.400] |
| pooled | ingame_30m | 0.20–0.30 | 52 | 13 | 39 | 0.252 | 0.250 | [0.152, 0.382] |
| pooled | ingame_30m | 0.30–0.40 | 42 | 15 | 27 | 0.331 | 0.357 | [0.230, 0.508] |
| pooled | ingame_60m | 0.00–0.10 | 35 | 1 | 34 | 0.036 | 0.029 | [0.005, 0.145] |
| pooled | ingame_60m | 0.10–0.20 | 18 | 6 | 12 | 0.152 | 0.333 | [0.163, 0.563] |
| pooled | ingame_60m | 0.20–0.30 | 25 | 6 | 19 | 0.248 | 0.240 | [0.115, 0.434] |
| pooled | ingame_60m | 0.30–0.40 | 15 | 6 | 9 | 0.340 | 0.400 | [0.198, 0.643] |
| pooled | ingame_60m | 0.40–0.50 | 29 | 14 | 15 | 0.446 | 0.483 | [0.314, 0.656] |
| pooled | ingame_60m | 0.50–0.60 | 1 | 1 | 0 | 0.505 | 1.000 | [0.207, 1.000] |
| pooled | ingame_75m | 0.00–0.10 | 43 | 1 | 42 | 0.025 | 0.023 | [0.004, 0.121] |
| pooled | ingame_75m | 0.10–0.20 | 26 | 4 | 22 | 0.160 | 0.154 | [0.062, 0.335] |
| pooled | ingame_75m | 0.20–0.30 | 20 | 6 | 14 | 0.249 | 0.300 | [0.145, 0.519] |
| pooled | ingame_75m | 0.30–0.40 | 5 | 3 | 2 | 0.335 | 0.600 | [0.231, 0.882] |
| pooled | ingame_75m | 0.40–0.50 | 3 | 1 | 2 | 0.468 | 0.333 | [0.061, 0.792] |
| pooled | ingame_75m | 0.50–0.60 | 22 | 13 | 9 | 0.550 | 0.591 | [0.387, 0.767] |
| pooled | ingame_75m | 0.60–0.70 | 10 | 7 | 3 | 0.617 | 0.700 | [0.397, 0.892] |
| pooled | ingame_90m | 0.00–0.10 | 70 | 3 | 67 | 0.029 | 0.043 | [0.015, 0.119] |
| pooled | ingame_90m | 0.10–0.20 | 22 | 5 | 17 | 0.133 | 0.227 | [0.101, 0.434] |
| pooled | ingame_90m | 0.20–0.30 | 2 | 1 | 1 | 0.220 | 0.500 | [0.095, 0.905] |
| pooled | ingame_90m | 0.60–0.70 | 2 | 1 | 1 | 0.667 | 0.500 | [0.095, 0.905] |
| pooled | ingame_90m | 0.70–0.80 | 15 | 9 | 6 | 0.761 | 0.600 | [0.357, 0.802] |
| pooled | ingame_90m | 0.80–0.90 | 16 | 14 | 2 | 0.819 | 0.875 | [0.640, 0.965] |

| League | Horizon | n | Brier | Log loss |
|---|---|---:|---:|---:|
| pooled | pregame_24h | 133 | 0.1916 | 0.5709 |
| La Liga | pregame_24h | 25 | 0.1832 | 0.5380 |
| pooled | pregame_6h | 133 | 0.1916 | 0.5710 |
| La Liga | pregame_6h | 25 | 0.1839 | 0.5396 |
| pooled | pregame_1h | 133 | 0.1919 | 0.5716 |
| La Liga | pregame_1h | 25 | 0.1841 | 0.5402 |
| pooled | pregame_30m | 133 | 0.1925 | 0.5724 |
| La Liga | pregame_30m | 25 | 0.1858 | 0.5447 |
| pooled | pregame_closing_5m | 133 | 0.1917 | 0.5702 |
| La Liga | pregame_closing_5m | 25 | 0.1855 | 0.5440 |
| pooled | ingame_15m | 114 | 0.1917 | 0.5639 |
| La Liga | ingame_15m | 21 | 0.2176 | 0.6167 |
| pooled | ingame_30m | 124 | 0.1837 | 0.5455 |
| La Liga | ingame_30m | 24 | 0.1985 | 0.5899 |
| pooled | ingame_60m | 123 | 0.1730 | 0.5040 |
| La Liga | ingame_60m | 21 | 0.1854 | 0.5123 |
| pooled | ingame_75m | 129 | 0.1405 | 0.4187 |
| La Liga | ingame_75m | 24 | 0.1271 | 0.3769 |
| pooled | ingame_90m | 127 | 0.1072 | 0.3340 |
| La Liga | ingame_90m | 25 | 0.0760 | 0.2643 |
| Ligue 1 | pregame_24h | 18 | 0.2818 | 0.7733 |
| Ligue 1 | pregame_6h | 18 | 0.2808 | 0.7710 |
| Ligue 1 | pregame_1h | 18 | 0.2808 | 0.7731 |
| Ligue 1 | pregame_30m | 18 | 0.2804 | 0.7694 |
| Ligue 1 | pregame_closing_5m | 18 | 0.2786 | 0.7644 |
| Ligue 1 | ingame_30m | 18 | 0.2379 | 0.6513 |
| Ligue 1 | ingame_60m | 18 | 0.1798 | 0.5145 |
| Ligue 1 | ingame_75m | 17 | 0.1234 | 0.3965 |
| Ligue 1 | ingame_90m | 17 | 0.0488 | 0.1965 |
| Ligue 1 | ingame_15m | 15 | 0.2241 | 0.6122 |
| Bundesliga | pregame_24h | 19 | 0.1463 | 0.4852 |
| Bundesliga | pregame_6h | 19 | 0.1462 | 0.4850 |
| Bundesliga | pregame_1h | 19 | 0.1459 | 0.4817 |
| Bundesliga | pregame_30m | 19 | 0.1455 | 0.4782 |
| Bundesliga | pregame_closing_5m | 19 | 0.1446 | 0.4739 |
| Bundesliga | ingame_15m | 16 | 0.1674 | 0.5280 |
| Bundesliga | ingame_60m | 17 | 0.1485 | 0.4602 |
| Bundesliga | ingame_75m | 19 | 0.1053 | 0.3304 |
| Bundesliga | ingame_90m | 17 | 0.0574 | 0.2069 |
| Bundesliga | ingame_30m | 18 | 0.1414 | 0.4496 |
| Serie A | pregame_24h | 21 | 0.1352 | 0.4450 |
| Serie A | pregame_6h | 21 | 0.1368 | 0.4492 |
| Serie A | pregame_1h | 21 | 0.1374 | 0.4512 |
| Serie A | pregame_30m | 21 | 0.1374 | 0.4506 |
| Serie A | pregame_closing_5m | 21 | 0.1372 | 0.4503 |
| Serie A | ingame_30m | 19 | 0.1229 | 0.4161 |
| Serie A | ingame_60m | 19 | 0.1222 | 0.3884 |
| Serie A | ingame_75m | 20 | 0.1048 | 0.3193 |
| Serie A | ingame_90m | 21 | 0.0758 | 0.2420 |
| Serie A | ingame_15m | 18 | 0.1234 | 0.4205 |
| Premier League | pregame_24h | 50 | 0.2042 | 0.6000 |
| Premier League | pregame_6h | 50 | 0.2038 | 0.5985 |
| Premier League | pregame_1h | 50 | 0.2041 | 0.5996 |
| Premier League | pregame_30m | 50 | 0.2052 | 0.6023 |
| Premier League | pregame_closing_5m | 50 | 0.2044 | 0.6003 |
| Premier League | ingame_15m | 44 | 0.2051 | 0.5940 |
| Premier League | ingame_30m | 45 | 0.1966 | 0.5725 |
| Premier League | ingame_60m | 48 | 0.1937 | 0.5577 |
| Premier League | ingame_75m | 49 | 0.1812 | 0.5217 |
| Premier League | ingame_90m | 47 | 0.1769 | 0.5080 |

The reliability diagram in `data/processed/reliability.png` includes the identity line, Wilson intervals, and bin counts for pooled horizons with data.

### Common-fixture horizon comparison

Rows below use fixtures with eligible snapshots at every configured pregame horizon. The paired Brier difference is horizon minus the configured closing horizon; confidence intervals bootstrap whole fixtures, preserving repeated forecasts from a fixture.

| Horizon | Common fixtures | Common-set Brier | Paired Brier difference vs closing | Fixture-bootstrap 95% CI |
|---|---:|---:|---:|---|
| pregame_24h | 133 | 0.19157497932330828 | -0.00016645112781955333 | [-0.002026037593984965, 0.0016100526315789407] |
| pregame_6h | 133 | 0.191642257518797 | -9.917293233082615e-05 | [-0.0015962781954887183, 0.0015124812030075257] |
| pregame_1h | 133 | 0.1918828590225564 | 0.00014142857142857278 | [-0.001136428571428572, 0.00146353383458647] |
| pregame_30m | 133 | 0.1924707537593985 | 0.0007293233082706763 | [-6.597744360902317e-05, 0.0015539473684210534] |
| pregame_closing_5m | 133 | 0.19174143045112785 | 0.0 | [0.0, 0.0] |

## Exclusions, limitations, and outputs

- `completed_sporting_result_unavailable;independent_scheduled_kickoff_unavailable`: 117
- `eligible`: 133

- A historical observation's age is API-observation age, not proven price staleness. Polymarket documentation and experiments do not establish whether `p` is a trade, midpoint, or fidelity-bucket statistic.
- Rescheduled or otherwise ambiguous fixture identities are retained but excluded from calibration pending review. Conflicting ESPN/official scores and mismatches between final score and Polymarket settlement are preserved in `fixture_result_audit.csv`.
- An API settlement mismatch may reflect exceptional contract resolution, such as a cancellation rule, rather than a bad score. Those rows are excluded and require contract-specific review.
- The sample is deterministic and reproducible, but it is a hashed sample of resolved draw-market fixtures, not a population census; listings without a resolved full-match draw contract are outside the sampling frame.
- See `fixtures.csv`, `historical_prices.csv`, `forecast_snapshots.csv`, `snapshot_coverage.csv`, `in_game_timing.csv`, `horizon_comparison.csv`, and cached raw response references for row-level evidence.
