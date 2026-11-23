#!/usr/bin/env python3
"""Feasibility pilot for independently timed EPL first goals and Polymarket prices."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests

if __package__:
    from . import validate_pipeline as pipeline
else:
    import validate_pipeline as pipeline


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw"
FOTMOB = "https://www.fotmob.com/api/data"
DEFAULT_LIMIT = 15
DEFAULT_FRESH_MINUTES = 5
WINDOW_MINUTES = 3
MANUAL_FIELDS = [
    "fixture_id",
    "fotmob_match_id",
    "firstHalfStarted_raw",
    "half_start_utc",
    "half_start_timezone",
    "goal_team_side",
    "goal_minute_display",
    "goal_period",
    "goal_event_json",
]
PRICE_FIELDS = {
    "fixture_id",
    "outcome",
    "observation_timestamp_utc",
    "probability",
}
PRICE_OUTCOMES = ("home", "draw", "away")


def normalize_team(value: Any) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value).casefold())
    if tokens and tokens[0] == "afc":
        tokens.pop(0)
    if tokens[-2:] == ["football", "club"]:
        tokens = tokens[:-2]
    elif tokens and tokens[-1] in {"club", "fc", "afc"}:
        tokens.pop()
    text = "".join(tokens)
    aliases = {
        "brightonandhovealbion": "brighton",
        "brightonhovealbion": "brighton",
        "wolverhamptonwanderers": "wolves",
        "wolves": "wolves",
        "manchestercity": "mancity",
        "manchesterunited": "manunited",
    }
    return aliases.get(text, text)


def cache_json(
    url: str,
    params: dict[str, Any],
    path: Path,
    *,
    refresh: bool = False,
) -> tuple[Any, str]:
    if path.exists() and not refresh:
        envelope = json.loads(path.read_text())
        if envelope.get("http_status") == 200 and "payload" in envelope:
            return envelope["payload"], str(path.relative_to(ROOT))
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; soccer-goal-pilot/1.0)"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    envelope = {
        "source": "FotMob public API",
        "url": response.url,
        "request_parameters": params,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "http_status": response.status_code,
        "response_headers": {
            key: value
            for key, value in response.headers.items()
            if key.casefold() in {"content-type", "etag", "last-modified"}
        },
        "payload": payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    return payload, str(path.relative_to(ROOT))


def fotmob_match_id(match: dict[str, Any]) -> str:
    return str(match.get("id") or "")


def match_identity(fixture: dict[str, Any], match: dict[str, Any]) -> bool:
    return (
        normalize_team(fixture["home_team"]) == normalize_team((match.get("home") or {}).get("name"))
        and normalize_team(fixture["away_team"]) == normalize_team((match.get("away") or {}).get("name"))
    )


def match_candidates(
    fixture: dict[str, Any],
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    kickoff = pd.to_datetime(fixture.get("kickoff_utc"), utc=True, errors="coerce")
    event_date = str(fixture.get("event_date") or "")[:10]
    found = []
    for match in matches:
        if not match_identity(fixture, match):
            continue
        match_time = pd.to_datetime(
            (match.get("status") or {}).get("utcTime"), utc=True, errors="coerce"
        )
        same_date = (
            pd.notna(match_time)
            and match_time.date().isoformat() == event_date
        )
        near_kickoff = (
            pd.notna(kickoff)
            and pd.notna(match_time)
            and abs((match_time - kickoff).total_seconds()) <= 3 * 3600
        )
        if same_date or near_kickoff:
            found.append(match)
    return found


def season_for(date_value: Any) -> str:
    date = pd.Timestamp(date_value)
    start = date.year if date.month >= 7 else date.year - 1
    return f"{start}/{start + 1}"


def get_season_matches(season: str, refresh: bool) -> tuple[list[dict[str, Any]], str]:
    path = RAW / "fotmob_league_fixtures" / f"premier-league-{season.replace('/', '-')}.json"
    payload, ref = cache_json(
        FOTMOB + "/leagues",
        {"id": 47, "ccode3": "USA", "season": season},
        path,
        refresh=refresh,
    )
    matches = ((payload.get("fixtures") or {}).get("allMatches") or [])
    if not isinstance(matches, list):
        raise ValueError(f"FotMob season {season} did not contain fixtures.allMatches")
    return matches, ref


def fetch_match_details(match_id: str, refresh: bool) -> tuple[dict[str, Any], str]:
    path = RAW / "fotmob_match_details" / f"{match_id}.json"
    payload, ref = cache_json(
        FOTMOB + "/matchDetails",
        {"matchId": match_id},
        path,
        refresh=refresh,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"FotMob match {match_id} response was not an object")
    return payload, ref


def search_matches(fixture: dict[str, Any], refresh: bool) -> tuple[list[dict[str, Any]], str]:
    term = f"{fixture['home_team']} {fixture['away_team']}"
    digest = hashlib.sha256(term.encode()).hexdigest()[:20]
    path = RAW / "fotmob_search" / f"{digest}.json"
    payload, ref = cache_json(
        FOTMOB + "/search/suggest",
        {"term": term},
        path,
        refresh=refresh,
    )
    suggestions = []
    sections = payload if isinstance(payload, list) else []
    for section in sections:
        suggestions.extend(
            item for item in section.get("suggestions", [])
            if item.get("type") == "match"
        )
    return suggestions, ref


def extract_goals(details: dict[str, Any]) -> list[dict[str, Any]]:
    events = (
        (((details.get("content") or {}).get("matchFacts") or {}).get("events") or {})
        .get("events", [])
    )
    if not isinstance(events, list):
        return []
    goals = []
    for event in events:
        if str(event.get("type", "")).casefold() != "goal":
            continue
        if event.get("isPenaltyShootoutEvent") is True:
            continue
        shot = event.get("shotmapEvent") or {}
        period = shot.get("period") or event.get("period")
        try:
            minute = int(event.get("time"))
        except (TypeError, ValueError):
            continue
        added = event.get("overloadTime")
        try:
            added_minute = int(added) if added is not None else 0
        except (TypeError, ValueError):
            added_minute = 0
        new_score = event.get("newScore")
        old_home, old_away = event.get("homeScore"), event.get("awayScore")
        side = None
        if isinstance(new_score, list) and len(new_score) == 2:
            try:
                if int(new_score[0]) > int(old_home):
                    side = "home"
                elif int(new_score[1]) > int(old_away):
                    side = "away"
            except (TypeError, ValueError):
                pass
        if side is None and isinstance(event.get("isHome"), bool):
            side = "home" if event["isHome"] else "away"
        goals.append({
            "event": event,
            "minute": minute,
            "added_minute": added_minute,
            "period": period,
            "side": side,
            "sort_key": (minute + added_minute, minute, added_minute),
        })
    return sorted(goals, key=lambda row: row["sort_key"])


def parse_naive_half_start(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%d.%m.%Y %H:%M:%S")
    except ValueError:
        return None


def resolve_half_start(
    raw: Any,
    half_start_utc: Any,
    timezone_name: Any,
) -> tuple[datetime | None, str]:
    if half_start_utc:
        try:
            parsed = datetime.fromisoformat(str(half_start_utc).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None, "manual_half_start_utc_missing_timezone"
            return parsed.astimezone(timezone.utc), "manual_utc"
        except ValueError:
            return None, "invalid_manual_half_start_utc"
    naive = parse_naive_half_start(raw)
    if naive is None:
        return None, "missing_or_unparseable_half_start"
    if not timezone_name:
        return None, "half_start_timezone_unverified"
    try:
        local = naive.replace(tzinfo=ZoneInfo(str(timezone_name)))
    except (ZoneInfoNotFoundError, ValueError):
        return None, "invalid_half_start_timezone"
    return local.astimezone(timezone.utc), "manual_timezone"


def infer_half_start_from_kickoff(
    raw: Any,
    scheduled_kickoff_utc: Any,
    fotmob_status_utc: Any,
) -> tuple[datetime | None, int | None, str]:
    naive = parse_naive_half_start(raw)
    scheduled = pd.to_datetime(scheduled_kickoff_utc, utc=True, errors="coerce")
    fotmob_kickoff = pd.to_datetime(fotmob_status_utc, utc=True, errors="coerce")
    if naive is None or pd.isna(scheduled) or pd.isna(fotmob_kickoff):
        return None, None, "half_start_timezone_unverified_missing_utc_crosscheck"
    if abs((scheduled - fotmob_kickoff).total_seconds()) > 15 * 60:
        return None, None, "half_start_timezone_unverified_kickoff_conflict"
    matching = []
    for offset_minutes in range(-12 * 60, 14 * 60 + 1, 15):
        utc_value = naive.replace(
            tzinfo=timezone(timedelta(minutes=offset_minutes))
        ).astimezone(timezone.utc)
        scheduled_delay = (utc_value - scheduled.to_pydatetime()).total_seconds()
        fotmob_delay = (utc_value - fotmob_kickoff.to_pydatetime()).total_seconds()
        if 0 <= scheduled_delay <= 15 * 60 and 0 <= fotmob_delay <= 15 * 60:
            matching.append((utc_value, offset_minutes))
    if len(matching) != 1:
        return None, None, "half_start_timezone_unverified_nonunique_utc_offset"
    utc_value, offset_minutes = matching[0]
    return utc_value, offset_minutes, "offset_inferred_from_scheduled_and_fotmob_utc;timezone_name_unavailable"


def goal_bounds(
    half_start: datetime | None,
    minute: int | None,
    added_minute: int | None,
) -> tuple[datetime | None, datetime | None]:
    if half_start is None or minute is None:
        return None, None
    minute_end = minute + (added_minute or 0)
    return (
        half_start + timedelta(minutes=max(0, minute_end - 1)),
        half_start + timedelta(minutes=minute_end),
    )


def manual_overrides(path: Path, selected: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=MANUAL_FIELDS)
            writer.writeheader()
            for fixture in selected:
                writer.writerow({**{key: "" for key in MANUAL_FIELDS}, "fixture_id": fixture["fixture_id"]})
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(MANUAL_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks manual columns: {', '.join(sorted(missing))}")
        return {
            str(row["fixture_id"]): row
            for row in reader
            if row.get("fixture_id")
        }


def select_fixtures(
    fixtures_path: Path,
    prices_path: Path,
    limit: int,
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    fixtures = pd.read_csv(fixtures_path, dtype={"fixture_id": str, "event_id": str})
    histories = pd.read_csv(prices_path, dtype={"fixture_id": str, "token_id": str})
    if not {"fixture_id", "observation_timestamp_utc"}.issubset(histories.columns):
        raise ValueError("Historical prices require fixture_id and observation_timestamp_utc")
    histories["timestamp"] = pd.to_datetime(
        histories["observation_timestamp_utc"], utc=True, errors="coerce"
    )
    epl = fixtures[fixtures["league"].eq("Premier League")].copy()
    eligible = []
    for row in epl.to_dict("records"):
        kickoff = pd.to_datetime(row.get("kickoff_utc"), utc=True, errors="coerce")
        if pd.isna(kickoff):
            continue
        samples = histories[histories["fixture_id"].eq(str(row["fixture_id"]))]
        within_first_half = samples[
            samples["timestamp"].between(
                kickoff, kickoff + pd.Timedelta(minutes=45), inclusive="both"
            )
        ]
        if len(within_first_half) >= 10:
            row["legacy_price_observation_count_first_45m"] = int(len(within_first_half))
            row["selection_hash"] = hashlib.sha256(
                str(row["fixture_id"]).encode("utf-8")
            ).hexdigest()
            eligible.append(row)
    eligible.sort(key=lambda row: (row["selection_hash"], row["fixture_id"]))
    return eligible[:limit], histories


def series_by_fixture(prices_path: Path | None) -> dict[str, pd.DataFrame]:
    if prices_path is None or not prices_path.exists():
        return {}
    prices = pd.read_csv(prices_path, dtype={"fixture_id": str, "token_id": str})
    missing = PRICE_FIELDS - set(prices.columns)
    if missing:
        raise ValueError(
            f"Three-way prices file {prices_path} lacks: {', '.join(sorted(missing))}"
        )
    prices["timestamp"] = pd.to_datetime(
        prices["observation_timestamp_utc"], utc=True, errors="coerce"
    )
    prices["probability"] = pd.to_numeric(prices["probability"], errors="coerce")
    prices["outcome"] = prices["outcome"].astype(str).str.casefold()
    prices = prices[
        prices["outcome"].isin({"home", "draw", "away"})
        & prices["timestamp"].notna()
        & prices["probability"].between(0, 1)
    ]
    return {
        str(fixture_id): group.sort_values("timestamp")
        for fixture_id, group in prices.groupby("fixture_id")
    }


def embedded_polymarket_event(
    fixture: dict[str, Any],
    api: pipeline.API,
    refresh: bool,
) -> tuple[dict[str, Any] | None, str | None, str]:
    raw_ref = fixture.get("market_raw_ref")
    if not refresh and isinstance(raw_ref, str) and raw_ref and raw_ref.casefold() != "nan":
        path = ROOT / raw_ref
        if path.exists():
            envelope = json.loads(path.read_text())
            payload = envelope.get("payload", {})
            events = payload.get("events", []) if isinstance(payload, dict) else []
            matches = [
                event for event in events
                if str(event.get("id")) == str(fixture.get("event_id"))
            ]
            if len(matches) == 1:
                return matches[0], raw_ref, "matched_cached_gamma_event"
            if len(matches) > 1:
                return None, raw_ref, "ambiguous_cached_gamma_event_id"
    payload, ref = api.get(
        "gamma_event_details",
        f"{pipeline.GAMMA}/events/{fixture['event_id']}",
        {},
    )
    if not isinstance(payload, dict) or str(payload.get("id")) != str(fixture["event_id"]):
        return None, ref, "gamma_event_detail_id_mismatch"
    return payload, ref, "matched_fetched_gamma_event"


def result_markets_by_outcome(
    event: dict[str, Any],
    fixture: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    markets = event.get("markets") or []
    found: dict[str, list[dict[str, Any]]] = {name: [] for name in PRICE_OUTCOMES}
    for market in markets:
        orientation = pipeline.market_orientation(market)
        if not orientation["orientation_valid"]:
            continue
        if pipeline.draw_market_reasons(market):
            found["draw"].append(market)
            continue
        match = re.match(
            r"^\s*will\s+(.+?)\s+win(?:\s+on\b|[?.!]|$)",
            str(market.get("question") or ""),
            re.IGNORECASE,
        )
        if not match:
            continue
        team = pipeline.normalize_team(match.group(1))
        if team == pipeline.normalize_team(fixture["home_team"]):
            found["home"].append(market)
        elif team == pipeline.normalize_team(fixture["away_team"]):
            found["away"].append(market)
    selected: dict[str, dict[str, Any]] = {}
    status = {}
    for outcome, choices in found.items():
        if len(choices) == 1:
            selected[outcome] = choices[0]
            status[outcome] = "matched"
        elif not choices:
            status[outcome] = "missing_result_contract"
        else:
            status[outcome] = "ambiguous_result_contract"
    return selected, status


def first_half_price_window(
    row: dict[str, Any],
    details: dict[str, Any] | None,
    lookback_minutes: float,
) -> tuple[datetime | None, datetime | None]:
    if not row.get("half_start_utc") or not details:
        return None, None
    try:
        half_start = datetime.fromisoformat(row["half_start_utc"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, None
    half_end_raw = (
        (((details.get("header") or {}).get("status") or {}).get("halfs") or {})
        .get("firstHalfEnded")
    )
    half_end_naive = parse_naive_half_start(half_end_raw)
    if half_end_naive is None:
        return None, None
    offset = row.get("half_start_utc_offset_minutes_inferred")
    if offset is not None and not pd.isna(offset):
        half_end = half_end_naive.replace(
            tzinfo=timezone(timedelta(minutes=int(offset)))
        ).astimezone(timezone.utc)
    else:
        half_end, _ = resolve_half_start(
            half_end_raw, None, row.get("half_start_timezone")
        )
    if half_end is None:
        return None, None
    return (
        half_start.astimezone(timezone.utc) - timedelta(minutes=lookback_minutes),
        half_end + timedelta(minutes=lookback_minutes),
    )


def price_history_records(
    history: list[dict[str, Any]],
    fixture: dict[str, Any],
    outcome: str,
    market: dict[str, Any],
    token_id: str,
    source_ref: str,
    token_status: str,
    market_ref: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    records = []
    for item in history:
        try:
            timestamp = datetime.fromtimestamp(float(item["t"]), tz=timezone.utc)
            probability = float(item["p"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if timestamp < start or timestamp > end or not 0 <= probability <= 1:
            continue
        records.append({
            "fixture_id": str(fixture["fixture_id"]),
            "event_id": str(fixture["event_id"]),
            "league": fixture["league"],
            "home_team": fixture["home_team"],
            "away_team": fixture["away_team"],
            "outcome": outcome,
            "market_id": str(market.get("id") or ""),
            "condition_id": str(market.get("conditionId") or ""),
            "token_id": token_id,
            "observation_timestamp_utc": timestamp.isoformat(),
            "probability": probability,
            "source_ref": source_ref,
            "token_mapping_status": token_status,
            "gamma_event_raw_ref": market_ref,
        })
    return records


def fetch_result_price_series(
    fixture: dict[str, Any],
    row: dict[str, Any],
    details: dict[str, Any] | None,
    histories: pd.DataFrame,
    api: pipeline.API,
    refresh: bool,
    lookback_minutes: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    audit: dict[str, Any] = {"polymarket_event_raw_ref": None}
    for outcome in PRICE_OUTCOMES:
        for prefix in (f"{outcome}_win",) if outcome != "draw" else ("draw",):
            audit.update({
                f"{prefix}_market_status": "not_requested",
                f"{prefix}_market_id": None,
                f"{prefix}_condition_id": None,
                f"{prefix}_yes_token_id": None,
                f"{prefix}_token_mapping_status": None,
                f"{prefix}_clob_market_raw_ref": None,
                f"{prefix}_price_history_raw_ref": None,
                f"{prefix}_price_observation_count": 0,
            })
    if row.get("first_goal_period") != "FirstHalf" or not row.get("goal_utc_lower"):
        for outcome in PRICE_OUTCOMES:
            prefix = "draw" if outcome == "draw" else f"{outcome}_win"
            audit[f"{prefix}_market_status"] = "not_requested_no_first_half_goal_utc"
        return [], audit
    start, end = first_half_price_window(row, details, lookback_minutes)
    if start is None or end is None:
        for outcome in PRICE_OUTCOMES:
            prefix = "draw" if outcome == "draw" else f"{outcome}_win"
            audit[f"{prefix}_market_status"] = "first_half_price_window_unavailable"
        return [], audit
    event, event_ref, event_status = embedded_polymarket_event(
        fixture, api, refresh
    )
    audit["polymarket_event_raw_ref"] = event_ref
    if event is None:
        for outcome in PRICE_OUTCOMES:
            prefix = "draw" if outcome == "draw" else f"{outcome}_win"
            audit[f"{prefix}_market_status"] = event_status
        return [], audit
    markets, market_statuses = result_markets_by_outcome(event, fixture)
    records: list[dict[str, Any]] = []
    fixture_history = histories[histories["fixture_id"].eq(str(fixture["fixture_id"]))]
    for outcome in PRICE_OUTCOMES:
        prefix = "draw" if outcome == "draw" else f"{outcome}_win"
        market = markets.get(outcome)
        audit[f"{prefix}_market_status"] = market_statuses[outcome]
        if market is None:
            continue
        orientation = pipeline.market_orientation(market)
        token_id = orientation["yes_token_id"]
        condition_id = str(market.get("conditionId") or "")
        audit[f"{prefix}_market_id"] = str(market.get("id") or "")
        audit[f"{prefix}_condition_id"] = condition_id
        audit[f"{prefix}_yes_token_id"] = token_id
        if not token_id or not condition_id:
            audit[f"{prefix}_market_status"] = "missing_yes_token_or_condition_id"
            continue

        try:
            clob_market, clob_ref = api.get(
                "clob_market_details",
                f"{pipeline.CLOB}/markets/{condition_id}",
                {},
            )
            crosscheck = pipeline.compare_token_orientation(
                orientation["outcomes"],
                orientation["tokens"],
                clob_market.get("tokens", []) if isinstance(clob_market, dict) else [],
            )
            token_status = crosscheck["status"]
        except RuntimeError as error:
            token_status = f"clob_market_request_failed:{error}"
        audit[f"{prefix}_token_mapping_status"] = token_status
        audit[f"{prefix}_clob_market_raw_ref"] = clob_ref
        if token_status not in {"matched", "matched_existing_draw_mapping"}:
            audit[f"{prefix}_market_status"] = "token_mapping_not_verified"
            continue

        history_ref = None
        history: list[dict[str, Any]] = []
        if outcome == "draw":
            draw_rows = fixture_history[
                fixture_history["token_id"].astype(str).eq(token_id)
            ]
            draw_rows = draw_rows[
                draw_rows["timestamp"].between(
                    pd.Timestamp(start), pd.Timestamp(end), inclusive="both"
                )
            ]
            history = [
                {"t": timestamp.timestamp(), "p": float(probability)}
                for timestamp, probability in zip(
                    draw_rows["timestamp"], draw_rows["probability"]
                )
                if pd.notna(timestamp) and pd.notna(probability)
            ]
            history_ref = (
                str(draw_rows["source_ref"].iloc[0])
                if not draw_rows.empty else None
            )
        if not history:
            try:
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
            except RuntimeError as error:
                audit[f"{prefix}_market_status"] = f"price_history_request_failed:{error}"
                continue
        records.extend(price_history_records(
            history,
            fixture,
            outcome,
            market,
            token_id,
            history_ref or "",
            token_status,
            event_ref or "",
            start,
            end,
        ))
        audit[f"{prefix}_price_history_raw_ref"] = history_ref
        audit[f"{prefix}_price_observation_count"] = sum(
            record["fixture_id"] == str(fixture["fixture_id"])
            and record["outcome"] == outcome
            for record in records
        )
        audit[f"{prefix}_market_status"] = (
            "history_available" if audit[f"{prefix}_price_observation_count"]
            else "empty_price_history"
        )
    return records, audit


def fresh_bracket(
    rows: pd.DataFrame,
    start: datetime,
    end: datetime,
    max_age: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    before = rows[rows["timestamp"] < pd.Timestamp(start)]
    after = rows[rows["timestamp"] > pd.Timestamp(end)]
    if before.empty or after.empty:
        return None, None
    pre = before.iloc[-1]
    post = after.iloc[0]
    pre_distance = (start - pre["timestamp"].to_pydatetime()).total_seconds() / 60
    post_distance = (post["timestamp"].to_pydatetime() - end).total_seconds() / 60
    if pre_distance > max_age or post_distance > max_age:
        return None, None
    return pre.to_dict(), post.to_dict()


def delta_record(
    rows: pd.DataFrame,
    start: datetime,
    end: datetime,
    max_age: float,
) -> dict[str, Any]:
    pre, post = fresh_bracket(rows, start, end, max_age)
    if pre is None or post is None:
        return {
            "move": None,
            "before_timestamp": None,
            "after_timestamp": None,
            "before_distance_minutes": None,
            "after_distance_minutes": None,
            "status": "missing_or_stale_observation",
        }
    return {
        "move": float(post["probability"] - pre["probability"]),
        "before_timestamp": pre["timestamp"].isoformat(),
        "after_timestamp": post["timestamp"].isoformat(),
        "before_distance_minutes": (start - pre["timestamp"].to_pydatetime()).total_seconds() / 60,
        "after_distance_minutes": (post["timestamp"].to_pydatetime() - end).total_seconds() / 60,
        "status": "measured",
    }


def choose_control(
    rows: pd.DataFrame,
    goal_start: datetime,
    goal_end: datetime,
    half_start: datetime,
    half_end: datetime,
    goal_intervals: list[tuple[datetime, datetime]],
    scoring_outcome: str,
    max_age: float,
) -> tuple[datetime | None, datetime | None]:
    del scoring_outcome  # Coverage and event times only determine control placement.
    duration = goal_end - goal_start
    latest_start = half_end - duration
    if latest_start < half_start:
        return None, None
    possible = []
    start = half_start
    while start <= latest_start:
        end = start + duration
        if end <= goal_start or start >= goal_end:
            if not any(start <= other_end and end >= other_start for other_start, other_end in goal_intervals):
                pre, post = fresh_bracket(rows, start, end, max_age)
                if pre is not None and post is not None:
                    distance = abs(((start + (end - start) / 2) - (goal_start + (goal_end - goal_start) / 2)).total_seconds())
                    possible.append((distance, start, end))
        start += timedelta(minutes=1)
    if not possible:
        return None, None
    _, chosen_start, chosen_end = min(possible, key=lambda item: (item[0], item[1]))
    return chosen_start, chosen_end


def row_for_fixture(
    fixture: dict[str, Any],
    season_matches: list[dict[str, Any]] | None,
    match_source_ref: str | None,
    overrides: dict[str, str],
    histories: pd.DataFrame,
    team_prices: dict[str, pd.DataFrame],
    max_age: float,
    refresh: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    result: dict[str, Any] = {
        "fixture_id": fixture["fixture_id"],
        "event_id": fixture["event_id"],
        "event_date": fixture["event_date"],
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "scheduled_kickoff_utc": fixture.get("kickoff_utc"),
        "selection_rule": "EPL fixtures with >=10 cached price timestamps during kickoff-to-45m; lowest SHA256(fixture_id), first 15",
        "selection_hash": fixture["selection_hash"],
        "legacy_draw_history_first_45m_count": fixture["legacy_price_observation_count_first_45m"],
        "fotmob_crosswalk_source_ref": match_source_ref,
        "fotmob_match_id": overrides.get("fotmob_match_id") or None,
        "fotmob_match_status": "not_matched",
        "fotmob_candidate_ids": "",
        "fotmob_detail_raw_ref": None,
        "fotmob_status_utc": None,
        "polymarket_event_raw_ref": None,
        "firstHalfStarted_raw": overrides.get("firstHalfStarted_raw") or None,
        "half_start_utc": overrides.get("half_start_utc") or None,
        "half_start_timezone": overrides.get("half_start_timezone") or None,
        "half_start_timezone_status": None,
        "half_start_utc_offset_minutes_inferred": None,
        "half_start_timezone_review_flag": True,
        "raw_naive_half_start_minus_scheduled_kickoff_minutes": None,
        "first_goal_event_id": None,
        "first_goal_type": None,
        "first_goal_minute": None,
        "first_goal_added_minute": None,
        "first_goal_minute_display": overrides.get("goal_minute_display") or None,
        "first_goal_period": overrides.get("goal_period") or None,
        "first_goal_is_home": None,
        "scoring_side": overrides.get("goal_team_side") or None,
        "scoring_team": None,
        "first_goal_raw_json": overrides.get("goal_event_json") or None,
        "goal_utc_lower": None,
        "goal_utc_upper": None,
        "goal_window_plot_start_utc": None,
        "goal_window_plot_end_utc": None,
        "goal_before_price_timestamp_utc": None,
        "goal_after_price_timestamp_utc": None,
        "goal_before_distance_to_interval_minutes": None,
        "goal_after_distance_to_interval_minutes": None,
        "goal_window_signed_price_change": None,
        "goal_price_status": "not_measured",
        "control_window_start_utc": None,
        "control_window_end_utc": None,
        "control_before_price_timestamp_utc": None,
        "control_after_price_timestamp_utc": None,
        "control_before_distance_minutes": None,
        "control_after_distance_minutes": None,
        "control_window_signed_price_change": None,
        "control_price_status": "not_measured",
        "exclusion_reasons": "",
    }
    for outcome in PRICE_OUTCOMES:
        prefix = "draw" if outcome == "draw" else f"{outcome}_win"
        result.update({
            f"{prefix}_market_status": "not_fetched",
            f"{prefix}_market_id": None,
            f"{prefix}_condition_id": None,
            f"{prefix}_yes_token_id": None,
            f"{prefix}_token_mapping_status": None,
            f"{prefix}_clob_market_raw_ref": None,
            f"{prefix}_price_history_raw_ref": None,
            f"{prefix}_price_observation_count": 0,
        })
    raw_draws = histories[histories["fixture_id"].eq(str(fixture["fixture_id"]))]
    result["legacy_draw_history_total_count"] = int(len(raw_draws))
    candidates = match_candidates(fixture, season_matches or [])
    if len(candidates) == 1:
        match = candidates[0]
        result["fotmob_match_id"] = fotmob_match_id(match)
        result["fotmob_match_status"] = "matched_by_ordered_teams_and_date_or_kickoff"
        result["fotmob_candidate_ids"] = result["fotmob_match_id"]
    elif len(candidates) > 1:
        result["fotmob_match_status"] = "ambiguous_multiple_team_date_matches"
        result["fotmob_candidate_ids"] = "|".join(fotmob_match_id(item) for item in candidates)
    else:
        result["fotmob_match_status"] = "failed_no_ordered_team_date_or_kickoff_match"

    details = None
    if result["fotmob_match_id"]:
        try:
            details, detail_ref = fetch_match_details(result["fotmob_match_id"], refresh=refresh)
            result["fotmob_detail_raw_ref"] = detail_ref
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            result["fotmob_match_status"] = f"detail_fetch_failed:{type(error).__name__}:{error}"
    if details:
        status = ((details.get("header") or {}).get("status") or {})
        result["fotmob_status_utc"] = status.get("utcTime")
        result["firstHalfStarted_raw"] = result["firstHalfStarted_raw"] or (status.get("halfs") or {}).get("firstHalfStarted")
        goals = extract_goals(details)
        if goals and not result["first_goal_minute_display"]:
            first = goals[0]
            event = first["event"]
            result.update({
                "first_goal_event_id": event.get("eventId"),
                "first_goal_type": event.get("type"),
                "first_goal_minute": first["minute"],
                "first_goal_added_minute": first["added_minute"],
                "first_goal_minute_display": event.get("timeStr"),
                "first_goal_period": first["period"],
                "first_goal_is_home": event.get("isHome"),
                "scoring_side": first["side"],
                "first_goal_raw_json": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            })
        elif not goals:
            result["first_goal_period"] = result["first_goal_period"] or "no_goal_event_found"

    if result["first_goal_raw_json"] and not result["first_goal_event_id"]:
        try:
            event = json.loads(result["first_goal_raw_json"])
            goals = extract_goals({
                "content": {"matchFacts": {"events": {"events": [event]}}}
            })
            if goals:
                first = goals[0]
                result.update({
                    "first_goal_event_id": event.get("eventId"),
                    "first_goal_type": event.get("type"),
                    "first_goal_minute": first["minute"],
                    "first_goal_added_minute": first["added_minute"],
                    "first_goal_period": first["period"] or result["first_goal_period"],
                    "first_goal_is_home": event.get("isHome"),
                    "scoring_side": result["scoring_side"] or first["side"],
                })
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    elif result["first_goal_minute_display"] and not result["first_goal_minute"]:
        minute_parts = re.fullmatch(
            r"\s*(\d+)(?:\s*\+\s*(\d+))?\s*['′]?\s*",
            str(result["first_goal_minute_display"]),
        )
        if minute_parts:
            result["first_goal_minute"] = int(minute_parts.group(1))
            result["first_goal_added_minute"] = int(minute_parts.group(2) or 0)

    half_start, half_status = resolve_half_start(
        result["firstHalfStarted_raw"],
        result["half_start_utc"],
        result["half_start_timezone"],
    )
    if half_start is None and not result["half_start_utc"] and not result["half_start_timezone"]:
        half_start, offset_minutes, half_status = infer_half_start_from_kickoff(
            result["firstHalfStarted_raw"],
            fixture.get("kickoff_utc"),
            result["fotmob_status_utc"],
        )
        if half_start is not None:
            result["half_start_utc"] = half_start.isoformat()
            result["half_start_utc_offset_minutes_inferred"] = offset_minutes
    result["half_start_timezone_status"] = half_status
    kickoff = pd.to_datetime(fixture.get("kickoff_utc"), utc=True, errors="coerce")
    raw_half = parse_naive_half_start(result["firstHalfStarted_raw"])
    if raw_half is not None and pd.notna(kickoff):
        result["raw_naive_half_start_minus_scheduled_kickoff_minutes"] = (
            raw_half - kickoff.tz_convert(None).to_pydatetime()
        ).total_seconds() / 60
    first_half = str(result["first_goal_period"] or "").casefold() == "firsthalf"
    if not result["first_goal_minute"]:
        reasons = ["no_first_goal_fields"]
    elif not first_half:
        reasons = ["opening_goal_not_confirmed_in_first_half"]
    else:
        reasons = []
    if not result["fotmob_match_status"].startswith("matched_"):
        reasons.append(result["fotmob_match_status"])
    if first_half:
        lower, upper = goal_bounds(
            half_start,
            result["first_goal_minute"],
            result["first_goal_added_minute"],
        )
        if lower is not None and upper is not None:
            result["goal_utc_lower"] = lower.isoformat()
            result["goal_utc_upper"] = upper.isoformat()
            result["goal_window_plot_start_utc"] = (lower - timedelta(minutes=WINDOW_MINUTES)).isoformat()
            result["goal_window_plot_end_utc"] = (upper + timedelta(minutes=WINDOW_MINUTES)).isoformat()
        else:
            reasons.append(half_status)
    scoring_side = result["scoring_side"]
    if scoring_side not in {"home", "away"}:
        reasons.append("scoring_side_unavailable")
    else:
        result["scoring_team"] = fixture["home_team"] if scoring_side == "home" else fixture["away_team"]
    if first_half and result["goal_utc_lower"] and scoring_side:
        if str(fixture["fixture_id"]) not in team_prices:
            reasons.append("scoring_team_win_price_history_unavailable")
        else:
            side_prices = team_prices[str(fixture["fixture_id"])]
            side_prices = side_prices[side_prices["outcome"].eq(scoring_side)]
            if side_prices.empty:
                reasons.append("scoring_team_win_price_history_unavailable")
            else:
                lower = datetime.fromisoformat(result["goal_utc_lower"])
                upper = datetime.fromisoformat(result["goal_utc_upper"])
                goal_move = delta_record(side_prices, lower, upper, max_age)
                result["goal_before_price_timestamp_utc"] = goal_move["before_timestamp"]
                result["goal_after_price_timestamp_utc"] = goal_move["after_timestamp"]
                result["goal_before_distance_to_interval_minutes"] = goal_move["before_distance_minutes"]
                result["goal_after_distance_to_interval_minutes"] = goal_move["after_distance_minutes"]
                result["goal_window_signed_price_change"] = goal_move["move"]
                result["goal_price_status"] = goal_move["status"]
                if goal_move["move"] is None:
                    reasons.append("goal_window_price_missing_or_stale")

    result["exclusion_reasons"] = ";".join(dict.fromkeys(reasons))
    return result, details


def write_price_history(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "fixture_id", "event_id", "league", "home_team", "away_team", "outcome",
        "market_id", "condition_id", "token_id", "observation_timestamp_utc",
        "probability", "source_ref", "token_mapping_status", "gamma_event_raw_ref",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def measure_controls(
    rows: list[dict[str, Any]],
    details_by_fixture: dict[str, dict[str, Any]],
    team_prices: dict[str, pd.DataFrame],
    max_age: float,
) -> None:
    for row in rows:
        if not row.get("goal_utc_lower") or row.get("scoring_side") not in {"home", "away"}:
            continue
        fixture_id = str(row["fixture_id"])
        if fixture_id not in team_prices:
            continue
        outcome_rows = team_prices[fixture_id]
        outcome_rows = outcome_rows[outcome_rows["outcome"].eq(row["scoring_side"])]
        if outcome_rows.empty:
            row["exclusion_reasons"] = ";".join(filter(None, [
                row["exclusion_reasons"], "scoring_team_win_price_history_unavailable",
            ]))
            continue
        details = details_by_fixture.get(fixture_id) or {}
        halfs = (((details.get("header") or {}).get("status") or {}).get("halfs") or {})
        half_start, _ = resolve_half_start(
            row.get("firstHalfStarted_raw"),
            row.get("half_start_utc"),
            row.get("half_start_timezone"),
        )
        half_end_raw = parse_naive_half_start(halfs.get("firstHalfEnded"))
        half_end = None
        if half_end_raw is not None and row.get("half_start_utc_offset_minutes_inferred") is not None:
            half_end = half_end_raw.replace(
                tzinfo=timezone(timedelta(minutes=int(row["half_start_utc_offset_minutes_inferred"])))
            ).astimezone(timezone.utc)
        if half_end is None:
            half_end, _ = resolve_half_start(
                halfs.get("firstHalfEnded"),
                None,
                row.get("half_start_timezone"),
            )
        if half_end is None and half_start is not None:
            half_end = half_start + timedelta(minutes=45)
        if half_start is None or half_end is None:
            row["exclusion_reasons"] = ";".join(filter(None, [
                row["exclusion_reasons"], "first_half_bounds_unavailable_for_control",
            ]))
            continue
        lower = datetime.fromisoformat(row["goal_utc_lower"])
        upper = datetime.fromisoformat(row["goal_utc_upper"])
        expanded_start = lower - timedelta(minutes=WINDOW_MINUTES)
        expanded_end = upper + timedelta(minutes=WINDOW_MINUTES)
        # All first-half goal intervals are excluded from potential control windows.
        intervals = [(lower, upper)]
        goals = extract_goals(details)
        for goal in goals:
            if str(goal["period"]).casefold() != "firsthalf":
                continue
            other_lower, other_upper = goal_bounds(
                half_start, goal["minute"], goal["added_minute"]
            )
            if other_lower is not None and other_upper is not None:
                intervals.append((other_lower, other_upper))
        control_start, control_end = choose_control(
            outcome_rows,
            expanded_start,
            expanded_end,
            half_start,
            half_end,
            intervals,
            row["scoring_side"],
            max_age,
        )
        goal_move = delta_record(outcome_rows, lower, upper, max_age)
        row["goal_before_price_timestamp_utc"] = goal_move["before_timestamp"]
        row["goal_after_price_timestamp_utc"] = goal_move["after_timestamp"]
        row["goal_before_distance_to_interval_minutes"] = goal_move["before_distance_minutes"]
        row["goal_after_distance_to_interval_minutes"] = goal_move["after_distance_minutes"]
        row["goal_window_signed_price_change"] = goal_move["move"]
        row["goal_price_status"] = goal_move["status"]
        if control_start is None:
            row["exclusion_reasons"] = ";".join(filter(None, [
                row["exclusion_reasons"], "no_goal_free_control_with_fresh_prices",
            ]))
            continue
        control_move = delta_record(outcome_rows, control_start, control_end, max_age)
        row["control_window_start_utc"] = control_start.isoformat()
        row["control_window_end_utc"] = control_end.isoformat()
        row["control_before_price_timestamp_utc"] = control_move["before_timestamp"]
        row["control_after_price_timestamp_utc"] = control_move["after_timestamp"]
        row["control_before_distance_minutes"] = control_move["before_distance_minutes"]
        row["control_after_distance_minutes"] = control_move["after_distance_minutes"]
        row["control_window_signed_price_change"] = control_move["move"]
        row["control_price_status"] = control_move["status"]
        if goal_move["move"] is None or control_move["move"] is None:
            row["exclusion_reasons"] = ";".join(filter(None, [
                row["exclusion_reasons"], "goal_or_control_price_missing_or_stale",
            ]))


def save_paired_plot(rows: list[dict[str, Any]], path: Path) -> None:
    usable = [
        row for row in rows
        if row.get("goal_window_signed_price_change") is not None
        and row.get("control_window_signed_price_change") is not None
    ]
    fig, ax = plt.subplots(figsize=(10, max(4, 0.34 * len(usable) + 1.5)))
    if usable:
        for index, row in enumerate(usable):
            goal = float(row["goal_window_signed_price_change"])
            control = float(row["control_window_signed_price_change"])
            ax.plot([goal, control], [index, index], color="#aab4c3", linewidth=1, zorder=1)
            ax.scatter(goal, index, color="#1769aa", label="Goal window" if index == 0 else None, zorder=2)
            ax.scatter(control, index, color="#d97706", label="Control window" if index == 0 else None, zorder=2)
        ax.set_yticks(range(len(usable)), [str(row["fixture_id"]) for row in usable])
        ax.legend()
        ax.set_xlabel("Scoring-team win probability change (after minus before)")
    else:
        ax.text(
            0.5,
            0.52,
            "No paired fixture has both a UTC goal interval and a fresh scoring-team win-price series.\n"
            "No draw-contract proxy or price interpolation was used.",
            ha="center",
            va="center",
            transform=ax.transAxes,
            wrap=True,
        )
        ax.set_yticks([])
        ax.set_xticks([])
    ax.axvline(0, color="#5b6470", linewidth=0.8, linestyle="--")
    ax.set_title(f"First-goal move vs matched control (paired usable fixtures: {len(usable)})")
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def save_price_panels(
    rows: list[dict[str, Any]],
    prices: dict[str, pd.DataFrame],
    path: Path,
) -> None:
    available = []
    for row in rows:
        group = prices.get(str(row["fixture_id"]))
        if group is not None and {"home", "draw", "away"}.issubset(set(group["outcome"])):
            if row.get("goal_utc_lower"):
                available.append((row, group))
    available = available[:3]
    if not available:
        fig, ax = plt.subplots(figsize=(9, 3.5))
        ax.text(
            0.5, 0.5,
            "No cached 3-way home/draw/away win-price histories are available.\n"
            "The existing EPL history file contains binary draw-contract prices only.",
            ha="center", va="center", transform=ax.transAxes,
        )
        ax.set_axis_off()
        fig.tight_layout()
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=160)
        plt.close(fig)
        return
    fig, axes = plt.subplots(len(available), 1, figsize=(10, 3.1 * len(available)), squeeze=False)
    for ax, (row, group) in zip(axes[:, 0], available):
        lower = pd.Timestamp(row["goal_utc_lower"])
        start = lower - pd.Timedelta(minutes=WINDOW_MINUTES)
        end = pd.Timestamp(row["goal_utc_upper"]) + pd.Timedelta(minutes=WINDOW_MINUTES)
        window = group[group["timestamp"].between(start, end)]
        for outcome, label in (("home", "Home win"), ("draw", "Draw"), ("away", "Away win")):
            series = window[window["outcome"].eq(outcome)]
            ax.plot(series["timestamp"], series["probability"], marker=".", label=label)
        ax.axvspan(pd.Timestamp(row["goal_utc_lower"]), pd.Timestamp(row["goal_utc_upper"]), color="#ef4444", alpha=0.2)
        ax.set_title(f"{row['home_team']} vs {row['away_team']} — goal interval shaded")
        ax.set_ylabel("Probability")
        ax.legend(ncol=3)
        ax.grid(alpha=0.2)
    axes[-1, 0].set_xlabel("UTC")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_report(
    rows: list[dict[str, Any]],
    path: Path,
    fresh_minutes: float,
    three_way_path: Path,
    manual_path: Path,
) -> None:
    attempted = len(rows)
    matched = sum(str(row["fotmob_match_status"]).startswith("matched_") for row in rows)
    first_half_goals = sum(row.get("first_goal_period") == "FirstHalf" for row in rows)
    usable = [
        row for row in rows
        if row.get("goal_window_signed_price_change") is not None
        and row.get("control_window_signed_price_change") is not None
    ]
    goal_moves = [float(row["goal_window_signed_price_change"]) for row in usable]
    control_moves = [float(row["control_window_signed_price_change"]) for row in usable]
    median_goal = f"{pd.Series(goal_moves).median():.4f}" if goal_moves else "not estimable (0 paired fixtures)"
    goal_rises = sum(value > 0 for value in goal_moves)
    control_rises = sum(value > 0 for value in control_moves)
    raw_counts = pd.Series([row["legacy_draw_history_total_count"] for row in rows])
    all_three = sum(
        all(int(row.get(f"{outcome}_price_observation_count") or 0) > 0
            for outcome in ("home_win", "draw", "away_win"))
        for row in rows
    )
    win_series_fixtures = sum(
        int(row.get("home_win_price_observation_count") or 0) > 0
        and int(row.get("away_win_price_observation_count") or 0) > 0
        for row in rows
    )
    three_way_observations = sum(
        int(row.get(f"{outcome}_price_observation_count") or 0)
        for row in rows
        for outcome in ("home_win", "draw", "away_win")
    )
    median_control = (
        float(pd.Series(control_moves).median()) if control_moves else None
    )
    goal_better_than_control = sum(
        float(row["goal_window_signed_price_change"])
        > float(row["control_window_signed_price_change"])
        for row in usable
    )
    if len(usable) < 5:
        decision = (
            "**Insufficient timestamp/price coverage to tell.** "
            f"Only {len(usable)} fixtures have fresh goal and control measurements."
        )
    elif (
        float(pd.Series(goal_moves).median()) > float(pd.Series(control_moves).median())
        and goal_better_than_control / len(usable) >= (2 / 3)
    ):
        decision = (
            "**Visible signal worth testing on more matches.** "
            "The median goal-window move exceeds the control median and at least "
            "two-thirds of paired goal moves are larger than their controls. "
            "This is a feasibility heuristic, not evidence of overreaction."
        )
    else:
        decision = (
            "**Heavy overlap despite usable coverage.** "
            "The paired goal/control moves do not meet the pilot's visible-signal "
            "heuristic; this is descriptive, not a significance or overreaction claim."
        )
    reasons = sorted({
        reason
        for row in rows
        for reason in str(row.get("exclusion_reasons") or "").split(";")
        if reason
    })
    timing_example = next(
        (row for row in rows if row.get("goal_utc_lower") and row.get("firstHalfStarted_raw")),
        None,
    )
    if timing_example:
        timing_note = (
            f"For {timing_example['home_team']}–{timing_example['away_team']} "
            f"(FotMob match {timing_example['fotmob_match_id']}), FotMob `utcTime` was "
            f"`{timing_example['fotmob_status_utc']}`, scheduled UTC kickoff was "
            f"`{timing_example['scheduled_kickoff_utc']}`, and raw `firstHalfStarted` was "
            f"`{timing_example['firstHalfStarted_raw']}`. A unique numeric UTC offset of "
            f"{timing_example['half_start_utc_offset_minutes_inferred']} minutes places the "
            f"start at `{timing_example['half_start_utc']}`; the inferred start is checked "
            "against both UTC kickoff fields. The response has no IANA timezone name, so "
            "the row remains flagged `timezone_name_unavailable`; an offset is inferred "
            "only when unique and both kickoff cross-checks put the start within 15 minutes."
        )
    else:
        timing_note = (
            "No unique half-start UTC offset could be verified against both fixture and "
            "FotMob UTC kickoff values; raw half-start strings are retained and goal UTC "
            "bounds are left blank."
        )
    lines = [
        "# EPL first-goal price-response feasibility pilot",
        "",
        "## Decision",
        "",
        decision + " No price moves were used to select fixtures or controls, no interpolation was performed, and no goal time was moved to a price jump.",
        "",
        "## Coverage",
        "",
        f"- Candidate fixtures tried: **{attempted}** (limit and deterministic selection rule below).",
        f"- Matched to FotMob by ordered teams plus fixture date or kickoff proximity: **{matched}/{attempted}**.",
        f"- Opening goal confirmed in the first half: **{first_half_goals}/{attempted}**.",
        f"- Usable goal/control price pairs: **{len(usable)}/{attempted}**.",
        f"- Median signed scoring-team win-price move: **{median_goal}**.",
        f"- Median signed control-window move: **{median_control:.4f}**." if median_control is not None else "- Median signed control-window move: **not estimable**.",
        f"- Scoring-team price rose: goal **{goal_rises}/{len(usable) if usable else 0}**, control **{control_rises}/{len(usable) if usable else 0}**.",
        f"- Existing draw-history observations across selected fixtures: {int(raw_counts.sum())} total; these were not substituted for win prices.",
        f"- Polymarket home/away win histories acquired for **{win_series_fixtures}/{attempted}** fixtures; all three outcomes have timestamps for **{all_three}/{attempted}** fixtures ({three_way_observations} total observations).",
        "",
        "All requested event-level raw values, candidate IDs, UTC-bound status, price contract IDs, observation timestamps, signed changes, and exclusions are in the CSV. Full one-minute outcome histories and their raw CLOB source references are in the 3-way history CSV. The paired plot shows each usable goal/control pair; price panels are emitted for fixtures with all three outcomes.",
        "",
        "## Source and timing checks",
        "",
        "A live FotMob match-details response was inspected before relying on its schema. It contained `content.matchFacts.events.events[]` with `type`, `time`, `timeStr`, `overloadTime`, `isHome`, and `shotmapEvent.period`; it also contained `header.status.halfs.firstHalfStarted`. "
        + timing_note
        + " Raw event fields remain in the CSV and the response envelope is cached locally. The scoring side is taken from the event score increment when available, with `isHome` retained independently.",
        "",
        "## Selection and measurement",
        "",
        "The script takes the first 15 fixtures after sorting the available EPL set by SHA-256 of `fixture_id`. Availability requires at least 10 timestamped historical observations between scheduled kickoff and kickoff + 45 minutes. Selection uses identity and timestamp coverage only, never price values or movement. The inventory includes 490 EPL fixtures; the existing snapshot output has 486 result-eligible fixtures, and 392 eligible `ingame_45m` rows.",
        "",
        f"Event/control freshness limit: **{fresh_minutes:g} minutes**. Event movement uses the nearest observed scoring-team win price strictly before the goal interval and the first observation strictly after it, each no more than the freshness limit away. Control duration equals the plotted goal window (uncertainty interval plus ±{WINDOW_MINUTES} minutes); controls are selected by nearest goal-free time and timestamp coverage in the same half, without price magnitude. No interpolation is used. The visible-signal classification requires at least five pairs, a higher median goal move than control, and a goal move larger than control for at least two-thirds of pairs; otherwise usable data are described as heavy overlap.",
        "",
        "## Market data and next step",
        "",
        f"- Gamma supplied separate binary Yes/No contracts for home win, draw, and away win. The script reuses the draw observations already in `historical_prices.csv`, fetches the missing home/away Yes-token histories through the existing cached CLOB client, and stores all timestamped observations in `{three_way_path.relative_to(ROOT)}`.",
        "- Each fetched team-win Yes token is cross-checked against the CLOB market's named outcome/token mapping before using its history. Market IDs, condition IDs, token IDs, mapping statuses, and raw response paths are recorded in the fixture CSV.",
        f"- Manual FotMob override file: `{manual_path.relative_to(ROOT)}`. Fill `half_start_utc` **or** `half_start_timezone` only when independently verified, and use the explicit goal columns if a detail response is unavailable. Raw FotMob responses are cached under `data/raw/fotmob_*`.",
        "",
        f"- Observed exclusion reasons: {', '.join(reasons) if reasons else 'none'}.",
        "",
        "## Reproduce",
        "",
        "From `Sports-Market-Capstone/`:",
        "",
        "```bash",
        "python3 scripts/soccer_goal_response_pilot.py --limit 15",
        "```",
        "",
        "Outputs are `data/processed/soccer_goal_response_pilot.csv`, `data/processed/soccer_three_way_price_history.csv`, `data/processed/soccer_goal_response_paired.png`, and `data/processed/soccer_goal_response_price_panels.png`.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--fixtures", type=Path, default=PROCESSED / "fixtures.csv")
    parser.add_argument("--historical-prices", type=Path, default=PROCESSED / "historical_prices.csv")
    parser.add_argument(
        "--three-way-prices",
        type=Path,
        default=PROCESSED / "soccer_three_way_price_history.csv",
    )
    parser.add_argument(
        "--manual-overrides",
        type=Path,
        default=PROCESSED / "soccer_goal_manual_overrides.csv",
    )
    parser.add_argument("--max-age-minutes", type=float, default=DEFAULT_FRESH_MINUTES)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.limit < 1 or args.max_age_minutes <= 0:
        parser.error("--limit and --max-age-minutes must be positive")

    selected, histories = select_fixtures(args.fixtures, args.historical_prices, args.limit)
    if not selected:
        raise SystemExit("No EPL fixtures met the timestamp-coverage selection rule.")
    overrides = manual_overrides(args.manual_overrides, selected)
    api = pipeline.API(
        refresh=args.refresh,
        delay=pipeline.RATE_DELAY_SECONDS,
    )
    team_prices: dict[str, pd.DataFrame] = {}

    by_season: dict[str, list[dict[str, Any]]] = {}
    season_refs: dict[str, str] = {}
    season_errors: dict[str, str] = {}
    for season in sorted({season_for(row["event_date"]) for row in selected}):
        try:
            by_season[season], season_refs[season] = get_season_matches(season, args.refresh)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            season_errors[season] = f"{type(error).__name__}:{error}"
    results = []
    details_by_fixture: dict[str, dict[str, Any]] = {}
    for fixture in selected:
        season = season_for(fixture["event_date"])
        matches = by_season.get(season)
        source_ref = season_refs.get(season)
        if matches is None:
            try:
                matches, source_ref = search_matches(fixture, args.refresh)
            except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                source_ref = None
                season_errors[season] = (
                    season_errors.get(season, "")
                    + f";search:{type(error).__name__}:{error}"
                )
        row, details = row_for_fixture(
            fixture,
            matches,
            source_ref,
            overrides.get(str(fixture["fixture_id"]), {}),
            histories,
            team_prices,
            args.max_age_minutes,
            args.refresh,
        )
        if details:
            details_by_fixture[str(fixture["fixture_id"])] = details
        if season in season_errors:
            row["fotmob_season_lookup_error"] = season_errors[season]
        results.append(row)
        time.sleep(0.05)

    price_rows: list[dict[str, Any]] = []
    for fixture, row in zip(selected, results):
        try:
            fixture_prices, audit = fetch_result_price_series(
                fixture,
                row,
                details_by_fixture.get(str(fixture["fixture_id"])),
                histories,
                api,
                args.refresh,
                args.max_age_minutes,
            )
        except (RuntimeError, ValueError, json.JSONDecodeError) as error:
            audit = {
                "polymarket_event_raw_ref": None,
                **{
                    f"{'draw' if outcome == 'draw' else f'{outcome}_win'}_market_status":
                    f"market_acquisition_failed:{type(error).__name__}:{error}"
                    for outcome in PRICE_OUTCOMES
                },
            }
            fixture_prices = []
        row.update(audit)
        price_rows.extend(fixture_prices)
        if row.get("goal_utc_lower"):
            for outcome in PRICE_OUTCOMES:
                prefix = "draw" if outcome == "draw" else f"{outcome}_win"
                if row.get(f"{prefix}_market_status") != "history_available":
                    reason = f"{prefix}_history_{row.get(f'{prefix}_market_status') or 'unavailable'}"
                    row["exclusion_reasons"] = ";".join(filter(None, [
                        row.get("exclusion_reasons"), reason,
                    ]))
    write_price_history(args.three_way_prices, price_rows)
    team_prices = series_by_fixture(args.three_way_prices)
    for row in results:
        fixture_id = str(row["fixture_id"])
        outcome = row.get("scoring_side")
        series = team_prices.get(fixture_id)
        if outcome in {"home", "away"} and series is not None and not series[
            series["outcome"].eq(outcome)
        ].empty:
            reasons = [
                reason for reason in str(row.get("exclusion_reasons") or "").split(";")
                if reason and reason != "scoring_team_win_price_history_unavailable"
            ]
            row["exclusion_reasons"] = ";".join(reasons)
    measure_controls(results, details_by_fixture, team_prices, args.max_age_minutes)
    output = PROCESSED / "soccer_goal_response_pilot.csv"
    pd.DataFrame(results).to_csv(output, index=False)
    save_paired_plot(results, PROCESSED / "soccer_goal_response_paired.png")
    save_price_panels(results, team_prices, PROCESSED / "soccer_goal_response_price_panels.png")
    write_report(
        results,
        ROOT / "reports" / "soccer_goal_response_pilot.md",
        args.max_age_minutes,
        args.three_way_prices,
        args.manual_overrides,
    )
    matched = sum(str(row["fotmob_match_status"]).startswith("matched_") for row in results)
    usable = sum(
        row.get("goal_window_signed_price_change") is not None
        and row.get("control_window_signed_price_change") is not None
        for row in results
    )
    print(f"tried={len(results)} fotmob_matched={matched} paired_usable={usable}")
    print(f"csv={output.relative_to(ROOT)}")
    print(f"paired_plot={(PROCESSED / 'soccer_goal_response_paired.png').relative_to(ROOT)}")
    print(f"report={(ROOT / 'reports' / 'soccer_goal_response_pilot.md').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
