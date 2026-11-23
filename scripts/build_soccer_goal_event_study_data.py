#!/usr/bin/env python3
"""Build all-fixture FotMob goal events and scoring-team price coverage."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import requests

if __package__:
    from . import soccer_goal_response_pilot as pilot
    from . import validate_pipeline as pipeline
else:
    import soccer_goal_response_pilot as pilot
    import validate_pipeline as pipeline


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw"
FIXTURES_PATH = PROCESSED / "fixtures.csv"
GOALS_PATH = PROCESSED / "soccer_all_goals.csv"
COVERAGE_PATH = PROCESSED / "soccer_goal_fixture_coverage.csv"
PRICES_PATH = PROCESSED / "soccer_goal_event_price_history.csv"
PILOT_PRICES_PATH = PROCESSED / "soccer_three_way_price_history.csv"
FOTMOB_LEAGUE_IDS = {
    "Premier League": 47,
    "La Liga": 87,
    "Serie A": 55,
    "Bundesliga": 54,
    "Ligue 1": 53,
}
PERIOD_BASE_MINUTES = {
    "firsthalf": ("firstHalfStarted", 0, "FirstHalf"),
    "secondhalf": ("secondHalfStarted", 45, "SecondHalf"),
    "firstextrahalf": ("firstExtraHalfStarted", 90, "FirstExtraHalf"),
    "secondextrahalf": ("secondExtraHalfStarted", 105, "SecondExtraHalf"),
}
GOAL_COLUMNS = [
    "fixture_id", "event_id", "fotmob_match_id", "fotmob_event_id",
    "fotmob_event_order",
    "league", "match_date", "home_team", "away_team", "scoring_side",
    "scoring_team", "scorer", "scorer_side", "own_goal",
    "own_goal_received_side", "period", "raw_period", "displayed_goal_minute",
    "added_time_minute", "raw_event_time", "raw_timeStr", "raw_overloadTime",
    "raw_overloadTimeStr", "score_after_goal_home", "score_after_goal_away",
    "score_after_goal", "goal_utc_lower", "goal_utc_upper",
    "clock_anchor_used", "clock_anchor_raw", "clock_anchor_utc",
    "clock_anchor_quality", "independently_verified_clock_anchor",
    "timestamp_quality_flags", "fotmob_detail_raw_ref", "raw_goal_event_json",
]
PRICE_COLUMNS = [
    "fixture_id", "event_id", "league", "home_team", "away_team",
    "outcome", "market_id", "condition_id", "token_id",
    "observation_timestamp_utc", "probability", "source_ref",
    "token_mapping_status", "gamma_event_raw_ref",
]


def parse_fixture_season(value: Any) -> str:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Fixture date is missing or invalid: {value!r}")
    year = timestamp.year if timestamp.month >= 7 else timestamp.year - 1
    return f"{year}/{year + 1}"


def fixture_season(fixture: dict[str, Any]) -> str:
    for field in ("event_date", "kickoff_utc", "scheduled_kickoff_utc"):
        value = fixture.get(field)
        if value is None or pd.isna(value) or not str(value).strip():
            continue
        try:
            return parse_fixture_season(value)
        except ValueError:
            continue
    raise ValueError(f"Fixture {fixture.get('fixture_id')} has no usable date")


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None or pd.isna(value) or not str(value).strip():
            continue
        return value
    return None


def read_cached_season(league: str, season: str, *, refresh: bool) -> tuple[list[dict[str, Any]], str]:
    path = RAW / "fotmob_league_fixtures" / (
        f"{league.casefold().replace(' ', '-')}-{season.replace('/', '-')}.json"
    )
    payload, ref = pilot.cache_json(
        pilot.FOTMOB + "/leagues",
        {
            "id": FOTMOB_LEAGUE_IDS[league],
            "ccode3": "USA",
            "season": season,
        },
        path,
        refresh=refresh,
    )
    matches = (((payload or {}).get("fixtures") or {}).get("allMatches") or [])
    if not isinstance(matches, list):
        raise ValueError(f"FotMob {league} {season} response has no allMatches list")
    return matches, ref


def fixture_matches(fixture: dict[str, Any], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scheduled = pd.to_datetime(
        fixture.get("kickoff_utc") or fixture.get("scheduled_kickoff_utc"),
        utc=True,
        errors="coerce",
    )
    event_date = str(fixture.get("event_date") or "")[:10]
    home = pipeline.normalize_team(str(fixture.get("home_team") or ""))
    away = pipeline.normalize_team(str(fixture.get("away_team") or ""))
    found = []
    for match in matches:
        if (
            home != pipeline.normalize_team(str((match.get("home") or {}).get("name") or ""))
            or away != pipeline.normalize_team(str((match.get("away") or {}).get("name") or ""))
        ):
            continue
        match_time = pd.to_datetime(
            (match.get("status") or {}).get("utcTime"), utc=True, errors="coerce"
        )
        same_date = pd.notna(match_time) and match_time.date().isoformat() == event_date
        near_kickoff = (
            pd.notna(scheduled)
            and pd.notna(match_time)
            and abs((match_time - scheduled).total_seconds()) <= 3 * 3600
        )
        if same_date or near_kickoff:
            found.append(match)
    return found


def normalize_period(value: Any) -> str | None:
    if not value:
        return None
    compact = re.sub(r"[^a-z]", "", str(value).casefold())
    aliases = {
        "firsthalf": "FirstHalf",
        "secondhalf": "SecondHalf",
        "firstextrahalf": "FirstExtraHalf",
        "secondextrahalf": "SecondExtraHalf",
        "extrafirsthalf": "FirstExtraHalf",
        "extrasecondhalf": "SecondExtraHalf",
    }
    return aliases.get(compact)


def parse_minute_and_added(event: dict[str, Any]) -> tuple[int | None, int | None]:
    minute = None
    try:
        minute = int(event.get("time"))
    except (TypeError, ValueError):
        pass
    display = str(event.get("timeStr") or "")
    display_match = re.search(r"(\d+)\s*(?:\+\s*(\d+))?", display)
    if minute is None and display_match:
        minute = int(display_match.group(1))
    added = None
    try:
        raw_added = event.get("overloadTime")
        if raw_added not in (None, False, "") and not isinstance(raw_added, bool):
            added = int(event["overloadTime"])
    except (TypeError, ValueError):
        pass
    if added is None and display_match and display_match.group(2):
        added = int(display_match.group(2))
    return minute, (added if added is not None else (0 if minute is not None else None))


def score_after_goal(event: dict[str, Any]) -> tuple[int | None, int | None]:
    score = event.get("newScore")
    if isinstance(score, (list, tuple)) and len(score) == 2:
        try:
            return int(score[0]), int(score[1])
        except (TypeError, ValueError):
            pass
    return None, None


def _event_identifier(event: dict[str, Any]) -> Any:
    event_id = event.get("eventId")
    return event_id if event_id is not None else event.get("id")


def goal_side(
    event: dict[str, Any],
    score_before_home: Any,
    score_before_away: Any,
    after_home: int | None,
    after_away: int | None,
) -> tuple[str | None, str | None]:
    if after_home is not None and after_away is not None:
        try:
            before_home, before_away = int(score_before_home), int(score_before_away)
            if after_home > before_home and after_away == before_away:
                return "home", "score_increment"
            if after_away > before_away and after_home == before_home:
                return "away", "score_increment"
        except (TypeError, ValueError):
            pass
    own_goal = is_own_goal(event)
    is_home = event.get("isHome")
    if isinstance(is_home, bool):
        if own_goal:
            return ("away" if is_home else "home"), "own_goal_side_inferred_from_scorer_side"
        return ("home" if is_home else "away"), "isHome_fallback"
    return None, "scoring_side_unavailable"


def is_own_goal(event: dict[str, Any]) -> bool:
    shot = event.get("shotmapEvent") or {}
    value = event.get("ownGoal")
    return value is True or shot.get("isOwnGoal") is True


def extract_goal_events(details: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    container = (
        (((details.get("content") or {}).get("matchFacts") or {}).get("events") or {})
    )
    raw_events = container.get("events") if isinstance(container, dict) else None
    if not isinstance(raw_events, list):
        return [], False
    goals = []
    for raw_order, event in enumerate(raw_events):
        if not isinstance(event, dict) or str(event.get("type") or "").casefold() != "goal":
            continue
        if event.get("isPenaltyShootoutEvent") is True:
            continue
        shot = event.get("shotmapEvent") or {}
        period = normalize_period(shot.get("period") or event.get("period"))
        minute, added = parse_minute_and_added(event)
        period_inferred = False
        if period is None and minute is not None:
            if minute <= 45:
                period = "FirstHalf"
            elif minute <= 90:
                period = "SecondHalf"
            elif minute <= 105:
                period = "FirstExtraHalf"
            elif minute <= 120:
                period = "SecondExtraHalf"
            period_inferred = period is not None
        after_home, after_away = score_after_goal(event)
        side, side_source = goal_side(
            event,
            event.get("homeScore"),
            event.get("awayScore"),
            after_home,
            after_away,
        )
        if after_home is None or after_away is None:
            try:
                before_home, before_away = int(event.get("homeScore")), int(event.get("awayScore"))
            except (TypeError, ValueError):
                before_home = before_away = None
            if before_home is not None and before_away is not None and side in {"home", "away"}:
                after_home = before_home + int(side == "home")
                after_away = before_away + int(side == "away")
        goals.append({
            "event": event,
            "event_id": _event_identifier(event),
            "raw_order": raw_order,
            "period": period,
            "period_inferred": period_inferred,
            "minute": minute,
            "added": added,
            "after_home": after_home,
            "after_away": after_away,
            "side": side,
            "side_source": side_source,
        })
    goals.sort(key=lambda goal: (
        {"FirstHalf": 1, "SecondHalf": 2, "FirstExtraHalf": 3, "SecondExtraHalf": 4}.get(
            goal["period"], 99
        ),
        goal["minute"] if goal["minute"] is not None else 999,
        goal["added"] if goal["added"] is not None else 999,
        goal["raw_order"],
    ))
    return goals, True


def _anchor_datetime(
    raw: Any,
    *,
    offset_minutes: int | None,
    timezone_name: Any = None,
) -> tuple[datetime | None, str, bool]:
    if not raw:
        return None, "missing_anchor", False
    text = str(raw).strip()
    try:
        aware = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if aware.tzinfo is not None and aware.utcoffset() is not None:
            return aware.astimezone(timezone.utc), "raw_timezone_aware", True
    except ValueError:
        pass
    try:
        naive = datetime.strptime(text, "%d.%m.%Y %H:%M:%S")
    except ValueError:
        return None, "unparseable_anchor", False
    if timezone_name:
        try:
            local = naive.replace(tzinfo=ZoneInfo(str(timezone_name)))
            return local.astimezone(timezone.utc), "explicit_timezone", True
        except (ValueError, ZoneInfoNotFoundError):
            return None, "invalid_explicit_timezone", False
    if offset_minutes is None:
        return None, "offset_unverified", False
    return (
        naive.replace(tzinfo=timezone(timedelta(minutes=int(offset_minutes)))).astimezone(timezone.utc),
        "kickoff_crosschecked_offset_inferred",
        False,
    )


def infer_offset(
    halfs: dict[str, Any],
    kickoff: Any,
    fotmob_utc: Any,
    manual_timezone: Any = None,
) -> tuple[int | None, str]:
    if manual_timezone:
        return None, "explicit_timezone"
    _, offset, status = pilot.infer_half_start_from_kickoff(
        halfs.get("firstHalfStarted"), kickoff, fotmob_utc
    )
    return offset, status


def get_goal_bounds(
    period: str | None,
    minute: int | None,
    added: int | None,
    halfs: dict[str, Any],
    offset_minutes: int | None,
    timezone_name: Any,
) -> tuple[datetime | None, datetime | None, str, datetime | None, str, bool]:
    if not period or period.casefold() not in PERIOD_BASE_MINUTES:
        return None, None, "", None, "period_unknown", False
    anchor_field, base_minute, _ = PERIOD_BASE_MINUTES[period.casefold()]
    raw_anchor = halfs.get(anchor_field)
    anchor, quality, independent = _anchor_datetime(
        raw_anchor,
        offset_minutes=offset_minutes,
        timezone_name=timezone_name,
    )
    if anchor is None:
        return None, None, anchor_field, None, quality, independent
    if period == "SecondHalf":
        first_start, _, _ = _anchor_datetime(
            halfs.get("firstHalfStarted"),
            offset_minutes=offset_minutes,
            timezone_name=timezone_name,
        )
        first_end, _, _ = _anchor_datetime(
            halfs.get("firstHalfEnded"),
            offset_minutes=offset_minutes,
            timezone_name=timezone_name,
        )
        if first_start is not None:
            elapsed = (anchor - first_start).total_seconds() / 60
            if not 40 <= elapsed <= 75:
                return None, None, anchor_field, anchor, "second_half_anchor_conflicts_with_first_half_start", independent
        if first_end is not None:
            break_minutes = (anchor - first_end).total_seconds() / 60
            if not 0 <= break_minutes <= 35:
                return None, None, anchor_field, anchor, "second_half_anchor_conflicts_with_first_half_end", independent
    if minute is None or added is None:
        return None, None, anchor_field, anchor, "goal_minute_unavailable", independent
    elapsed = minute + added - base_minute
    if elapsed < 0:
        return None, None, anchor_field, anchor, "goal_minute_conflicts_with_period_anchor", independent
    lower = anchor + timedelta(minutes=max(0, elapsed - 1))
    upper = anchor + timedelta(minutes=elapsed)
    return lower, upper, anchor_field, anchor, quality, independent


def fotmob_score(status: dict[str, Any]) -> tuple[int | None, int | None]:
    score = str(status.get("scoreStr") or "")
    match = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", score)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def _number(value: Any) -> int | None:
    try:
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _fixture_result_score(fixture: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    home = _number(fixture.get("final_score_home"))
    away = _number(fixture.get("final_score_away"))
    source = fixture.get("draw_label_source")
    if home is None or away is None:
        return None, None, None
    return home, away, str(source or "fixture inventory verified score")


def make_goal_rows(
    fixture: dict[str, Any],
    match_id: str,
    goals: list[dict[str, Any]],
    details: dict[str, Any],
    details_ref: str,
    offset_minutes: int | None,
    timezone_name: Any,
    halfs: dict[str, Any],
) -> list[dict[str, Any]]:
    result = []
    for order, goal in enumerate(goals, start=1):
        event = goal["event"]
        lower, upper, anchor_used, anchor, anchor_quality, independent = get_goal_bounds(
            goal["period"],
            goal["minute"],
            goal["added"],
            halfs,
            offset_minutes,
            timezone_name,
        )
        own = is_own_goal(event)
        side = goal["side"]
        scorer_side = (
            "home" if event.get("isHome") is True
            else "away" if event.get("isHome") is False
            else None
        )
        flags = []
        if goal["event_id"] in (None, 0, "0", ""):
            flags.append("fotmob_event_id_missing_or_zero")
        if goal["period_inferred"]:
            flags.append("period_inferred_from_displayed_minute")
        if goal["minute"] is None:
            flags.append("displayed_minute_missing")
        if goal["added"] is None:
            flags.append("added_time_missing")
        if side is None:
            flags.append("scoring_side_unavailable")
        elif goal["side_source"] != "score_increment":
            flags.append(goal["side_source"])
        if anchor_quality != "raw_timezone_aware":
            flags.append(anchor_quality)
        if anchor_quality in {
            "kickoff_crosschecked_offset_inferred",
            "offset_unverified",
            "missing_anchor",
        }:
            flags.append("timezone_name_unavailable")
        if anchor is None or lower is None:
            flags.append("utc_bounds_unavailable")
        if own:
            flags.append("own_goal_credit_side_uses_score_increment_or_inference")
        scorer = (
            (event.get("player") or {}).get("fullName")
            or (event.get("player") or {}).get("name")
            or event.get("fullName")
            or event.get("nameStr")
        )
        score_text = (
            f"{goal['after_home']}-{goal['after_away']}"
            if goal["after_home"] is not None and goal["after_away"] is not None
            else None
        )
        result.append({
            "fixture_id": str(fixture["fixture_id"]),
            "event_id": str(fixture.get("event_id") or ""),
            "fotmob_match_id": match_id,
            "fotmob_event_id": (
                str(goal["event_id"]) if goal["event_id"] is not None else None
            ),
            "fotmob_event_order": goal["raw_order"],
            "league": fixture.get("league"),
            "match_date": fixture.get("event_date"),
            "home_team": fixture.get("home_team"),
            "away_team": fixture.get("away_team"),
            "scoring_side": side,
            "scoring_team": fixture.get("home_team") if side == "home"
            else fixture.get("away_team") if side == "away" else None,
            "scorer": scorer,
            "scorer_side": scorer_side,
            "own_goal": own,
            "own_goal_received_side": side if own else None,
            "period": goal["period"],
            "raw_period": (event.get("shotmapEvent") or {}).get("period") or event.get("period"),
            "displayed_goal_minute": event.get("timeStr"),
            "added_time_minute": goal["added"],
            "raw_event_time": event.get("time"),
            "raw_timeStr": event.get("timeStr"),
            "raw_overloadTime": event.get("overloadTime"),
            "raw_overloadTimeStr": event.get("overloadTimeStr"),
            "score_after_goal_home": goal["after_home"],
            "score_after_goal_away": goal["after_away"],
            "score_after_goal": score_text,
            "goal_utc_lower": lower.isoformat() if lower else None,
            "goal_utc_upper": upper.isoformat() if upper else None,
            "clock_anchor_used": anchor_used,
            "clock_anchor_raw": halfs.get(anchor_used) if anchor_used else None,
            "clock_anchor_utc": anchor.isoformat() if anchor else None,
            "clock_anchor_quality": anchor_quality,
            "independently_verified_clock_anchor": independent,
            "timestamp_quality_flags": ";".join(flags),
            "fotmob_detail_raw_ref": details_ref,
            "raw_goal_event_json": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            "goal_order": order,
        })
    return result


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def existing_price_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    frame = pd.read_csv(path, dtype={"fixture_id": str, "token_id": str})
    required = set(PRICE_COLUMNS)
    if not required.issubset(frame.columns):
        return []
    return frame[PRICE_COLUMNS].to_dict("records")


def _price_records(
    history: list[dict[str, Any]],
    fixture: dict[str, Any],
    outcome: str,
    market: dict[str, Any],
    token_id: str,
    source_ref: str,
    mapping_status: str,
    gamma_ref: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    records = []
    for item in history:
        try:
            stamp = datetime.fromtimestamp(float(item["t"]), tz=timezone.utc)
            probability = float(item["p"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if start <= stamp <= end and 0 <= probability <= 1:
            records.append({
                "fixture_id": str(fixture["fixture_id"]),
                "event_id": str(fixture.get("event_id") or ""),
                "league": fixture.get("league"),
                "home_team": fixture.get("home_team"),
                "away_team": fixture.get("away_team"),
                "outcome": outcome,
                "market_id": str(market.get("id") or ""),
                "condition_id": str(market.get("conditionId") or ""),
                "token_id": token_id,
                "observation_timestamp_utc": stamp.isoformat(),
                "probability": probability,
                "source_ref": source_ref,
                "token_mapping_status": mapping_status,
                "gamma_event_raw_ref": gamma_ref,
            })
    return records


def fetch_scoring_team_prices(
    fixture: dict[str, Any],
    side: str,
    start: datetime,
    end: datetime,
    api: pipeline.API,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    event, gamma_ref, event_status = pilot.embedded_polymarket_event(
        fixture, api, refresh=False
    )
    result = {
        "price_history_status": event_status,
        "price_market_id": None,
        "price_condition_id": None,
        "price_token_id": None,
        "price_token_mapping_status": None,
        "price_history_raw_ref": None,
        "price_observation_count": 0,
        "polymarket_event_raw_ref": gamma_ref,
    }
    if event is None:
        return [], result
    markets, statuses = pilot.result_markets_by_outcome(event, fixture)
    market = markets.get(side)
    result["price_history_status"] = statuses.get(side, "result_market_unavailable")
    if market is None:
        return [], result
    orientation = pipeline.market_orientation(market)
    token_id = orientation["yes_token_id"]
    condition = str(market.get("conditionId") or "")
    result.update({
        "price_market_id": str(market.get("id") or ""),
        "price_condition_id": condition,
        "price_token_id": token_id,
    })
    if not token_id or not condition:
        result["price_history_status"] = "missing_yes_token_or_condition_id"
        return [], result
    clob_market, market_ref = api.get(
        "clob_market_details", f"{pipeline.CLOB}/markets/{condition}", {}
    )
    crosscheck = pipeline.compare_token_orientation(
        orientation["outcomes"],
        orientation["tokens"],
        clob_market.get("tokens", []) if isinstance(clob_market, dict) else [],
    )
    result["price_token_mapping_status"] = crosscheck["status"]
    result["clob_market_raw_ref"] = market_ref
    if crosscheck["status"] != "matched":
        result["price_history_status"] = "token_mapping_not_verified"
        return [], result
    payload, history_ref = api.get(
        "soccer_goal_clob_prices_history",
        f"{pipeline.CLOB}/prices-history",
        {
            "market": token_id,
            "startTs": int(start.timestamp()),
            "endTs": int(end.timestamp()),
            "fidelity": 1,
        },
    )
    history = payload.get("history", []) if isinstance(payload, dict) else []
    rows = _price_records(
        history, fixture, side, market, token_id, history_ref,
        crosscheck["status"], gamma_ref or "", start, end,
    )
    result["price_history_raw_ref"] = history_ref
    result["price_observation_count"] = len(rows)
    result["price_history_status"] = "history_available" if rows else "empty_price_history"
    return rows, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_PATH)
    parser.add_argument("--goals-output", type=Path, default=GOALS_PATH)
    parser.add_argument("--coverage-output", type=Path, default=COVERAGE_PATH)
    parser.add_argument("--prices-output", type=Path, default=PRICES_PATH)
    parser.add_argument("--limit", type=int, default=None, help="deterministic smoke-test limit")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=0.15)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.delay_seconds < 0:
        parser.error("--delay-seconds cannot be negative")

    fixtures_frame = pd.read_csv(args.fixtures, dtype={"fixture_id": str, "event_id": str})
    fixtures = fixtures_frame.sort_values(["league", "event_id"]).to_dict("records")
    if args.limit is not None:
        fixtures = fixtures[:args.limit]
    season_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    season_refs: dict[tuple[str, str], str] = {}
    season_errors: dict[tuple[str, str], str] = {}
    season_keys = set()
    for row in fixtures:
        league = str(row.get("league") or "")
        if league not in FOTMOB_LEAGUE_IDS:
            continue
        try:
            season_keys.add((league, fixture_season(row)))
        except ValueError:
            continue
    for key in sorted(season_keys):
        league, season = key
        try:
            matches, ref = read_cached_season(league, season, refresh=args.refresh)
            season_cache[key] = matches
            season_refs[key] = ref
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            season_errors[key] = f"{type(error).__name__}:{error}"
        time.sleep(args.delay_seconds)

    existing_prices = existing_price_rows(PILOT_PRICES_PATH)
    price_rows: list[dict[str, Any]] = existing_prices
    price_fixtures = {
        str(row.get("fixture_id"))
        for row in existing_prices
        if row.get("token_mapping_status") == "matched"
    }
    api = pipeline.API(delay=args.delay_seconds)
    goal_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for index, fixture in enumerate(fixtures, start=1):
        fixture_id = str(fixture.get("fixture_id") or "")
        league = str(fixture.get("league") or "")
        try:
            season = fixture_season(fixture)
        except ValueError as error:
            row = {
                "fixture_id": fixture_id,
                "event_id": str(fixture.get("event_id") or ""),
                "league": league,
                "match_date": fixture.get("event_date"),
                "home_team": fixture.get("home_team"),
                "away_team": fixture.get("away_team"),
                "fotmob_match_status": "fixture_date_unavailable",
                "event_feed_status": "unavailable_no_fixture_date",
                "price_history_status": "unavailable_no_fixture_date",
                "goal_count": 0,
                "exclusion_reasons": f"fixture_date_unavailable:{error}",
            }
            coverage_rows.append(row)
            continue
        key = (league, season)
        row: dict[str, Any] = {
            "fixture_id": fixture_id,
            "event_id": str(fixture.get("event_id") or ""),
            "league": league,
            "match_date": fixture.get("event_date"),
            "home_team": fixture.get("home_team"),
            "away_team": fixture.get("away_team"),
            "scheduled_kickoff_utc": first_nonempty(
                fixture.get("kickoff_utc"), fixture.get("scheduled_kickoff_utc")
            ),
            "fotmob_league_season": season,
            "fotmob_fixture_source_ref": season_refs.get(key),
            "fotmob_match_id": None,
            "fotmob_match_status": "not_matched",
            "fotmob_detail_status": "not_requested",
            "fotmob_candidate_ids": "",
            "fotmob_detail_raw_ref": None,
            "fotmob_detail_error": None,
            "fotmob_status_utc": None,
            "event_feed_status": "not_requested",
            "fotmob_finished": None,
            "fotmob_score_home": None,
            "fotmob_score_away": None,
            "verified_final_score_home": _fixture_result_score(fixture)[0],
            "verified_final_score_away": _fixture_result_score(fixture)[1],
            "verified_final_score_source": _fixture_result_score(fixture)[2],
            "goal_count": 0,
            "extracted_home_goals": 0,
            "extracted_away_goals": 0,
            "score_mismatch_with_verified_final": None,
            "score_mismatch_reason": None,
            "scoreless_confirmed": False,
            "firstHalfStarted_raw": None,
            "secondHalfStarted_raw": None,
            "firstHalfEnded_raw": None,
            "half_start_utc": None,
            "half_end_utc": None,
            "half_start_utc_offset_minutes_inferred": None,
            "half_start_clock_quality": None,
            "independently_verified_clock_anchor": False,
            "opening_goal_fotmob_event_id": None,
            "opening_goal_period": None,
            "opening_goal_utc_lower": None,
            "opening_goal_utc_upper": None,
            "opening_goal_scoring_side": None,
            "price_history_status": "not_required",
            "price_market_id": None,
            "price_condition_id": None,
            "price_token_id": None,
            "price_token_mapping_status": None,
            "price_history_raw_ref": None,
            "price_observation_count": 0,
            "price_history_error": None,
            "polymarket_event_raw_ref": None,
            "exclusion_reasons": "",
        }
        matches = season_cache.get(key)
        if matches is None:
            row["fotmob_match_status"] = (
                "season_lookup_failed:" + season_errors.get(key, "not_cached_or_unsupported_league")
            )
            row["event_feed_status"] = "unavailable_no_fixture_crosswalk"
            row["price_history_status"] = "unavailable_no_fixture_crosswalk"
            row["exclusion_reasons"] = "fotmob_season_lookup_unavailable"
            coverage_rows.append(row)
            continue
        candidates = fixture_matches(fixture, matches)
        if len(candidates) == 0:
            row["fotmob_match_status"] = "failed_no_ordered_team_date_or_kickoff_match"
            row["event_feed_status"] = "unavailable_unmatched_fixture"
            row["price_history_status"] = "unavailable_unmatched_fixture"
            row["exclusion_reasons"] = "fotmob_match_not_found"
            coverage_rows.append(row)
            continue
        row["fotmob_candidate_ids"] = "|".join(str(m.get("id") or "") for m in candidates)
        if len(candidates) > 1:
            row["fotmob_match_status"] = "ambiguous_multiple_team_date_matches"
            row["event_feed_status"] = "unavailable_ambiguous_fixture"
            row["price_history_status"] = "unavailable_ambiguous_fixture"
            row["exclusion_reasons"] = "fotmob_match_ambiguous"
            coverage_rows.append(row)
            continue
        match = candidates[0]
        match_id = str(match.get("id") or "")
        status_hint = match.get("status") or {}
        row["fotmob_match_id"] = match_id
        row["fotmob_match_status"] = "matched_by_ordered_teams_and_date_or_kickoff"
        finished_hint = status_hint.get("finished")
        started_hint = status_hint.get("started")
        row["fotmob_finished"] = finished_hint
        if status_hint.get("cancelled") is True:
            row["event_feed_status"] = "cancelled_fixture_no_event_feed"
            row["fotmob_detail_status"] = "cancelled_fixture"
            row["price_history_status"] = "not_required_cancelled_fixture"
            row["exclusion_reasons"] = "fotmob_fixture_cancelled"
            coverage_rows.append(row)
            continue
        if finished_hint is False and started_hint is False:
            row["event_feed_status"] = "not_started_no_event_feed"
            row["fotmob_detail_status"] = "not_started"
            row["price_history_status"] = "not_required_match_not_started"
            row["exclusion_reasons"] = "match_not_started"
            coverage_rows.append(row)
            continue
        detail_cache = RAW / "fotmob_match_details" / f"{match_id}.json"
        detail_was_cached = detail_cache.exists() and not args.refresh
        try:
            details, details_ref = pilot.fetch_match_details(match_id, refresh=args.refresh)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            row["fotmob_detail_status"] = f"detail_fetch_failed:{type(error).__name__}"
            row["event_feed_status"] = f"detail_fetch_failed:{type(error).__name__}"
            row["fotmob_detail_error"] = str(error)
            row["price_history_status"] = "not_measured_event_feed_unavailable"
            row["exclusion_reasons"] = "fotmob_match_details_unavailable"
            coverage_rows.append(row)
            continue
        if not detail_was_cached:
            time.sleep(args.delay_seconds)
        row["fotmob_detail_raw_ref"] = details_ref
        row["fotmob_detail_status"] = "fetched"
        header = details.get("header") or {}
        status = header.get("status") or {}
        halfs = status.get("halfs") or {}
        row["fotmob_finished"] = status.get("finished", finished_hint)
        row["fotmob_status_utc"] = status.get("utcTime")
        row["fotmob_score_home"], row["fotmob_score_away"] = fotmob_score(status)
        row["firstHalfStarted_raw"] = halfs.get("firstHalfStarted")
        row["secondHalfStarted_raw"] = halfs.get("secondHalfStarted")
        row["firstHalfEnded_raw"] = halfs.get("firstHalfEnded")
        goals, feed_present = extract_goal_events(details)
        row["event_feed_status"] = (
            "available" if feed_present else "missing_events_array"
        )
        if feed_present and not goals and status.get("finished") is True:
            if row["fotmob_score_home"] == 0 and row["fotmob_score_away"] == 0:
                row["event_feed_status"] = "available_confirmed_scoreless"
                row["scoreless_confirmed"] = True
            else:
                row["event_feed_status"] = "available_no_goal_events_score_nonzero"
        row["goal_count"] = len(goals)
        row["extracted_home_goals"] = sum(goal["side"] == "home" for goal in goals)
        row["extracted_away_goals"] = sum(goal["side"] == "away" for goal in goals)
        verified_home, verified_away, verified_source = _fixture_result_score(fixture)
        if verified_home is not None and verified_away is not None:
            mismatch = (
                row["extracted_home_goals"] != verified_home
                or row["extracted_away_goals"] != verified_away
            )
            row["score_mismatch_with_verified_final"] = mismatch
            row["score_mismatch_reason"] = (
                f"extracted_{row['extracted_home_goals']}-{row['extracted_away_goals']}"
                f"_vs_verified_{verified_home}-{verified_away}_from_{verified_source}"
                if mismatch else None
            )
        if (
            row["fotmob_finished"] is True
            and row["fotmob_score_home"] is not None
            and row["fotmob_score_away"] is not None
            and (
                row["extracted_home_goals"] != row["fotmob_score_home"]
                or row["extracted_away_goals"] != row["fotmob_score_away"]
            )
        ):
            row["fotmob_event_score_mismatch"] = True
            row["fotmob_event_score_mismatch_reason"] = (
                f"events_{row['extracted_home_goals']}-{row['extracted_away_goals']}"
                f"_vs_fotmob_{row['fotmob_score_home']}-{row['fotmob_score_away']}"
            )
        else:
            row["fotmob_event_score_mismatch"] = False
            row["fotmob_event_score_mismatch_reason"] = None

        timezone_name = fixture.get("half_start_timezone")
        offset, offset_status = infer_offset(
            halfs,
            first_nonempty(fixture.get("kickoff_utc"), fixture.get("scheduled_kickoff_utc")),
            status.get("utcTime"),
            timezone_name,
        )
        half_start, half_quality, half_independent = _anchor_datetime(
            halfs.get("firstHalfStarted"),
            offset_minutes=offset,
            timezone_name=timezone_name,
        )
        half_end, _, _ = _anchor_datetime(
            halfs.get("firstHalfEnded"),
            offset_minutes=offset,
            timezone_name=timezone_name,
        )
        row["half_start_utc"] = half_start.isoformat() if half_start else None
        row["half_end_utc"] = half_end.isoformat() if half_end else None
        row["half_start_utc_offset_minutes_inferred"] = offset
        row["half_start_clock_quality"] = (
            "explicit_timezone" if timezone_name else offset_status
        )
        row["independently_verified_clock_anchor"] = half_independent

        goal_records = make_goal_rows(
            fixture, match_id, goals, details, details_ref, offset, timezone_name, halfs
        )
        goal_rows.extend(goal_records)
        if goals:
            first = goals[0]
            row["opening_goal_fotmob_event_id"] = (
                str(first["event_id"]) if first["event_id"] is not None else None
            )
            row["opening_goal_period"] = first["period"]
            row["opening_goal_scoring_side"] = first["side"]
            first_record = next(
                (
                    record for record in goal_records
                    if record["fotmob_event_id"] == (
                        str(first["event_id"]) if first["event_id"] is not None else None
                    )
                ),
                None,
            )
            if first_record:
                row["opening_goal_utc_lower"] = first_record["goal_utc_lower"]
                row["opening_goal_utc_upper"] = first_record["goal_utc_upper"]
        if not goals and not row["scoreless_confirmed"]:
            row["exclusion_reasons"] = "goal_event_feed_missing_or_no_confirmed_scoreless_result"
        if goals and goals[0]["period"] == "FirstHalf":
            if first_record is None or not first_record.get("goal_utc_lower"):
                row["price_history_status"] = "unavailable_goal_utc_alignment"
                row["exclusion_reasons"] = ";".join(filter(None, [
                    row["exclusion_reasons"], "opening_goal_utc_bounds_unavailable",
                ]))
            elif first["side"] not in {"home", "away"}:
                row["price_history_status"] = "unavailable_scoring_side"
                row["exclusion_reasons"] = ";".join(filter(None, [
                    row["exclusion_reasons"], "opening_goal_scoring_side_unavailable",
                ]))
            else:
                half_end_raw = halfs.get("firstHalfEnded")
                half_end, _, _ = _anchor_datetime(
                    half_end_raw,
                    offset_minutes=offset,
                    timezone_name=timezone_name,
                )
                if half_end is None and half_start is not None:
                    half_end = half_start + timedelta(minutes=45)
                if half_start is None or half_end is None:
                    row["price_history_status"] = "unavailable_half_bounds"
                    row["exclusion_reasons"] = ";".join(filter(None, [
                        row["exclusion_reasons"], "first_half_bounds_unavailable",
                    ]))
                else:
                    start = half_start - timedelta(minutes=5)
                    end = half_end + timedelta(minutes=5)
                    fixture_prices = [
                        price for price in price_rows
                        if str(price.get("fixture_id")) == fixture_id
                        and str(price.get("outcome")) == first["side"]
                        and price.get("token_mapping_status") == "matched"
                        and start <= (_parse_utc(price.get("observation_timestamp_utc")) or start - timedelta(seconds=1)) <= end
                    ]
                    if fixture_prices:
                        row["price_history_status"] = "reused_verified_cached_win_history"
                        row["price_observation_count"] = len(fixture_prices)
                        row["price_token_mapping_status"] = "matched"
                        row["price_token_id"] = fixture_prices[0].get("token_id")
                        row["price_market_id"] = fixture_prices[0].get("market_id")
                    else:
                        try:
                            fetched, price_audit = fetch_scoring_team_prices(
                                fixture, first["side"], start, end, api
                            )
                            row.update(price_audit)
                            price_rows.extend(fetched)
                        except (RuntimeError, ValueError, json.JSONDecodeError) as error:
                            row["price_history_status"] = (
                                f"price_history_request_failed:{type(error).__name__}"
                            )
                            row["price_history_error"] = str(error)
                        if not row.get("price_observation_count"):
                            row["exclusion_reasons"] = ";".join(filter(None, [
                                row["exclusion_reasons"], "scoring_team_win_price_history_unavailable",
                            ]))
        elif goals and goals[0]["period"] != "FirstHalf":
            row["price_history_status"] = "not_required_opening_goal_not_first_half"
        elif not goals:
            row["price_history_status"] = (
                "not_required_confirmed_scoreless" if row["scoreless_confirmed"]
                else "unavailable_no_confirmed_goal_status"
            )

        coverage_rows.append(row)
        if index % 25 == 0 or index == len(fixtures):
            print(f"processed {index}/{len(fixtures)} fixtures; goals={len(goal_rows)}")

    goals_frame = pd.DataFrame(goal_rows)
    for column in GOAL_COLUMNS:
        if column not in goals_frame:
            goals_frame[column] = pd.Series(dtype="object")
    goals_frame.to_csv(args.goals_output, index=False, columns=GOAL_COLUMNS + (
        ["goal_order"] if "goal_order" in goals_frame else []
    ))
    coverage_frame = pd.DataFrame(coverage_rows)
    coverage_frame.to_csv(args.coverage_output, index=False)
    prices_frame = pd.DataFrame(price_rows)
    for column in PRICE_COLUMNS:
        if column not in prices_frame:
            prices_frame[column] = pd.Series(dtype="object")
    if not prices_frame.empty:
        prices_frame = prices_frame.drop_duplicates(
            subset=["fixture_id", "outcome", "observation_timestamp_utc", "token_id"],
            keep="last",
        ).sort_values(["fixture_id", "outcome", "observation_timestamp_utc"])
    prices_frame.to_csv(args.prices_output, index=False, columns=PRICE_COLUMNS)
    print(
        f"fixtures={len(coverage_frame)} matched="
        f"{coverage_frame['fotmob_match_status'].str.startswith('matched_').sum()} "
        f"goals={len(goals_frame)} price_rows={len(prices_frame)}"
    )
    for label, output_path in (
        ("goals_csv", args.goals_output),
        ("coverage_csv", args.coverage_output),
        ("prices_csv", args.prices_output),
    ):
        try:
            display_path = output_path.resolve().relative_to(ROOT)
        except ValueError:
            display_path = output_path.resolve()
        print(f"{label}={display_path}")


if __name__ == "__main__":
    main()
