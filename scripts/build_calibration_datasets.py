#!/usr/bin/env python3
"""Build reusable fixture, historical-price, and forecast-snapshot datasets."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import validate_pipeline as pipeline


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
REPORT = ROOT / "reports" / "calibration_data_report.md"


def parse_pregame_horizons(value: str) -> list[int]:
    try:
        horizons = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pregame horizons must be comma-separated minutes") from exc
    if not horizons or any(item <= 0 for item in horizons) or len(horizons) != len(set(horizons)):
        raise argparse.ArgumentTypeError("pregame horizons must be unique positive minutes")
    return horizons


def parse_ingame_horizons(value: str) -> list[str]:
    horizons = [item.strip().casefold() for item in value.split(",") if item.strip()]
    if not horizons or len(horizons) != len(set(horizons)):
        raise argparse.ArgumentTypeError("in-game horizons must be nonempty and unique")
    if any(item != "halftime" and (not item.isdigit() or not 1 <= int(item) <= 120) for item in horizons):
        raise argparse.ArgumentTypeError("in-game horizons must be halftime or minutes from 1 to 120")
    return horizons


def age_sensitivity_counts(rows: list[dict[str, Any]], maximum_age_minutes: int) -> tuple[int, int]:
    candidates = [
        row for row in rows
        if row.get("price_observation_status") in {"selected", "price_exceeds_maximum_age"}
        and row.get("observation_age_minutes") is not None
        and row["observation_age_minutes"] <= maximum_age_minutes
    ]
    eligible = sum(
        bool(row.get("fixture_result_eligible"))
        and row.get("token_mapping_status") == "matched"
        and row.get("settlement_crosscheck_status") == "matched"
        and row.get("timing_precision") != "approximate"
        for row in candidates
    )
    return len(candidates), eligible


def fixture_result_record(
    row: dict[str, Any],
    token_audit: dict[str, Any],
    espn: dict[str, Any] | None,
    espn_error: str | None,
    official: dict[str, Any],
    kickoff_tolerance_minutes: int,
) -> dict[str, Any]:
    espn_completed = bool(
        espn
        and espn.get("score_match_status") == "matched"
        and espn.get("completed")
        and espn.get("score_home") is not None
        and espn.get("score_away") is not None
    )
    official_completed = bool(
        official.get("status") == "C"
        and official.get("score_home") is not None
        and official.get("score_away") is not None
    )
    if espn_completed:
        home_score = int(espn["score_home"])
        away_score = int(espn["score_away"])
        result_source = "ESPN completed score feed"
        source_ref = espn.get("raw_ref")
    elif official_completed:
        home_score = int(official["score_home"])
        away_score = int(official["score_away"])
        result_source = str(official.get("source") or "official competition feed")
        source_ref = official.get("source_ref")
    else:
        home_score = away_score = None
        result_source = None
        source_ref = None

    score_conflict = bool(
        espn_completed
        and official_completed
        and (
            int(espn["score_home"]) != int(official["score_home"])
            or int(espn["score_away"]) != int(official["score_away"])
        )
    )
    draw_label = int(home_score == away_score) if home_score is not None and away_score is not None else None
    if home_score is None or away_score is None:
        final_outcome = None
    elif home_score > away_score:
        final_outcome = "home_win"
    elif home_score < away_score:
        final_outcome = "away_win"
    else:
        final_outcome = "draw"

    api_kickoff = pipeline.parse_iso(row.get("market_kickoff_utc"))
    espn_kickoff = pipeline.parse_iso((espn or {}).get("event_date"))
    official_kickoff = pipeline.parse_iso(official.get("kickoff_utc"))
    scheduled_kickoff = espn_kickoff or official_kickoff or api_kickoff
    independent_kickoff = espn_kickoff or official_kickoff
    kickoff_difference = (
        (independent_kickoff - api_kickoff).total_seconds() / 60
        if independent_kickoff and api_kickoff else None
    )
    fixture_status = row.get("fixture_match_status")
    identity_ambiguous = fixture_status not in {
        "exact_match_key",
        "duplicate_listing_exact_key",
    }
    settlement_matches_score = (
        draw_label == row.get("api_settled_draw")
        if draw_label is not None and row.get("api_settled_draw") in (0, 1)
        else None
    )
    reasons = []
    if identity_ambiguous:
        reasons.append("fixture_identity_uncertain")
    if home_score is None or away_score is None:
        reasons.append(espn_error or "completed_sporting_result_unavailable")
    if kickoff_difference is None:
        reasons.append("independent_scheduled_kickoff_unavailable")
    elif abs(kickoff_difference) > kickoff_tolerance_minutes:
        reasons.append("API_and_ESPN_kickoff_mismatch")
    if row.get("api_settled_draw") not in (0, 1):
        reasons.append("unresolved_or_nonbinary_draw_contract")
    if settlement_matches_score is False:
        reasons.append("Polymarket_draw_settlement_conflicts_with_score")
    if score_conflict:
        reasons.append("ESPN_and_competition_feed_scores_conflict")
    if token_audit.get("token_mapping_status") != "matched":
        reasons.append("Gamma_CLOB_token_mapping_not_confirmed")
    if token_audit.get("settlement_crosscheck_status") != "matched":
        reasons.append("CLOB_settlement_not_confirmed")

    return {
        "league": row["league"],
        "event_id": row["event_id"],
        "fixture_id": row["fixture_id"],
        "outcome_type": "draw",
        "market_id": row["market_id"],
        "condition_id": row["condition_id"],
        "draw_token_id": row["yes_token_id"],
        "token_mapping_status": token_audit.get("token_mapping_status"),
        "settlement_crosscheck_status": token_audit.get("settlement_crosscheck_status"),
        "home_team": row.get("home_team"),
        "away_team": row.get("away_team"),
        "api_kickoff_utc": row.get("market_kickoff_utc"),
        "scheduled_kickoff_utc": scheduled_kickoff.isoformat() if scheduled_kickoff else None,
        "scheduled_kickoff_source": (
            "ESPN scoreboard event date" if espn_kickoff
            else "Premier League Football API" if official_kickoff
            else "Polymarket market kickoff fallback" if api_kickoff
            else None
        ),
        "kickoff_timezone": "UTC (timezone-aware source timestamp)" if scheduled_kickoff else None,
        "actual_kickoff_utc": None,
        "kickoff_difference_api_independent_minutes": kickoff_difference,
        "kickoff_difference_source": (
            "ESPN scoreboard event date" if espn_kickoff
            else "Premier League Football API" if official_kickoff
            else None
        ),
        "final_score_home": home_score,
        "final_score_away": away_score,
        "final_match_outcome": final_outcome,
        "final_draw_label": draw_label,
        "verified_draw": draw_label,
        "draw_label_source": result_source,
        "result_source_raw_ref": source_ref,
        "espn_event_id": (espn or {}).get("event_id"),
        "espn_event_status": (espn or {}).get("status"),
        "espn_result_match_status": (espn or {}).get("score_match_status") or espn_error,
        "espn_team_match_method": (espn or {}).get("team_match_method"),
        "official_result_status": official.get("status"),
        "official_score_home": official.get("score_home"),
        "official_score_away": official.get("score_away"),
        "official_result_raw_ref": official.get("source_ref"),
        "cross_provider_score_conflict": score_conflict,
        "api_settled_draw": row.get("api_settled_draw"),
        "settlement_matches_score_label": settlement_matches_score,
        "settlement_label_source": "Polymarket Gamma resolved binary outcomePrices",
        "fixture_identity_status": fixture_status,
        "fixture_result_eligible": not reasons,
        "fixture_result_exclusion_reason": ";".join(reasons) or None,
        "result_verification_status": "verified" if not reasons else "uncertain_or_excluded",
        "market_raw_ref": row.get("source_raw_ref"),
        "token_metadata_raw_ref": token_audit.get("raw_ref"),
        "score_feed_raw_ref": (espn or {}).get("raw_ref"),
        "official_season_raw_ref": official.get("season_ref"),
    }


def update_fixture_table(
    fixture_rows: list[dict[str, Any]],
    fixture_facts: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    for row in fixture_rows:
        facts = fixture_facts.get((row["league"], row["event_id"]))
        if facts:
            row.update({
                key: value for key, value in facts.items()
                if key not in {"market_id", "condition_id", "draw_token_id", "token_mapping_status", "settlement_crosscheck_status"}
            })
    return fixture_rows


def build_requests(
    kickoff: datetime | None,
    summary: dict[str, Any] | None,
    pregame_horizons: list[int],
    closing_horizon: int,
    ingame_horizons: list[str],
    max_clock_bracket_minutes: int,
    max_wallclock_bracket_minutes: int,
) -> list[dict[str, Any]]:
    requests = []
    for minutes in pregame_horizons:
        requests.append({
            "forecast_horizon": pipeline.horizon_name(minutes, closing_horizon),
            "horizon_type": "pregame",
            "requested_horizon_minutes": minutes,
            "requested_timestamp_utc": (
                (kickoff - timedelta(minutes=minutes)).isoformat() if kickoff else None
            ),
            "timing_status": "scheduled_kickoff_relative" if kickoff else "missing_scheduled_kickoff",
            "timing_precision": "scheduled_kickoff",
            "in_progress_check": None,
            "match_clock_source": None,
        })
    summary_matched = isinstance(summary, dict) and summary.get("_summary_status") == "matched"
    commentary = summary.get("commentary", []) if summary_matched else []
    in_game = pipeline.match_clock_cutoffs(
        commentary if isinstance(commentary, list) else [],
        ingame_horizons,
        max_clock_bracket_minutes,
        max_wallclock_bracket_minutes,
    )
    if not summary_matched:
        missing_status = (
            summary.get("_summary_status")
            if isinstance(summary, dict)
            else "espn_match_summary_unavailable"
        )
        in_game = [
            {
                "forecast_horizon": f"ingame_{horizon}m" if horizon.isdigit() else "ingame_halftime",
                "horizon_type": "in_game_match_clock",
                "requested_match_minute": int(horizon) if horizon.isdigit() else "halftime",
                "timing_status": missing_status,
                "match_clock_source": "ESPN commentary play clock and wallclock",
            }
            for horizon in ingame_horizons
        ]
    requests.extend(in_game)
    for request in in_game:
        request["match_summary_status"] = (
            summary.get("_summary_status")
            if isinstance(summary, dict) else "unavailable"
        )
    return requests


def report_text(
    listings: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    forecasts: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    calibration: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    comparison: list[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    selected_by_league = {
        league: [row for row in selected if row["league"] == league]
        for league in pipeline.LEAGUES
    }
    outcome_rows = []
    for league, group in selected_by_league.items():
        counts = Counter(row.get("final_match_outcome") or "unresolved" for row in group)
        outcome_rows.append(
            f"| {league} | {len(group)} | {counts['home_win']} | {counts['draw']} | "
            f"{counts['away_win']} | {counts['unresolved']} | "
            f"{sum(row.get('fixture_result_eligible', False) for row in group)} |"
        )
    trace_rows = []
    for league in pipeline.LEAGUES:
        examples = sorted(
            (
                row for row in forecasts
                if row["league"] == league
                and row.get("forecast_horizon") == f"pregame_closing_{config['closing_horizon']}m"
                and row.get("analysis_eligible")
            ),
            key=lambda row: row["fixture_id"],
        )
        if not examples:
            continue
        row = examples[0]
        trace_rows.append(
            f"| {league} | `{row['fixture_id']}`; {row['event_id']}/{row['market_id']} | "
            f"{row.get('home_team')}–{row.get('away_team')} | "
            f"{row.get('requested_timestamp_utc')} | {row.get('selected_observation_timestamp_utc')} | "
            f"{row.get('forecast_probability')} | "
            f"{row.get('final_score_home')}-{row.get('final_score_away')} / "
            f"{row.get('verified_draw')} / {row.get('api_settled_draw')} | "
            f"`{row.get('market_raw_ref')}`; `{row.get('price_history_raw_ref')}`; "
            f"`{row.get('result_source_raw_ref')}` |"
        )
    coverage_rows = "\n".join(
        f"| {row['league']} | {row['forecast_horizon']} | {row['selected_fixture_count']} | "
        f"{row['available_forecasts']} | {row['eligible_forecasts']} | {row['missing_prices']} | "
        f"{row['unmatched_or_uncertain_fixtures']} | {row['uncertain_match_timing']} | "
        f"{row['approximate_match_timing']} | {row['unresolved_outcomes']} |"
        for row in coverage
    )
    calibration_rows = "\n".join(
        f"| {row['league']} | {row['forecast_horizon']} | {row['bin_left']:.2f}–{row['bin_right']:.2f} | "
        f"{row['n']} | {row['draws']} | {row['non_draws']} | {row['mean_forecast_probability']:.3f} | "
        f"{row['observed_frequency']:.3f} | "
        f"[{row['observed_frequency_95ci_low']:.3f}, {row['observed_frequency_95ci_high']:.3f}] |"
        for row in calibration
        if row["league"] == "pooled"
    ) or "| none | — | — | 0 | 0 | 0 | — | — | — |"
    metric_rows = "\n".join(
        f"| {row['league']} | {row['horizon']} | {row['n']} | "
        f"{row['brier_score']:.4f} | {row['log_loss']:.4f} |"
        for row in metrics
    )
    comparison_rows = "\n".join(
        f"| {row['forecast_horizon']} | {row['common_fixture_count']} | "
        f"{row['brier_score_common_set'] if row['brier_score_common_set'] is not None else '—'} | "
        f"{row['paired_brier_difference_vs_closing'] if row['paired_brier_difference_vs_closing'] is not None else '—'} | "
        f"[{row['paired_difference_95ci_low']}, {row['paired_difference_95ci_high']}] |"
        for row in comparison
    )
    dates = [
        pipeline.parse_iso(row.get("scheduled_kickoff_utc"))
        for row in fixtures
        if row.get("fixture_result_eligible")
    ]
    dates = [value for value in dates if value]
    date_range = (
        f"{min(dates).date().isoformat()} to {max(dates).date().isoformat()}"
        if dates else "not available from verified sample results"
    )
    source_counts = Counter(row.get("draw_label_source") or "unresolved" for row in selected)
    unresolved_reasons = Counter(
        row.get("fixture_result_exclusion_reason") or "eligible"
        for row in selected
    )
    reason_lines = "\n".join(f"- `{reason}`: {count}" for reason, count in sorted(unresolved_reasons.items()))
    return (
        "# Calibration data usability report\n\n"
        f"Generated {pipeline.utc_now()}. Rebuild with `python3 scripts/build_calibration_datasets.py`.\n\n"
        "## Sample and definitions\n\n"
        f"- Retrieved listing count: {len(listings)} total listings in the configured series; these are not fixtures.\n"
        f"- Deterministic selected sample: up to {config['sample_per_league']} resolved, unambiguous draw-contract fixtures per league, "
        f"ordered by SHA-256 of seed `{config['seed']}` plus event and market IDs; one fixture per conservative fixture key.\n"
        f"- Selected fixture sample date range (score-verified subset): {date_range}.\n"
        "- Sporting outcomes come from matched, completed ESPN scores (with the Premier League feed used only as an optional conflict check). "
        "Final outcome is home win/draw/away win; the binary draw label is 1 only for a score draw and 0 otherwise.\n"
        "- Gamma/CLOB draw settlement is retained as a separate outcome label and cross-check, not silently substituted for the score-derived label. "
        "Both resolved Yes (1) and No (0) settlements are eligible when they agree with the matched score.\n"
        "- A calibration snapshot is eligible only when fixture identity, timezone-aware kickoff, completed score, resolved settlement, "
        "Gamma/CLOB token orientation, settlement-vs-score checks, and forecast timing are supported and a valid historical draw price is available. "
        "The approximate halftime proxy is kept for audit but excluded.\n\n"
        "| League | Selected fixtures | Home wins | Draws | Away wins | Unresolved scores | Result-eligible fixtures |\n"
        "|---|---:|---:|---:|---:|---:|---:|\n"
        + "\n".join(outcome_rows) + "\n\n"
        "### Traceable closing-pregame examples\n\n"
        "Each row links the forecast cutoff and selected API observation to the market, price-history, and independent result response caches. "
        "The final three values are score-derived draw label / Polymarket settlement label.\n\n"
        "| League | Fixture and event/market IDs | Match | Requested cutoff UTC | Selected observation UTC | p(draw) | Score / labels | Cached evidence: market; price history; result |\n"
        "|---|---|---|---|---|---:|---|---|\n"
        + "\n".join(trace_rows) + "\n\n"
        f"Score-derived label source counts: {dict(source_counts)}.\n\n"
        "## Forecast coverage\n\n"
        f"Pregame cutoffs are {config['pregame_horizons']} minutes before the independent scheduled kickoff where available. "
        f"The {config['closing_horizon']}-minute cutoff is the project's operational closing pregame forecast, not an exchange-defined close. "
        f"Maximum API-observation age is {config['max_lookback_minutes']} minutes pregame and "
        f"{config['max_in_game_age_minutes']} minutes in-game.\n\n"
        "In-game wallclock cutoffs are reconstructed from ESPN commentary play-clock and wallclock records. "
        "Within-period clock brackets are interpolated only when the bracket is no wider than "
        f"{config['max_clock_bracket_minutes']} match-clock minutes and "
        f"{config['max_wallclock_bracket_minutes']} wallclock minutes; forecasts use only Polymarket observations at or before that cutoff. "
        "Halftime uses the last timestamped first-half play as an approximate proxy because ESPN's explicit half-end entry had no wallclock "
        "in the inspected response; that proxy is retained but excluded from calibration. Approximate interpolated match-clock cutoffs are "
        "identified separately from missing/uncertain timing. A 90-minute cutoff is unavailable unless commentary "
        "brackets it and a later timestamped play establishes that the match was still underway; it is not the final whistle.\n\n"
        "| League | Horizon | Selected fixtures | Price available | Analysis eligible | Missing price | Unmatched/uncertain fixture | Missing/uncertain timing | Approximate timing | Unresolved outcome |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        + coverage_rows + "\n\n"
        "## Exploratory draw calibration\n\n"
        "Pooled probability-bin summaries use one forecast per fixture per horizon. The reported 95% intervals are Wilson intervals for observed draw frequency; "
        "they do not account for sample selection or other systematic uncertainty. Sparse bins remain visible. Mean forecast in each bin is the calibration reference.\n\n"
        "| League | Horizon | Probability bin | n | Draws | Non-draws | Mean quoted p | Draw frequency | Frequency 95% CI |\n"
        "|---|---|---|---:|---:|---:|---:|---:|---|\n"
        + calibration_rows + "\n\n"
        "| League | Horizon | n | Brier | Log loss |\n|---|---|---:|---:|---:|\n"
        + metric_rows + "\n\n"
        "The reliability diagram in `data/processed/reliability.png` includes the identity line, Wilson intervals, and bin counts for pooled horizons with data.\n\n"
        "### Common-fixture horizon comparison\n\n"
        "Rows below use fixtures with eligible snapshots at every configured pregame horizon. The paired Brier difference is horizon minus the configured closing horizon; "
        "confidence intervals bootstrap whole fixtures, preserving repeated forecasts from a fixture.\n\n"
        "| Horizon | Common fixtures | Common-set Brier | Paired Brier difference vs closing | Fixture-bootstrap 95% CI |\n"
        "|---|---:|---:|---:|---|\n"
        + comparison_rows + "\n\n"
        "## Exclusions, limitations, and outputs\n\n"
        + reason_lines + "\n\n"
        "- A historical observation's age is API-observation age, not proven price staleness. Polymarket documentation and experiments do not establish whether `p` is a trade, midpoint, or fidelity-bucket statistic.\n"
        "- Rescheduled or otherwise ambiguous fixture identities are retained but excluded from calibration pending review. Conflicting ESPN/official scores and mismatches between final score and Polymarket settlement are preserved in `fixture_result_audit.csv`.\n"
        "- An API settlement mismatch may reflect exceptional contract resolution, such as a cancellation rule, rather than a bad score. Those rows are excluded and require contract-specific review.\n"
        "- The sample is deterministic and reproducible, but it is a hashed sample of resolved draw-market fixtures, not a population census; listings without a resolved full-match draw contract are outside the sampling frame.\n"
        "- See `fixtures.csv`, `historical_prices.csv`, `forecast_snapshots.csv`, `snapshot_coverage.csv`, `in_game_timing.csv`, `horizon_comparison.csv`, and cached raw response references for row-level evidence.\n"
    )


def run(args: argparse.Namespace) -> None:
    api = pipeline.API(refresh=args.refresh, delay=args.rate_delay)
    listings, page_refs = pipeline.fetch_listings(api, max_pages=args.max_pages)
    evidence_rows, fixture_rows = pipeline.audit_rows(listings)
    candidates = pipeline.fixture_candidate_pairs(fixture_rows)
    candidate_evidence = pipeline.audit_fixture_candidates(api, candidates, evidence_rows)
    pilots = pipeline.select_pilot(evidence_rows, args.sample_per_league, args.seed)
    result_records: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    token_audits: list[dict[str, Any]] = []
    price_observations_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    fixture_facts: dict[tuple[str, str], dict[str, Any]] = {}
    timing_rows: list[dict[str, Any]] = []
    for index, row in enumerate(pilots, start=1):
        token_audit = pipeline.audit_token_mapping(api, row)
        token_audits.append(token_audit)
        espn, espn_error = pipeline.fetch_espn_result(api, row)
        official = (
            pipeline.fetch_pl_official(api, row)
            if row["league"] == "Premier League"
            else {"source": None, "status": "not_queried"}
        )
        summary = None
        summary_ref = None
        if espn and espn.get("event_id") and espn.get("score_match_status") == "matched":
            summary, summary_ref = pipeline.fetch_espn_summary(api, row["league"], str(espn["event_id"]))
        facts = fixture_result_record(
            row,
            token_audit,
            espn,
            espn_error,
            official,
            args.kickoff_tolerance_minutes,
        )
        facts["sample_selection_rule"] = "stable SHA-256 order within league; one row per exact fixture key"
        facts["sample_seed"] = args.seed
        facts["result_summary_raw_ref"] = summary_ref
        facts["result_summary_match_status"] = (
            summary.get("_summary_status") if isinstance(summary, dict) else "unavailable"
        )
        commentary = (
            summary.get("commentary", [])
            if isinstance(summary, dict) and summary.get("_summary_status") == "matched"
            else []
        )
        if isinstance(commentary, list):
            kickoff_play = next((
                item.get("play") or {}
                for item in commentary
                if "first half begins" in str(item.get("text") or "").casefold()
            ), {})
            actual_kickoff = pipeline.parse_iso(kickoff_play.get("wallclock"))
            facts["actual_kickoff_utc"] = actual_kickoff.isoformat() if actual_kickoff else None
        fixture_facts[(row["league"], row["event_id"])] = facts
        result_records.append(facts)

        scheduled_kickoff = pipeline.parse_iso(facts.get("scheduled_kickoff_utc"))
        requests = build_requests(
            scheduled_kickoff,
            summary,
            args.pregame_horizons,
            args.closing_horizon,
            args.ingame_horizons,
            args.max_clock_bracket_minutes,
            args.max_wallclock_bracket_minutes,
        )
        for request in requests:
            request["match_summary_raw_ref"] = summary_ref
            request["market_resolution_status"] = row.get("settlement_status")
        valid_targets = [
            pipeline.parse_iso(request.get("requested_timestamp_utc"))
            for request in requests
        ]
        valid_targets = [target for target in valid_targets if target is not None]
        observations: list[dict[str, Any]] = []
        price_ref = None
        if valid_targets:
            observations, price_ref = pipeline.collect_price_window(
                api,
                row,
                min(valid_targets),
                max(valid_targets),
                max(args.max_lookback_minutes, args.max_in_game_age_minutes),
            )
        facts["price_history_raw_ref"] = price_ref
        for observation in observations:
            key = (
                observation["fixture_id"],
                observation["token_id"],
                observation["unix_timestamp_seconds"],
            )
            previous = price_observations_by_key.get(key)
            if previous and previous["probability"] != observation["probability"]:
                raise ValueError(f"Conflicting price observations for {key}")
            price_observations_by_key[key] = observation
        for request in requests:
            max_age = (
                args.max_in_game_age_minutes
                if request.get("horizon_type") == "in_game_match_clock"
                else args.max_lookback_minutes
            )
            snapshot = pipeline.build_forecast_snapshot(
                facts,
                request,
                observations,
                max_age,
            )
            snapshot["status"] = snapshot["price_observation_status"]
            snapshot["independent_result_source_status"] = facts["draw_label_source"]
            snapshot["forecast_source_raw_ref"] = price_ref
            snapshot["market_rules"] = row.get("market_rules_text")
            snapshot.update({
                "token_id": row["yes_token_id"],
                "token_mapping_status": token_audit.get("token_mapping_status"),
                "settlement_crosscheck_status": token_audit.get("settlement_crosscheck_status"),
                "api_settled_draw": row.get("api_settled_draw"),
                "market_resolution_source": row.get("market_resolution_source"),
                "market_raw_ref": row.get("source_raw_ref"),
            })
            forecasts.append(snapshot)
            if request.get("horizon_type") == "in_game_match_clock":
                timing_rows.append({
                    "league": row["league"],
                    "fixture_id": row["fixture_id"],
                    "event_id": row["event_id"],
                    "market_id": row["market_id"],
                    **request,
                    "match_summary_raw_ref": summary_ref,
                })
        if index % 25 == 0:
            print(f"processed {index}/{len(pilots)} selected fixtures", file=sys.stderr)

    fixture_rows = update_fixture_table(fixture_rows, fixture_facts)
    for row in evidence_rows:
        facts = fixture_facts.get((row["league"], row["event_id"]))
        if facts:
            row.update({
                "fixture_result_eligible": facts["fixture_result_eligible"],
                "fixture_result_exclusion_reason": facts["fixture_result_exclusion_reason"],
                "final_score_home": facts["final_score_home"],
                "final_score_away": facts["final_score_away"],
                "final_match_outcome": facts["final_match_outcome"],
                "final_draw_label": facts["final_draw_label"],
                "draw_label_source": facts["draw_label_source"],
                "result_source_raw_ref": facts["result_source_raw_ref"],
            })

    check_results = pipeline.validate_analysis_rows(evidence_rows, forecasts)
    observations = list(price_observations_by_key.values())
    observations.sort(key=lambda item: (item["fixture_id"], item["unix_timestamp_seconds"]))
    analysis_keys = [
        (row["fixture_id"], row["outcome_type"], row["forecast_horizon"])
        for row in forecasts
    ]
    if len(analysis_keys) != len(set(analysis_keys)):
        raise ValueError("Duplicate fixture/outcome/horizon snapshots were generated")
    if len(observations) != len({
        (row["fixture_id"], row["token_id"], row["unix_timestamp_seconds"])
        for row in observations
    }):
        raise ValueError("Duplicate historical price observations were generated")

    calibration, metrics = pipeline.calibration_rows(forecasts, args.bin_edges)
    pregame_names = [
        pipeline.horizon_name(value, args.closing_horizon)
        for value in args.pregame_horizons
    ]
    closing_name = pipeline.horizon_name(args.closing_horizon, args.closing_horizon)
    comparison = pipeline.common_horizon_comparison(
        forecasts,
        pregame_names,
        closing_name,
        args.bootstrap_replicates,
        args.seed,
    )
    coverage = pipeline.snapshot_coverage(forecasts, list(pipeline.LEAGUES))
    age_sensitivity = []
    for league in pipeline.LEAGUES:
        for horizon in sorted({row["forecast_horizon"] for row in forecasts if row["league"] == league}):
            horizon_rows = [
                row for row in forecasts
                if row["league"] == league and row["forecast_horizon"] == horizon
            ]
            standard_thresholds = (1, 5, 10) if horizon.startswith("ingame_") else (30, 60, 180, 360)
            maximum_age = (
                args.max_in_game_age_minutes
                if horizon.startswith("ingame_")
                else args.max_lookback_minutes
            )
            thresholds = sorted(set((*standard_thresholds, maximum_age)))
            for threshold in thresholds:
                available_count, eligible_count = age_sensitivity_counts(horizon_rows, threshold)
                age_sensitivity.append({
                    "league": league,
                    "forecast_horizon": horizon,
                    "maximum_observation_age_minutes": threshold,
                    "selected_fixture_count": len(horizon_rows),
                    "forecasts_within_age_limit": available_count,
                    "eligible_forecasts_within_age_limit": eligible_count,
                })

    pipeline.write_csv(PROCESSED / "listings.csv", [
        {
            "league": item["league"],
            "event_id": item["event"].get("id"),
            "title": item["event"].get("title"),
            "slug": item["event"].get("slug"),
            "closed": item["event"].get("closed"),
            "event_date": item["event"].get("eventDate"),
            "start_time": item["event"].get("startTime"),
            "series": item["event"].get("series"),
            "raw_ref": item["raw_ref"],
        }
        for item in listings
    ])
    pipeline.write_csv(PROCESSED / "fixtures.csv", fixture_rows)
    pipeline.write_csv(PROCESSED / "fixture_result_audit.csv", result_records)
    pipeline.write_csv(PROCESSED / "outcome_discrepancies.csv", [
        row for row in result_records
        if row.get("settlement_matches_score_label") is False
        or row.get("cross_provider_score_conflict")
        or row.get("final_draw_label") is None
    ])
    pipeline.write_csv(PROCESSED / "validation_evidence.csv", evidence_rows)
    pipeline.write_csv(PROCESSED / "fixture_candidates.csv", candidate_evidence)
    pipeline.write_csv(PROCESSED / "token_orientation_audit.csv", token_audits)
    pipeline.write_csv(PROCESSED / "historical_prices.csv", observations)
    pipeline.write_csv(PROCESSED / "price_observations.csv", observations)
    pipeline.write_csv(PROCESSED / "forecast_snapshots.csv", forecasts)
    pipeline.write_csv(PROCESSED / "forecasts.csv", forecasts)
    pipeline.write_csv(PROCESSED / "snapshot_coverage.csv", coverage)
    pipeline.write_csv(PROCESSED / "forecast_age_sensitivity.csv", age_sensitivity)
    pipeline.write_csv(PROCESSED / "in_game_timing.csv", timing_rows)
    pipeline.write_csv(PROCESSED / "horizon_comparison.csv", comparison)
    pipeline.write_csv(PROCESSED / "calibration.csv", calibration)
    pipeline.write_csv(PROCESSED / "calibration_metrics.csv", metrics)
    pipeline.create_reliability_plot(calibration)
    (PROCESSED / "validation_checks.json").write_text(
        json.dumps(check_results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    by_league = {}
    for league in pipeline.LEAGUES:
        group = [row for row in result_records if row["league"] == league]
        by_league[league] = {
            "selected_fixtures": len(group),
            "result_eligible_fixtures": sum(bool(row["fixture_result_eligible"]) for row in group),
            "home_wins": sum(row["final_match_outcome"] == "home_win" for row in group),
            "draws": sum(row["final_match_outcome"] == "draw" for row in group),
            "away_wins": sum(row["final_match_outcome"] == "away_win" for row in group),
            "unresolved_scores": sum(row["final_match_outcome"] is None for row in group),
            "settlement_conflicts": sum(row["settlement_matches_score_label"] is False for row in group),
            "ambiguous_fixture_identities": sum(not row["fixture_result_eligible"] and "fixture_identity_uncertain" in str(row["fixture_result_exclusion_reason"]) for row in group),
        }
    coverage_document = {
        "generated_at_utc": pipeline.utc_now(),
        "listings_are_not_unique_fixtures": True,
        "retrieved_listings": len(listings),
        "listing_page_raw_refs": page_refs,
        "sample_selection": {
            "rule": "stable SHA-256 ordering by seed/event/market within league; one fixture key each",
            "seed": args.seed,
            "maximum_per_league": args.sample_per_league,
            "selected_count": len(pilots),
        },
        "by_league": by_league,
        "pregame_horizons_minutes": args.pregame_horizons,
        "closing_pregame_horizon_minutes": args.closing_horizon,
        "ingame_horizons": args.ingame_horizons,
        "max_pregame_observation_age_minutes": args.max_lookback_minutes,
        "max_ingame_observation_age_minutes": args.max_in_game_age_minutes,
        "maximum_clock_interpolation_bracket_minutes": args.max_clock_bracket_minutes,
        "maximum_wallclock_interpolation_bracket_minutes": args.max_wallclock_bracket_minutes,
        "snapshot_coverage": coverage,
        "validation_checks": check_results,
        "price_semantics": "API-reported historical price; trade/midpoint/bucket meaning not established",
    }
    (PROCESSED / "coverage.json").write_text(
        json.dumps(coverage_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_config = {
        "seed": args.seed,
        "sample_per_league": args.sample_per_league,
        "pregame_horizons": args.pregame_horizons,
        "closing_horizon": args.closing_horizon,
        "ingame_horizons": args.ingame_horizons,
        "max_lookback_minutes": args.max_lookback_minutes,
        "max_in_game_age_minutes": args.max_in_game_age_minutes,
        "max_clock_bracket_minutes": args.max_clock_bracket_minutes,
        "max_wallclock_bracket_minutes": args.max_wallclock_bracket_minutes,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        report_text(
            listings, result_records, fixture_rows, forecasts, coverage,
            calibration, metrics, comparison, report_config,
        ),
        encoding="utf-8",
    )
    print(json.dumps({
        "listings": len(listings),
        "selected_fixtures": len(pilots),
        "result_eligible_fixtures": sum(row["fixture_result_eligible"] for row in result_records),
        "historical_price_observations": len(observations),
        "forecast_snapshots": len(forecasts),
        "eligible_snapshots": sum(bool(row["analysis_eligible"]) for row in forecasts),
        "calibration_bins": len(calibration),
        "report": str(REPORT.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Refresh live API responses and keep prior snapshots")
    parser.add_argument("--max-pages", type=int, help="Optional per-league pagination cap for a smoke run")
    parser.add_argument("--sample-per-league", type=int, default=50, help="Hash-selected resolved fixtures per league")
    parser.add_argument("--seed", default="calibration-datasets-v1")
    parser.add_argument(
        "--pregame-horizons",
        type=parse_pregame_horizons,
        default=pipeline.DEFAULT_PREGAME_HORIZONS_MINUTES,
    )
    parser.add_argument("--closing-horizon", type=int, default=5, help="Operational closing pregame horizon in minutes")
    parser.add_argument(
        "--ingame-horizons",
        type=parse_ingame_horizons,
        default=pipeline.DEFAULT_INGAME_HORIZONS,
    )
    parser.add_argument("--max-lookback-minutes", type=int, default=360)
    parser.add_argument("--max-in-game-age-minutes", type=int, default=10)
    parser.add_argument("--kickoff-tolerance-minutes", type=int, default=15)
    parser.add_argument("--max-clock-bracket-minutes", type=int, default=5)
    parser.add_argument("--max-wallclock-bracket-minutes", type=int, default=10)
    parser.add_argument("--bootstrap-replicates", type=int, default=1000)
    parser.add_argument("--rate-delay", type=float, default=pipeline.RATE_DELAY_SECONDS)
    parser.add_argument(
        "--bin-edges",
        default=",".join(str(edge) for edge in pipeline.PRICE_BINS),
        help="Comma-separated increasing probability-bin edges from 0 to 1",
    )
    args = parser.parse_args()
    if args.sample_per_league < 1:
        parser.error("--sample-per-league must be positive")
    if args.closing_horizon <= 0 or args.closing_horizon not in args.pregame_horizons:
        parser.error("--closing-horizon must be a positive member of --pregame-horizons")
    if args.max_lookback_minutes < 1 or args.max_in_game_age_minutes < 1:
        parser.error("observation-age limits must be positive")
    if (
        args.kickoff_tolerance_minutes < 0
        or args.max_clock_bracket_minutes < 1
        or args.max_wallclock_bracket_minutes < 1
    ):
        parser.error("kickoff tolerance must be nonnegative and clock brackets must be positive")
    if args.bootstrap_replicates < 1 or args.rate_delay < 0:
        parser.error("bootstrap count must be positive and rate delay nonnegative")
    try:
        args.bin_edges = [float(edge) for edge in args.bin_edges.split(",")]
    except ValueError:
        parser.error("--bin-edges must be comma-separated numbers")
    if (
        len(args.bin_edges) < 2
        or args.bin_edges[0] != 0
        or args.bin_edges[-1] != 1
        or any(left >= right for left, right in zip(args.bin_edges, args.bin_edges[1:]))
    ):
        parser.error("bin edges must be strictly increasing from 0 to 1")
    run(args)


if __name__ == "__main__":
    main()
