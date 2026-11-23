# EPL first-goal price-response feasibility pilot

## Decision

**Visible signal worth testing on more matches.** The median goal-window move exceeds the control median and at least two-thirds of paired goal moves are larger than their controls. This is a feasibility heuristic, not evidence of overreaction. No price moves were used to select fixtures or controls, no interpolation was performed, and no goal time was moved to a price jump.

## Coverage

- Candidate fixtures tried: **15** (limit and deterministic selection rule below).
- Matched to FotMob by ordered teams plus fixture date or kickoff proximity: **15/15**.
- Opening goal confirmed in the first half: **11/15**.
- Usable goal/control price pairs: **11/15**.
- Median signed scoring-team win-price move: **0.2100**.
- Median signed control-window move: **0.0100**.
- Scoring-team price rose: goal **11/11**, control **8/11**.
- Existing draw-history observations across selected fixtures: 28577 total; these were not substituted for win prices.
- Polymarket home/away win histories acquired for **11/15** fixtures; all three outcomes have timestamps for **11/15** fixtures (1940 total observations).

All requested event-level raw values, candidate IDs, UTC-bound status, price contract IDs, observation timestamps, signed changes, and exclusions are in the CSV. Full one-minute outcome histories and their raw CLOB source references are in the 3-way history CSV. The paired plot shows each usable goal/control pair; price panels are emitted for fixtures with all three outcomes.

## Source and timing checks

A live FotMob match-details response was inspected before relying on its schema. It contained `content.matchFacts.events.events[]` with `type`, `time`, `timeStr`, `overloadTime`, `isHome`, and `shotmapEvent.period`; it also contained `header.status.halfs.firstHalfStarted`. For Burnley FC–Newcastle United FC (FotMob match 4813557), FotMob `utcTime` was `2025-12-30T19:30:00.000Z`, scheduled UTC kickoff was `2025-12-30T19:30:00+00:00`, and raw `firstHalfStarted` was `30.12.2025 20:31:37`. A unique numeric UTC offset of 60 minutes places the start at `2025-12-30T19:31:37+00:00`; the inferred start is checked against both UTC kickoff fields. The response has no IANA timezone name, so the row remains flagged `timezone_name_unavailable`; an offset is inferred only when unique and both kickoff cross-checks put the start within 15 minutes. Raw event fields remain in the CSV and the response envelope is cached locally. The scoring side is taken from the event score increment when available, with `isHome` retained independently.

## Selection and measurement

The script takes the first 15 fixtures after sorting the available EPL set by SHA-256 of `fixture_id`. Availability requires at least 10 timestamped historical observations between scheduled kickoff and kickoff + 45 minutes. Selection uses identity and timestamp coverage only, never price values or movement. The inventory includes 490 EPL fixtures; the existing snapshot output has 486 result-eligible fixtures, and 392 eligible `ingame_45m` rows.

Event/control freshness limit: **5 minutes**. Event movement uses the nearest observed scoring-team win price strictly before the goal interval and the first observation strictly after it, each no more than the freshness limit away. Control duration equals the plotted goal window (uncertainty interval plus ±3 minutes); controls are selected by nearest goal-free time and timestamp coverage in the same half, without price magnitude. No interpolation is used. The visible-signal classification requires at least five pairs, a higher median goal move than control, and a goal move larger than control for at least two-thirds of pairs; otherwise usable data are described as heavy overlap.

## Market data and next step

- Gamma supplied separate binary Yes/No contracts for home win, draw, and away win. The script reuses the draw observations already in `historical_prices.csv`, fetches the missing home/away Yes-token histories through the existing cached CLOB client, and stores all timestamped observations in `data/processed/soccer_three_way_price_history.csv`.
- Each fetched team-win Yes token is cross-checked against the CLOB market's named outcome/token mapping before using its history. Market IDs, condition IDs, token IDs, mapping statuses, and raw response paths are recorded in the fixture CSV.
- Manual FotMob override file: `data/processed/soccer_goal_manual_overrides.csv`. Fill `half_start_utc` **or** `half_start_timezone` only when independently verified, and use the explicit goal columns if a detail response is unavailable. Raw FotMob responses are cached under `data/raw/fotmob_*`.

- Observed exclusion reasons: no_first_goal_fields, opening_goal_not_confirmed_in_first_half, scoring_side_unavailable.

## Reproduce

From `Sports-Market-Capstone/`:

```bash
python3 scripts/soccer_goal_response_pilot.py --limit 15
```

Outputs are `data/processed/soccer_goal_response_pilot.csv`, `data/processed/soccer_three_way_price_history.csv`, `data/processed/soccer_goal_response_paired.png`, and `data/processed/soccer_goal_response_price_panels.png`.
