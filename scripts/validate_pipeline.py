#!/usr/bin/env python3
"""Capture Polymarket football listings and produce a traceable validation pilot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
REPORT = ROOT / "reports" / "validation_report.md"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUES = {
    "La Liga": {"series": "10193", "espn": "esp.1"},
    "Ligue 1": {"series": "10195", "espn": "fra.1"},
    "Bundesliga": {"series": "10194", "espn": "ger.1"},
    "Serie A": {"series": "10203", "espn": "ita.1"},
    "Premier League": {"series": "10188", "espn": "eng.1"},
}
RETRY_STATUSES = {429, 500, 502, 503, 504}
RATE_DELAY_SECONDS = 0.2
PRICE_BINS = [i / 10 for i in range(11)]
DEFAULT_PREGAME_HORIZONS_MINUTES = [1440, 360, 60, 30, 5]
DEFAULT_INGAME_HORIZONS = ["15", "30", "halftime", "60", "75", "90"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def parse_iso(value: Any) -> datetime | None:
    """Parse only timezone-aware ISO timestamps; never guess a naive timezone."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def normalize_team_literal(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", " and ")
    text = re.sub(
        r"\b(fc|cf|afc|ssc|ac|sc|bc|ud|ca|as|us|rcd|club|de|the|calcio)\b",
        " ",
        text,
    )
    return re.sub(r"[^a-z0-9]+", "", text)


def normalize_team(value: str | None) -> str:
    normalized = normalize_team_literal(value)
    aliases = {
        "realracing": "racingsantander",
        "racingsantander": "racingsantander",
        "atleticodemadrid": "atleticomadrid",
        "atleticomadrid": "atleticomadrid",
        "rayovallecanomadrid": "rayovallecano",
        "rayovallecano": "rayovallecano",
        "olympiquemarseille": "marseille",
        "marseille": "marseille",
        "ogcnice": "nice",
        "nice": "nice",
        "bayer04leverkusen": "bayerleverkusen",
        "bayerleverkusen": "bayerleverkusen",
        "bayer04": "bayerleverkusen",
        "tsg1899hoffenheim": "hoffenheim",
        "tsghoffenheim": "hoffenheim",
        "hoffenheim": "hoffenheim",
        "bayernmunchen": "bayernmunich",
        "bayernmunich": "bayernmunich",
        "hellasverona": "hellasverona",
        "internazionale": "inter",
        "inter": "inter",
        "koeln": "cologne",
        "cologne": "cologne",
        "santander": "racingsantander",
    }
    return aliases.get(normalized, normalized)


def split_event_title(title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    value = re.sub(r"\s+-\s+more markets\s*$", "", title, flags=re.I).strip()
    value = re.sub(r"^(?:[^:]+:\s*)+", "", value).strip()
    match = re.search(r"^(.+?)\s+vs\.?\s+(.+?)\s*(?:\(|$)", value, re.I)
    if not match:
        return None, None
    home, away = (part.strip(" .-") for part in match.groups())
    return (home, away) if home and away else (None, None)


def parse_market_fields(market: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    outcomes = [str(x) for x in json_list(market.get("outcomes"))]
    tokens = [str(x) for x in json_list(market.get("clobTokenIds"))]
    prices = [str(x) for x in json_list(market.get("outcomePrices"))]
    return outcomes, tokens, prices


def draw_market_reasons(market: dict[str, Any]) -> list[str]:
    question = str(market.get("question") or "")
    label = str(market.get("groupItemTitle") or "")
    combined = f"{question} {label}".casefold()
    if re.search(r"\b(half|1st half|2nd half|first half|second half|extra time|penalt)", combined):
        return []
    if label.strip().casefold() == "draw":
        return ["groupItemTitle_exact_draw"]
    if re.search(r"\bend in (?:a )?draw\b", question, re.I):
        return ["question_full_match_draw_phrase"]
    if question.strip().casefold() == "draw":
        return ["question_exact_draw"]
    return []


def market_orientation(market: dict[str, Any]) -> dict[str, Any]:
    outcomes, tokens, prices = parse_market_fields(market)
    labels = [x.strip().casefold() for x in outcomes]
    yes_indexes = [i for i, label in enumerate(labels) if label == "yes"]
    no_indexes = [i for i, label in enumerate(labels) if label == "no"]
    reasons: list[str] = []
    if len(outcomes) != len(set(labels)):
        reasons.append("duplicate_outcome_labels")
    if len(outcomes) != len(tokens):
        reasons.append("outcome_token_length_mismatch")
    if len(yes_indexes) != 1 or len(no_indexes) != 1:
        reasons.append("expected_unique_yes_no_labels")
    if len(prices) != len(outcomes):
        reasons.append("outcome_price_length_mismatch")
    valid = not reasons
    return {
        "outcomes": outcomes,
        "tokens": tokens,
        "outcome_prices": prices,
        "yes_index": yes_indexes[0] if len(yes_indexes) == 1 else None,
        "yes_token_id": (
            tokens[yes_indexes[0]]
            if valid and tokens[yes_indexes[0]]
            else None
        ),
        "orientation_valid": valid,
        "orientation_issues": reasons,
    }


def compare_token_orientation(
    gamma_outcomes: list[str],
    gamma_tokens: list[str],
    clob_tokens: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(gamma_outcomes) != len(gamma_tokens) or len(clob_tokens) != len(gamma_outcomes):
        return {"status": "length_mismatch", "gamma_length": len(gamma_outcomes), "clob_length": len(clob_tokens)}
    gamma_mapping = {
        outcome.strip().casefold(): token
        for outcome, token in zip(gamma_outcomes, gamma_tokens)
    }
    clob_mapping = {
        str(token.get("outcome") or "").strip().casefold(): str(token.get("token_id") or "")
        for token in clob_tokens
    }
    if len(gamma_mapping) != len(gamma_outcomes) or len(clob_mapping) != len(clob_tokens):
        return {"status": "duplicate_or_missing_outcome_label"}
    return {
        "status": "matched" if gamma_mapping == clob_mapping else "token_mapping_mismatch",
        "gamma_mapping": gamma_mapping,
        "clob_mapping": clob_mapping,
    }


class API:
    def __init__(self, refresh: bool = False, delay: float = RATE_DELAY_SECONDS):
        self.refresh = refresh
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "sports-market-capstone-validation/1.0"}
        )

    def get(self, source: str, url: str, params: dict[str, Any]) -> tuple[Any, str]:
        clean_params = {key: value for key, value in params.items() if value is not None}
        cache_key = digest(json.dumps(
            {"url": url, "params": clean_params},
            sort_keys=True,
            separators=(",", ":"),
        ))
        source_dir = RAW / source
        source_dir.mkdir(parents=True, exist_ok=True)
        cache_file = source_dir / f"{cache_key}.json"
        if cache_file.exists() and not self.refresh:
            with cache_file.open(encoding="utf-8") as handle:
                cached = json.load(handle)
            return cached["payload"], str(cache_file.relative_to(ROOT))

        error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.get(url, params=clean_params, timeout=40)
                if response.status_code in RETRY_STATUSES and attempt < 3:
                    retry_after = response.headers.get("Retry-After")
                    wait = min(float(retry_after), 10.0) if retry_after else 0.5 * (2**attempt)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                payload = response.json()
                envelope = {
                    "source": source,
                    "url": response.url,
                    "request_parameters": clean_params,
                    "retrieved_at_utc": utc_now(),
                    "http_status": response.status_code,
                    "response_body_sha256": hashlib.sha256(response.content).hexdigest(),
                    "response_headers": {
                        key: value
                        for key, value in response.headers.items()
                        if key.casefold() in {
                            "content-type", "date", "etag", "last-modified",
                            "x-ratelimit-limit", "x-ratelimit-remaining",
                        }
                    },
                    "payload": payload,
                }
                if self.refresh and cache_file.exists():
                    cache_file = source_dir / f"{cache_key}-{int(time.time())}.json"
                with cache_file.open("w", encoding="utf-8") as handle:
                    json.dump(envelope, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                time.sleep(self.delay)
                return payload, str(cache_file.relative_to(ROOT))
            except (requests.RequestException, ValueError) as exc:
                error = exc
                if attempt < 3:
                    time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"Request failed after 4 attempts: {url} {clean_params}: {error}")


def fetch_listings(api: API, max_pages: int | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    listings: dict[tuple[str, str], dict[str, Any]] = {}
    source_refs: dict[str, str] = {}
    for league, config in LEAGUES.items():
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page = 0
        while True:
            params = {
                "series_id": config["series"],
                "limit": 100,
                "order": "id",
                "ascending": "true",
                "after_cursor": cursor,
            }
            payload, ref = api.get(
                "gamma_events",
                f"{GAMMA}/events/keyset",
                params,
            )
            source_refs[f"{league}:{page}"] = ref
            batch = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise ValueError(f"Unexpected keyset response for {league}: {ref}")
            for event in batch:
                event_id = str(event.get("id") or "")
                if not event_id:
                    continue
                listings[(league, event_id)] = {"league": league, "event": event, "raw_ref": ref}
            page += 1
            next_cursor = payload.get("next_cursor")
            if not batch or not next_cursor or (max_pages and page >= max_pages):
                break
            if next_cursor in seen_cursors:
                raise RuntimeError(f"Repeated pagination cursor for {league}; results incomplete")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    return list(listings.values()), source_refs


def event_kickoff(event: dict[str, Any], market: dict[str, Any]) -> tuple[datetime | None, str | None, str | None]:
    for field, source in (
        ("gameStartTime", "market.gameStartTime"),
        ("startTime", "event.startTime"),
    ):
        raw = market.get(field) if field == "gameStartTime" else event.get(field)
        parsed = parse_iso(raw)
        if parsed:
            return parsed, source, str(raw)
    return None, None, None


def select_latest_before(
    observations: list[dict[str, Any]],
    target: datetime,
    max_age_minutes: int,
) -> tuple[dict[str, Any] | None, float | None]:
    eligible = []
    for observation in observations:
        timestamp = parse_iso(observation.get("observation_timestamp_utc"))
        if timestamp and timestamp <= target:
            eligible.append((timestamp, observation))
    if not eligible:
        return None, None
    timestamp, observation = max(eligible, key=lambda pair: pair[0])
    age = (target - timestamp).total_seconds() / 60
    if age > max_age_minutes:
        return None, age
    return observation, age


def horizon_name(minutes: int, closing_minutes: int) -> str:
    if minutes == closing_minutes:
        return f"pregame_closing_{minutes}m"
    if minutes % 60 == 0:
        return f"pregame_{minutes // 60}h"
    return f"pregame_{minutes}m"


def parse_clock_seconds(play: dict[str, Any]) -> tuple[int, float] | None:
    period = (play.get("period") or {}).get("number")
    clock = play.get("clock") or {}
    value = clock.get("value")
    if period not in (1, 2) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    display = str(clock.get("displayValue") or "")
    added = re.search(r"\b(?:45|90)\s*'\s*\+\s*(\d+)", display)
    if added and seconds in (2700, 5400):
        seconds += int(added.group(1)) * 60
    return int(period), seconds


def match_clock_cutoffs(
    commentary: list[dict[str, Any]],
    horizons: list[str],
    max_clock_bracket_minutes: int,
    max_wallclock_bracket_minutes: int = 15,
) -> list[dict[str, Any]]:
    plays: list[dict[str, Any]] = []
    for item in commentary:
        play = item.get("play") or {}
        parsed_clock = parse_clock_seconds(play)
        wallclock = parse_iso(play.get("wallclock"))
        if parsed_clock and wallclock:
            period, seconds = parsed_clock
            plays.append({
                "period": period,
                "match_clock_seconds": seconds,
                "wallclock": wallclock,
                "text": str(item.get("text") or ""),
            })
    plays.sort(key=lambda item: (item["wallclock"], item["period"], item["match_clock_seconds"]))
    last_observed_play = max((item["wallclock"] for item in plays), default=None)
    results: list[dict[str, Any]] = []
    for horizon in horizons:
        base = {
            "forecast_horizon": f"ingame_{horizon}m" if horizon.isdigit() else "ingame_halftime",
            "horizon_type": "in_game_match_clock",
            "requested_match_minute": int(horizon) if horizon.isdigit() else "halftime",
            "match_clock_source": "ESPN commentary play clock and wallclock",
        }
        if horizon.casefold() == "halftime":
            first_half = [item for item in plays if item["period"] == 1]
            if not first_half:
                results.append({**base, "timing_status": "missing_first_half_commentary"})
                continue
            last_first_half = max(first_half, key=lambda item: (item["match_clock_seconds"], item["wallclock"]))
            results.append({
                **base,
                "requested_timestamp_utc": last_first_half["wallclock"].isoformat(),
                "timing_status": "halftime_proxy_last_first_half_play",
                "timing_precision": "approximate",
                "clock_bracket_seconds": None,
                "in_progress_check": (
                    "passed"
                    if last_observed_play and last_first_half["wallclock"] < last_observed_play
                    else "uncertain_no_later_play_timestamp"
                ),
            })
            continue

        minute = int(horizon)
        target_seconds = minute * 60
        period = 1 if minute <= 45 else 2
        candidates = [item for item in plays if item["period"] == period]
        before = [item for item in candidates if item["match_clock_seconds"] <= target_seconds]
        after = [item for item in candidates if item["match_clock_seconds"] >= target_seconds]
        if not before or not after:
            results.append({**base, "timing_status": "target_not_bracketed_by_commentary"})
            continue
        left = max(before, key=lambda item: (item["match_clock_seconds"], item["wallclock"]))
        right = min(after, key=lambda item: (item["match_clock_seconds"], item["wallclock"]))
        clock_gap = right["match_clock_seconds"] - left["match_clock_seconds"]
        wall_gap = (right["wallclock"] - left["wallclock"]).total_seconds()
        if clock_gap > max_clock_bracket_minutes * 60:
            results.append({
                **base,
                "timing_status": "clock_bracket_too_wide",
                "clock_bracket_seconds": clock_gap,
            })
            continue
        if wall_gap < 0 or wall_gap > max_wallclock_bracket_minutes * 60:
            results.append({
                **base,
                "timing_status": "wallclock_bracket_too_wide",
                "clock_bracket_seconds": clock_gap,
                "wallclock_bracket_seconds": wall_gap,
            })
            continue
        if clock_gap == 0:
            target_time = min(left["wallclock"], right["wallclock"])
            precision = "recorded_play_clock"
        else:
            fraction = (target_seconds - left["match_clock_seconds"]) / clock_gap
            target_time = left["wallclock"] + timedelta(seconds=wall_gap * fraction)
            precision = "interpolated_between_commentary_plays"
        in_progress = bool(last_observed_play and target_time < last_observed_play)
        results.append({
            **base,
            "requested_timestamp_utc": target_time.isoformat(),
            "timing_status": "match_clock_aligned" if in_progress else "target_not_verified_in_progress",
            "timing_precision": precision,
            "clock_bracket_seconds": clock_gap,
            "wallclock_bracket_seconds": wall_gap,
            "in_progress_check": "passed" if in_progress else "failed_or_uncertain",
            "left_play_timestamp_utc": left["wallclock"].isoformat(),
            "right_play_timestamp_utc": right["wallclock"].isoformat(),
            "left_play_text": left["text"],
            "right_play_text": right["text"],
        })
    return results


def rule_features(description: str) -> dict[str, str]:
    text = description.casefold()
    patterns = {
        "regulation_or_90_minutes": r"\b90 minutes?\b|\bregular play\b|\bregulation\b",
        "stoppage_time": r"\bstoppage time\b",
        "extra_time": r"\bextra time\b|\bextra-time\b",
        "penalties": r"\bpenalt(?:y|ies)\b",
        "postponement": r"\bpostpon(?:ed|ement)\b|\bdelayed\b",
        "cancellation": r"\bcancel(?:ed|led|lation)\b",
        "void": r"\bvoid\b|\binvalid\b",
    }
    return {
        f"rule_mentions_{name}": "yes" if re.search(pattern, text) else "not_stated_in_description"
        for name, pattern in patterns.items()
    }


def team_names(event: dict[str, Any]) -> tuple[str | None, str | None, str]:
    structured = event.get("teams")
    if isinstance(structured, list) and len(structured) >= 2:
        names: list[str] = []
        ordering: dict[str, str] = {}
        for item in structured[:2]:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("team") or "").strip()
                names.append(name)
                role = str(item.get("ordering") or "").strip().casefold()
                if role in {"home", "away"}:
                    ordering[role] = name
            else:
                names.append(str(item).strip())
        if set(ordering) == {"home", "away"}:
            return ordering["home"], ordering["away"], "event.teams.ordering"
    home, away = split_event_title(str(event.get("title") or ""))
    if home and away:
        if isinstance(structured, list):
            names = [
                str(item.get("name") or item.get("team") or "").strip()
                if isinstance(item, dict) else str(item).strip()
                for item in structured[:2]
            ]
            if len(names) >= 2 and {
                normalize_team(names[0]), normalize_team(names[1])
            } == {normalize_team(home), normalize_team(away)}:
                return home, away, "event.title_order_structured_teams_corroborated"
        return home, away, "event.title_order_unverified"
    return None, None, "unordered_or_unparsed"


def fixture_keys(listings: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    details: dict[tuple[str, str], dict[str, Any]] = {}
    exact_groups: dict[tuple[str, str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for listing in listings:
        league, event = listing["league"], listing["event"]
        event_id = str(event.get("id"))
        markets = event.get("markets") or []
        draw_markets = [market for market in markets if draw_market_reasons(market)]
        if not draw_markets:
            continue
        home, away, team_source = team_names(event)
        selected_market = draw_markets[0]
        kickoff, kickoff_source, kickoff_raw = event_kickoff(event, selected_market)
        event_date = str(event.get("eventDate") or "")
        date_key = kickoff.date().isoformat() if kickoff else event_date[:10]
        key = (league, event_id)
        details[key] = {
            "league": league,
            "event_id": event_id,
            "event_title": event.get("title"),
            "event_slug": event.get("slug"),
            "event_date": event_date or None,
            "home_team": home,
            "away_team": away,
            "team_source": team_source,
            "kickoff_utc": kickoff.isoformat() if kickoff else None,
            "kickoff_source": kickoff_source,
            "kickoff_raw": kickoff_raw,
            "fixture_date_basis": "timezone_aware_utc_kickoff" if kickoff else ("eventDate_only" if date_key else "missing"),
            "draw_market_count": len(draw_markets),
            "raw_ref": listing["raw_ref"],
        }
        if home and away and date_key:
            exact_groups[(league, normalize_team(home), normalize_team(away), date_key)].append(key)

    fixture_assignment: dict[tuple[str, str], tuple[str, str]] = {}
    for group, members in exact_groups.items():
        fixture_id = "fx-" + digest("|".join(group))
        status = "exact_match_key"
        if len(members) > 1:
            for member in members:
                fixture_assignment[member] = (fixture_id, "duplicate_listing_exact_key")
        else:
            fixture_assignment[members[0]] = (fixture_id, status)
    for key in details:
        if key not in fixture_assignment:
            fixture_assignment[key] = ("fx-listing-" + digest("|".join(key)), "uncertain_missing_identity_or_date")
        details[key]["fixture_id"], details[key]["fixture_match_status"] = fixture_assignment[key]

    # Nearby dates with the same ordered teams are possible reschedules, not automatic merges.
    by_teams: dict[tuple[str, str, str], list[tuple[tuple[str, str], datetime]]] = defaultdict(list)
    for key, row in details.items():
        if row["home_team"] and row["away_team"] and row["kickoff_utc"]:
            dt = parse_iso(row["kickoff_utc"])
            if dt:
                by_teams[(row["league"], normalize_team(row["home_team"]), normalize_team(row["away_team"]))].append((key, dt))
    for entries in by_teams.values():
        for i, (left_key, left_dt) in enumerate(entries):
            for right_key, right_dt in entries[i + 1:]:
                if 0 < abs((left_dt - right_dt).days) <= 21:
                    details[left_key]["nearby_same_teams_candidate"] = True
                    details[right_key]["nearby_same_teams_candidate"] = True
                    if details[left_key]["fixture_match_status"] == "exact_match_key":
                        details[left_key]["fixture_match_status"] = "possible_reschedule_or_distinct_fixture_review"
                    if details[right_key]["fixture_match_status"] == "exact_match_key":
                        details[right_key]["fixture_match_status"] = "possible_reschedule_or_distinct_fixture_review"
    rows = list(details.values())
    return details, rows


def settlement(market: dict[str, Any], orient: dict[str, Any]) -> tuple[int | None, str]:
    if not orient["orientation_valid"]:
        return None, "invalid_orientation"
    if not market.get("closed"):
        return None, "market_not_closed"
    status = str(market.get("umaResolutionStatus") or "").casefold()
    if status != "resolved":
        return None, f"resolution_status_{status or 'missing'}"
    prices = orient["outcome_prices"]
    try:
        values = [float(value) for value in prices]
    except (TypeError, ValueError):
        return None, "nonnumeric_outcome_prices"
    if not all(value in (0.0, 1.0) for value in values) or sum(values) != 1.0:
        return None, "nonbinary_or_noncomplementary_settlement"
    return int(values[orient["yes_index"]]), "resolved_binary_api_prices"


def audit_rows(listings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    detail_map, fixtures = fixture_keys(listings)
    rows: list[dict[str, Any]] = []
    for listing in listings:
        event = listing["event"]
        league = listing["league"]
        fixture = detail_map.get((league, str(event.get("id"))))
        if fixture is None:
            continue
        for market in event.get("markets") or []:
            reasons = draw_market_reasons(market)
            if not reasons:
                continue
            orient = market_orientation(market)
            kickoff, kickoff_source, kickoff_raw = event_kickoff(event, market)
            settled, settled_status = settlement(market, orient)
            description = str(market.get("description") or "")
            rows.append({
                **fixture,
                "outcome_type": "draw",
                "market_id": str(market.get("id") or ""),
                "market_slug": market.get("slug"),
                "condition_id": market.get("conditionId"),
                "question": market.get("question"),
                "group_item_title": market.get("groupItemTitle"),
                "draw_classification": reasons,
                "market_description": description,
                "market_resolution_source": market.get("resolutionSource"),
                "market_rules_text": description,
                **rule_features(description),
                "market_closed": bool(market.get("closed")),
                "uma_resolution_status": market.get("umaResolutionStatus"),
                "closed_time": market.get("closedTime"),
                "market_updated_at": market.get("updatedAt"),
                "automatically_resolved": market.get("automaticallyResolved"),
                "resolved_by": market.get("resolvedBy"),
                "uma_resolution_statuses": market.get("umaResolutionStatuses"),
                "resolution_mechanism": market.get("resolutionMechanism"),
                "outcomes": orient["outcomes"],
                "token_ids": orient["tokens"],
                "outcome_prices": orient["outcome_prices"],
                "yes_index": orient["yes_index"],
                "yes_token_id": orient["yes_token_id"],
                "orientation_valid": orient["orientation_valid"],
                "orientation_issues": orient["orientation_issues"],
                "api_settled_draw": settled,
                "settlement_status": settled_status,
                "listing_validation_issue": "; ".join(
                    reason for reason, present in (
                        ("ambiguous_draw_market", fixture["draw_market_count"] != 1),
                        ("invalid_token_outcome_orientation", not orient["orientation_valid"]),
                        ("missing_timezone_aware_kickoff", kickoff is None),
                        ("unverified_or_nonbinary_settlement", settled is None),
                    ) if present
                ) or None,
                "market_kickoff_utc": kickoff.isoformat() if kickoff else None,
                "market_kickoff_source": kickoff_source,
                "market_kickoff_raw": kickoff_raw,
                "source_raw_ref": listing["raw_ref"],
                "eligibility_status": "pending_independent_verification",
                "exclusion_reason": None,
            })
    return rows, fixtures


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - radius), min(1.0, center + radius)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                for key, value in row.items()
            })


def fetch_espn_result(api: API, row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    kickoff = parse_iso(row.get("kickoff_utc") or row.get("market_kickoff_utc"))
    if not kickoff:
        return None, "missing_timezone_aware_kickoff"
    league_code = LEAGUES[row["league"]]["espn"]
    date = kickoff.strftime("%Y%m%d")
    payload, ref = api.get(
        "espn_scoreboard",
        f"{ESPN}/{league_code}/scoreboard",
        {"dates": date},
    )
    events = payload.get("events", []) if isinstance(payload, dict) else []
    target_home, target_away = normalize_team(row.get("home_team")), normalize_team(row.get("away_team"))
    matches = []
    for event in events:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        home = next((item for item in competitors if item.get("homeAway") == "home"), None)
        away = next((item for item in competitors if item.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        raw_home = (home.get("team") or {}).get("displayName")
        raw_away = (away.get("team") or {}).get("displayName")
        hn = normalize_team(raw_home)
        an = normalize_team(raw_away)
        same = (hn == target_home and an == target_away)
        reverse = (hn == target_away and an == target_home)
        if same or reverse:
            matches.append((event, home, away, reverse))
    if len(matches) != 1:
        return {
            "source": "ESPN score feed (secondary, not competition-authoritative)",
            "candidate_count": len(matches),
            "score_match_status": "no_match" if not matches else "ambiguous_match",
            "raw_ref": ref,
        }, None
    event, home, away, reverse = matches[0]
    score_home, score_away = home.get("score"), away.get("score")
    try:
        score_home, score_away = int(float(score_home)), int(float(score_away))
    except (TypeError, ValueError):
        score_home = score_away = None
    return {
        "source": "ESPN score feed (secondary, not competition-authoritative)",
        "event_id": event.get("id"),
        "event_date": event.get("date"),
        "status": (event.get("status") or {}).get("type", {}).get("name"),
        "completed": (event.get("status") or {}).get("type", {}).get("completed"),
        "home_team": (home.get("team") or {}).get("displayName"),
        "away_team": (away.get("team") or {}).get("displayName"),
        "team_match_method": (
            "exact_after_conservative_normalization"
            if normalize_team(raw_home) == target_home and normalize_team(raw_away) == target_away
            and normalize_team_literal(str(raw_home)) == normalize_team_literal(str(row.get("home_team")))
            and normalize_team_literal(str(raw_away)) == normalize_team_literal(str(row.get("away_team")))
            else "explicit_alias_normalization"
        ),
        "score_home": score_away if reverse else score_home,
        "score_away": score_home if reverse else score_away,
        "score_match_status": "matched" if score_home is not None and score_away is not None else "score_missing",
        "raw_ref": ref,
    }, None


def fetch_espn_summary(api: API, league: str, event_id: str) -> tuple[dict[str, Any] | None, str | None]:
    league_code = LEAGUES[league]["espn"]
    payload, ref = api.get(
        "espn_match_summary",
        f"{ESPN}/{league_code}/summary",
        {"event": event_id},
    )
    if not isinstance(payload, dict):
        return None, ref
    header = payload.get("header") or {}
    summary_event_id = str(header.get("id") or "")
    if summary_event_id != event_id:
        return {
            "_raw_ref": ref,
            "_summary_status": "event_id_mismatch" if summary_event_id else "summary_event_id_missing",
            "commentary": [],
        }, ref
    return {**payload, "_raw_ref": ref, "_summary_status": "matched"}, ref


def fetch_pl_official(api: API, row: dict[str, Any]) -> dict[str, Any]:
    kickoff = parse_iso(row.get("kickoff_utc") or row.get("market_kickoff_utc"))
    if not kickoff:
        return {"source": "Premier League Football API", "status": "missing_kickoff"}
    seasons, season_ref = api.get(
        "premierleague_seasons",
        "https://footballapi.pulselive.com/football/competitions/1/compseasons",
        {"page": 0, "pageSize": 100},
    )
    start_year = kickoff.year if kickoff.month >= 7 else kickoff.year - 1
    def matches_season_label(label: str) -> bool:
        match = re.search(r"\b(20\d{2})\s*/\s*(\d{2}|20\d{2})\b", label)
        if not match or int(match.group(1)) != start_year:
            return False
        next_year = match.group(2)
        return int(next_year) == (start_year + 1) % 100 or int(next_year) == start_year + 1

    season_entry = next(
        (
            item for item in seasons.get("content", [])
            if matches_season_label(str(item.get("label") or ""))
        ),
        None,
    )
    season_label = season_entry.get("label") if season_entry else f"{start_year}/{start_year + 1}"
    if not season_entry:
        return {"source": "Premier League Football API", "status": "season_unavailable", "season_ref": season_ref}
    params = {
        "comp": 1,
        "compSeasons": int(season_entry["id"]),
        "statuses": "C",
        "page": 0,
        "pageSize": 500,
    }
    fixtures, fixture_ref = api.get(
        "premierleague_fixtures",
        "https://footballapi.pulselive.com/football/fixtures",
        params,
    )
    target_home, target_away = normalize_team(row.get("home_team")), normalize_team(row.get("away_team"))
    candidates = []
    for fixture in fixtures.get("content", []):
        teams = fixture.get("teams") or []
        if len(teams) < 2:
            continue
        # The public feed returns the home team first and away team second;
        # the historical response objects do not include a teamType field.
        home, away = teams[0], teams[1]
        home_team = home.get("team") or {}
        away_team = away.get("team") or {}
        hn, an = normalize_team(home_team.get("name")), normalize_team(away_team.get("name"))
        if hn == target_home and an == target_away:
            candidates.append((fixture, home, away))
    if len(candidates) != 1:
        return {
            "source": "Premier League official Football API",
            "status": "no_match" if not candidates else "ambiguous_match",
            "candidate_count": len(candidates),
            "raw_ref": fixture_ref,
            "season_ref": season_ref,
            "season_label": season_label,
        }
    fixture, home, away = candidates[0]
    kickoff_data = fixture.get("kickoff") or {}
    millis = kickoff_data.get("millis")
    actual = datetime.fromtimestamp(float(millis) / 1000, tz=timezone.utc) if millis else None
    home_score = (home.get("score") if isinstance(home.get("score"), (int, float)) else None)
    away_score = (away.get("score") if isinstance(away.get("score"), (int, float)) else None)
    return {
        "source": "Premier League official Football API",
        "status": fixture.get("status"),
        "fixture_id": fixture.get("id"),
        "kickoff_utc": actual.isoformat() if actual else None,
        "kickoff_timezone": "UTC (millis since Unix epoch)",
        "kickoff_local_label": kickoff_data.get("label"),
        "provisional_kickoff": fixture.get("provisionalKickoff"),
        "home_team": (home.get("team") or {}).get("name"),
        "away_team": (away.get("team") or {}).get("name"),
        "score_home": int(home_score) if home_score is not None else None,
        "score_away": int(away_score) if away_score is not None else None,
        "team_order_evidence": "feed array order; cross-checked against market and ESPN teams in the pilot",
        "source_ref": fixture_ref,
        "season_ref": season_ref,
    }


def run_price_api_experiments(api: API) -> dict[str, Any]:
    """Reproduce bounded/unbounded and overlap checks for the saved notebook match."""
    market, market_ref = api.get(
        "gamma_market",
        f"{GAMMA}/markets/521526",
        {},
    )
    event, event_ref = api.get(
        "gamma_event_id_collision_check",
        f"{GAMMA}/events/521526",
        {},
    )
    orientation = market_orientation(market)
    if not orientation["yes_token_id"]:
        result = {
            "status": "blocked_invalid_market_orientation",
            "market_ref": market_ref,
            "event_ref": event_ref,
        }
        return result
    clob_market, clob_market_ref = api.get(
        "clob_market_details",
        f"{CLOB}/markets/{market.get('conditionId')}",
        {},
    )
    token_crosscheck = compare_token_orientation(
        orientation["outcomes"],
        orientation["tokens"],
        clob_market.get("tokens", []),
    )
    token = orientation["yes_token_id"]
    unbounded, unbounded_ref = api.get(
        "clob_prices_history_experiment",
        f"{CLOB}/prices-history",
        {"market": token, "interval": "max", "fidelity": 60},
    )
    begin = datetime(2025, 2, 8, 18, tzinfo=timezone.utc)
    split = datetime(2025, 2, 8, 20, tzinfo=timezone.utc)
    overlap_start = datetime(2025, 2, 8, 19, tzinfo=timezone.utc)
    end = datetime(2025, 2, 8, 21, tzinfo=timezone.utc)

    def bounded(start: datetime, finish: datetime, fidelity: int) -> tuple[list[dict[str, Any]], str]:
        payload, ref = api.get(
            "clob_prices_history_experiment",
            f"{CLOB}/prices-history",
            {
                "market": token,
                "startTs": int(start.timestamp()),
                "endTs": int(finish.timestamp()),
                "fidelity": fidelity,
            },
        )
        return payload.get("history", []) if isinstance(payload, dict) else [], ref

    hourly, hourly_ref = bounded(begin, end, 60)
    fine_left, fine_left_ref = bounded(begin, split, 1)
    fine_right, fine_right_ref = bounded(overlap_start, end, 1)
    coarse_left, coarse_left_ref = bounded(begin, split, 60)
    coarse_right, coarse_right_ref = bounded(overlap_start, end, 60)

    def valid_series(series: list[dict[str, Any]]) -> list[tuple[int, float]]:
        result: list[tuple[int, float]] = []
        for item in series:
            try:
                result.append((int(item["t"]), float(item["p"])))
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def overlap(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
        left_values, right_values = dict(valid_series(left)), dict(valid_series(right))
        shared = set(left_values) & set(right_values)
        return {
            "shared_timestamps": len(shared),
            "exact_price_matches": sum(left_values[t] == right_values[t] for t in shared),
            "timestamp_units": "Unix seconds (t field; also confirmed by saved notebook conversion)",
        }

    bounded_valid = valid_series(hourly)
    unbounded_history = unbounded.get("history", []) if isinstance(unbounded, dict) else []
    return {
        "status": "completed",
        "market_id": market.get("id"),
        "market_question": market.get("question"),
        "market_description": market.get("description"),
        "market_ref": market_ref,
        "market_orientation": {
            "outcomes": orientation["outcomes"],
            "yes_index": orientation["yes_index"],
            "selected_yes_token": token,
        },
        "clob_market_token_crosscheck": {
            **token_crosscheck,
            "raw_ref": clob_market_ref,
        },
        "same_numeric_id_event_lookup": {
            "event_id": event.get("id") if isinstance(event, dict) else None,
            "event_title": event.get("title") if isinstance(event, dict) else None,
            "market_id": market.get("id"),
            "market_question": market.get("question"),
            "interpretation": "Event and market numeric IDs are separate namespaces; do not infer event identity from a market ID.",
            "event_ref": event_ref,
        },
        "unbounded_max_interval_fidelity_60": {
            "observation_count": len(unbounded_history),
            "raw_ref": unbounded_ref,
        },
        "bounded_2025_02_08_18_to_21_utc_fidelity_60": {
            "observation_count": len(bounded_valid),
            "minimum_timestamp_utc": datetime.fromtimestamp(min((t for t, _ in bounded_valid), default=0), tz=timezone.utc).isoformat() if bounded_valid else None,
            "maximum_timestamp_utc": datetime.fromtimestamp(max((t for t, _ in bounded_valid), default=0), tz=timezone.utc).isoformat() if bounded_valid else None,
            "raw_ref": hourly_ref,
        },
        "overlap_consistency": {
            "one_minute_fidelity": overlap(fine_left, fine_right),
            "sixty_minute_fidelity": overlap(coarse_left, coarse_right),
            "window_1_utc": f"{begin.isoformat()} to {split.isoformat()}",
            "window_2_utc": f"{overlap_start.isoformat()} to {end.isoformat()}",
            "one_minute_left_ref": fine_left_ref,
            "one_minute_right_ref": fine_right_ref,
            "sixty_minute_left_ref": coarse_left_ref,
            "sixty_minute_right_ref": coarse_right_ref,
        },
        "semantics_limit": "OpenAPI calls t an integer timestamp and p a numeric price. No documentation located in this audit establishes that p is a trade, midpoint, or original event-time price, or that each t is an original observation rather than a fidelity sample.",
        "pagination_limit": "The endpoint schema documents startTs, endTs, interval, and fidelity; no cursor/page parameter is documented.",
    }


def select_pilot(rows: list[dict[str, Any]], per_league: int, seed: str) -> list[dict[str, Any]]:
    candidates = [
        row for row in rows
        if row["draw_market_count"] == 1 and row["orientation_valid"]
        and row["market_kickoff_utc"] and row["api_settled_draw"] is not None
        and row.get("fixture_match_status") in {
            "exact_match_key",
            "duplicate_listing_exact_key",
        }
    ]
    selected: list[dict[str, Any]] = []
    for league in LEAGUES:
        group = [row for row in candidates if row["league"] == league]
        group.sort(key=lambda row: digest(seed + row["event_id"] + row["market_id"]))
        seen_fixtures: set[tuple[str, str]] = set()
        for row in group:
            fixture_outcome = (row["fixture_id"], "draw")
            if fixture_outcome in seen_fixtures:
                continue
            selected.append(row)
            seen_fixtures.add(fixture_outcome)
            if sum(item["league"] == league for item in selected) >= per_league:
                break
    return selected


def audit_token_mapping(api: API, row: dict[str, Any]) -> dict[str, Any]:
    condition_id = row.get("condition_id")
    if not condition_id:
        return {
            "event_id": row["event_id"],
            "market_id": row["market_id"],
            "token_mapping_status": "missing_condition_id",
            "settlement_crosscheck_status": "not_checked",
        }
    payload, ref = api.get(
        "clob_market_details",
        f"{CLOB}/markets/{condition_id}",
        {},
    )
    crosscheck = compare_token_orientation(
        row["outcomes"],
        row["token_ids"],
        payload.get("tokens", []) if isinstance(payload, dict) else [],
    )
    clob_tokens = payload.get("tokens", []) if isinstance(payload, dict) else []
    settlement_status = "not_binary"
    if crosscheck["status"] == "matched" and row.get("api_settled_draw") is not None:
        clob_by_label = {
            str(item.get("outcome") or "").strip().casefold(): item
            for item in clob_tokens
        }
        try:
            clob_prices = {
                label: float(item["price"])
                for label, item in clob_by_label.items()
            }
            gamma_prices = {
                label.strip().casefold(): float(price)
                for label, price in zip(row["outcomes"], row["outcome_prices"])
            }
        except (KeyError, TypeError, ValueError):
            settlement_status = "missing_clob_settlement_values"
        else:
            winner_labels = {
                label for label, item in clob_by_label.items()
                if item.get("winner") is True
            }
            gamma_winner_labels = {
                label for label, price in gamma_prices.items() if price == 1.0
            }
            settlement_status = (
                "matched"
                if clob_prices == gamma_prices and winner_labels == gamma_winner_labels
                else "settlement_label_mismatch"
            )
    return {
        "event_id": row["event_id"],
        "fixture_id": row["fixture_id"],
        "market_id": row["market_id"],
        "condition_id": condition_id,
        "outcomes": row["outcomes"],
        "gamma_token_ids": row["token_ids"],
        "yes_index": row["yes_index"],
        "yes_token_id": row["yes_token_id"],
        "token_mapping_status": crosscheck["status"],
        "settlement_crosscheck_status": settlement_status,
        "gamma_closed": row["market_closed"],
        "gamma_uma_resolution_status": row["uma_resolution_status"],
        "gamma_outcome_prices": row["outcome_prices"],
        "clob_market_closed": payload.get("closed") if isinstance(payload, dict) else None,
        "clob_tokens": clob_tokens,
        "raw_ref": ref,
    }


def fixture_candidate_pairs(fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_teams: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for fixture in fixtures:
        if fixture.get("home_team") and fixture.get("away_team") and fixture.get("kickoff_utc"):
            by_teams[(
                fixture["league"], normalize_team(fixture["home_team"]),
                normalize_team(fixture["away_team"]),
            )].append(fixture)
    candidates = []
    for members in by_teams.values():
        for index, left in enumerate(members):
            left_time = parse_iso(left["kickoff_utc"])
            for right in members[index + 1:]:
                right_time = parse_iso(right["kickoff_utc"])
                if left_time and right_time and 0 < abs((left_time - right_time).days) <= 21:
                    candidates.append({
                        "league": left["league"],
                        "home_team_left": left["home_team"],
                        "away_team_left": left["away_team"],
                        "event_id_left": left["event_id"],
                        "fixture_id_left": left["fixture_id"],
                        "kickoff_left_utc": left["kickoff_utc"],
                        "home_team_right": right["home_team"],
                        "away_team_right": right["away_team"],
                        "event_id_right": right["event_id"],
                        "fixture_id_right": right["fixture_id"],
                        "kickoff_right_utc": right["kickoff_utc"],
                        "days_apart": abs((left_time - right_time).days),
                        "adjudication": "possible_reschedule_or_distinct_fixture; not merged",
                    })
    return candidates


def audit_fixture_candidates(
    api: API,
    candidates: list[dict[str, Any]],
    listing_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_event = {
        (row["league"], row["event_id"]): row
        for row in listing_rows
    }
    results = []
    for candidate in candidates:
        left = rows_by_event.get((candidate["league"], candidate["event_id_left"]))
        right = rows_by_event.get((candidate["league"], candidate["event_id_right"]))
        left_result, left_error = fetch_espn_result(api, left) if left else (None, "left listing missing")
        right_result, right_error = fetch_espn_result(api, right) if right else (None, "right listing missing")
        left_espn_id = (left_result or {}).get("event_id")
        right_espn_id = (right_result or {}).get("event_id")
        results.append({
            **candidate,
            "left_title": left.get("event_title") if left else None,
            "right_title": right.get("event_title") if right else None,
            "left_market_id": left.get("market_id") if left else None,
            "right_market_id": right.get("market_id") if right else None,
            "left_market_question": left.get("question") if left else None,
            "right_market_question": right.get("question") if right else None,
            "left_api_settled_draw": left.get("api_settled_draw") if left else None,
            "right_api_settled_draw": right.get("api_settled_draw") if right else None,
            "left_market_raw_ref": left.get("source_raw_ref") if left else None,
            "right_market_raw_ref": right.get("source_raw_ref") if right else None,
            "left_espn_status": (left_result or {}).get("score_match_status"),
            "right_espn_status": (right_result or {}).get("score_match_status"),
            "left_espn_event_id": left_espn_id,
            "right_espn_event_id": right_espn_id,
            "left_espn_score": f"{(left_result or {}).get('score_home')}-{(left_result or {}).get('score_away')}" if left_result and left_result.get("score_home") is not None else None,
            "right_espn_score": f"{(right_result or {}).get('score_home')}-{(right_result or {}).get('score_away')}" if right_result and right_result.get("score_home") is not None else None,
            "same_espn_event": bool(left_espn_id and left_espn_id == right_espn_id),
            "left_source_ref": (left_result or {}).get("raw_ref"),
            "right_source_ref": (right_result or {}).get("raw_ref"),
            "source_errors": "; ".join(error for error in (left_error, right_error) if error),
        })
    return results


def collect_price_window(
    api: API,
    row: dict[str, Any],
    first_target: datetime,
    last_target: datetime,
    max_lookback_minutes: int,
) -> tuple[list[dict[str, Any]], str]:
    start = first_target - timedelta(minutes=max_lookback_minutes)
    payload, ref = api.get(
        "clob_prices_history",
        f"{CLOB}/prices-history",
        {
            "market": row["yes_token_id"],
            "startTs": int(start.timestamp()),
            "endTs": int(last_target.timestamp()),
            "fidelity": 1,
        },
    )
    history = payload.get("history", []) if isinstance(payload, dict) else []
    observations: list[dict[str, Any]] = []
    for item in history:
        try:
            timestamp = datetime.fromtimestamp(float(item["t"]), tz=timezone.utc)
            probability = float(item["p"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        observations.append({
            "event_id": row["event_id"],
            "fixture_id": row["fixture_id"],
            "league": row["league"],
            "market_id": row["market_id"],
            "condition_id": row["condition_id"],
            "token_id": row["yes_token_id"],
            "draw_token_id": row["yes_token_id"],
            "observation_timestamp_utc": timestamp.isoformat(),
            "unix_timestamp_seconds": int(float(item["t"])),
            "probability": probability,
            "historical_draw_price": probability,
            "source_ref": ref,
        })
    observations.sort(key=lambda item: item["unix_timestamp_seconds"])
    return observations, ref


def collect_price(
    api: API,
    row: dict[str, Any],
    max_lookback_minutes: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kickoff = parse_iso(row.get("market_kickoff_utc"))
    if not kickoff:
        return [], {"status": "missing_kickoff"}
    target = kickoff - timedelta(hours=1)
    observations, ref = collect_price_window(api, row, target, target, max_lookback_minutes)
    selected = build_forecast_snapshot(
        {
            "fixture_id": row["fixture_id"],
            "market_id": row["market_id"],
            "token_mapping_status": "matched",
            "settlement_crosscheck_status": "matched",
            "fixture_result_eligible": True,
        },
        {
            "forecast_horizon": "pregame_1h",
            "horizon_type": "pregame",
            "requested_timestamp_utc": target.isoformat(),
        },
        observations,
        max_lookback_minutes,
    )
    selected.update({
        "status": selected["price_observation_status"],
        "raw_ref": ref,
        "forecast_source": (
            "Polymarket CLOB /prices-history historical price (underlying measure unspecified)"
            if selected["forecast_probability"] is not None else None
        ),
    })
    return observations, selected


def build_forecast_snapshot(
    fixture: dict[str, Any],
    request: dict[str, Any],
    observations: list[dict[str, Any]],
    max_age_minutes: int,
) -> dict[str, Any]:
    requested_raw = request.get("requested_timestamp_utc")
    target = parse_iso(requested_raw)
    reason = request.get("timing_status")
    chosen: dict[str, Any] | None = None
    age: float | None = None
    if target is None:
        reason = reason or "missing_requested_timestamp"
        status = "missing_timing"
    else:
        chosen, age = select_latest_before(observations, target, max_age_minutes)
        if chosen is None:
            any_before, _ = select_latest_before(observations, target, 10**9)
            status = "no_price_before_cutoff" if any_before is None else "price_exceeds_maximum_age"
            reason = status
        elif not 0 <= chosen["probability"] <= 1:
            status = "invalid_probability"
            reason = status
            chosen = None
        elif request.get("in_progress_check") not in (None, "passed"):
            status = "timing_not_verified_in_progress"
            reason = status
            chosen = None
        elif request.get("timing_status") not in (
            None,
            "scheduled_kickoff_relative",
            "match_clock_aligned",
            "halftime_proxy_last_first_half_play",
        ):
            status = "timing_uncertain"
            reason = request.get("timing_status")
            chosen = None
        else:
            status = "selected"
            reason = None
    selected_time = parse_iso(chosen.get("observation_timestamp_utc")) if chosen else None
    timing_is_eligible = request.get("timing_precision") != "approximate"
    analysis_eligible = bool(fixture.get("fixture_result_eligible") and chosen and timing_is_eligible)
    if fixture.get("fixture_result_eligible") and chosen and not timing_is_eligible:
        reason = "halftime_wallclock_is_proxy_only"
    return {
        **fixture,
        **request,
        "requested_timestamp_utc": target.isoformat() if target else None,
        "selected_observation_timestamp_utc": selected_time.isoformat() if selected_time else None,
        "forecast_probability": chosen["probability"] if chosen else None,
        "observation_age_minutes": age,
        "max_lookback_minutes": max_age_minutes,
        "price_observation_status": status,
        "forecast_source": (
            "Polymarket CLOB /prices-history historical draw-token price; underlying measure unspecified"
            if chosen else None
        ),
        "price_history_raw_ref": chosen.get("source_ref") if chosen else fixture.get("price_history_raw_ref"),
        "observation_count_in_window": sum(
            parse_iso(item["observation_timestamp_utc"]) is not None
            and target is not None
            and parse_iso(item["observation_timestamp_utc"]) <= target
            for item in observations
        ),
        "leakage_check": (
            bool(selected_time and target and selected_time <= target)
            if chosen else None
        ),
        "analysis_eligible": analysis_eligible,
        "eligibility_status": (
            "eligible_exploratory"
            if analysis_eligible
            else "excluded"
        ),
        "eligibility_reason": (
            None
            if analysis_eligible
            else fixture.get("fixture_result_exclusion_reason") or reason
        ),
    }


def calibration_rows(
    forecasts: list[dict[str, Any]],
    bin_edges: list[float] = PRICE_BINS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = [
        row for row in forecasts
        if row.get("analysis_eligible") and row.get("forecast_probability") is not None
        and row.get("verified_draw") in (0, 1)
    ]
    tables: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        groups[("pooled", row["forecast_horizon"])].append(row)
        groups[(row["league"], row["forecast_horizon"])].append(row)
    for (league, horizon), group in groups.items():
        brier = sum((r["forecast_probability"] - r["verified_draw"]) ** 2 for r in group) / len(group)
        eps = 1e-6
        logloss = -sum(
            y * math.log(min(1 - eps, max(eps, p)))
            + (1 - y) * math.log(min(1 - eps, max(eps, 1 - p)))
            for p, y in ((r["forecast_probability"], r["verified_draw"]) for r in group)
        ) / len(group)
        metrics.append({"league": league, "horizon": horizon, "n": len(group), "brier_score": brier, "log_loss": logloss, "log_loss_clip": eps})
        bins: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in group:
            p = row["forecast_probability"]
            index = len(bin_edges) - 2
            for candidate in range(len(bin_edges) - 1):
                if bin_edges[candidate] <= p < bin_edges[candidate + 1]:
                    index = candidate
                    break
            bins[index].append(row)
        for bin_index, bin_rows in sorted(bins.items()):
            n = len(bin_rows)
            mean_p = sum(row["forecast_probability"] for row in bin_rows) / n
            events = sum(row["verified_draw"] for row in bin_rows)
            observed = events / n
            low, high = wilson(events, n)
            tables.append({
                "league": league,
                "forecast_horizon": horizon,
                "bin_left": bin_edges[bin_index],
                "bin_right": bin_edges[bin_index + 1],
                "boundary_rule": "[left, right), except final bin includes 1",
                "n": n,
                "draws": events,
                "non_draws": n - events,
                "mean_forecast_probability": mean_p,
                "observed_frequency": observed,
                "calibration_gap_observed_minus_mean_forecast": observed - mean_p,
                "observed_frequency_95ci_low": low,
                "observed_frequency_95ci_high": high,
                "gap_95ci_low": low - mean_p if low is not None else None,
                "gap_95ci_high": high - mean_p if high is not None else None,
                "sparse_bin": n < 30,
            })
    return tables, metrics


def common_horizon_comparison(
    forecasts: list[dict[str, Any]],
    horizons: list[str],
    closing_horizon: str,
    bootstrap_replicates: int,
    seed: str,
) -> list[dict[str, Any]]:
    eligible = [
        row for row in forecasts
        if row.get("analysis_eligible")
        and row.get("forecast_probability") is not None
        and row.get("verified_draw") in (0, 1)
        and row.get("horizon_type") == "pregame"
    ]
    by_fixture: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in eligible:
        by_fixture[row["fixture_id"]][row["forecast_horizon"]] = row
    common = sorted(
        fixture_id for fixture_id, values in by_fixture.items()
        if all(horizon in values for horizon in horizons)
    )
    if not common or closing_horizon not in horizons:
        return [
            {
                "forecast_horizon": horizon,
                "common_fixture_count": len(common),
                "closing_reference_horizon": closing_horizon,
                "brier_score_common_set": None,
                "paired_brier_difference_vs_closing": None,
                "paired_difference_95ci_low": None,
                "paired_difference_95ci_high": None,
                "bootstrap_unit": "fixture",
                "status": "no_common_fixture_set",
            }
            for horizon in horizons
        ]
    rng = random.Random(seed)
    results = []
    for horizon in horizons:
        paired = []
        for fixture_id in common:
            row = by_fixture[fixture_id][horizon]
            close = by_fixture[fixture_id][closing_horizon]
            outcome = row["verified_draw"]
            horizon_loss = (row["forecast_probability"] - outcome) ** 2
            closing_loss = (close["forecast_probability"] - outcome) ** 2
            paired.append((horizon_loss, horizon_loss - closing_loss))
        mean_brier = sum(loss for loss, _ in paired) / len(paired)
        difference = sum(delta for _, delta in paired) / len(paired)
        if horizon == closing_horizon or len(paired) < 2:
            low = high = 0.0 if horizon == closing_horizon else None
        else:
            samples = []
            for _ in range(bootstrap_replicates):
                drawn = [paired[rng.randrange(len(paired))][1] for _ in paired]
                samples.append(sum(drawn) / len(drawn))
            samples.sort()
            low = samples[int(0.025 * (len(samples) - 1))]
            high = samples[int(0.975 * (len(samples) - 1))]
        results.append({
            "forecast_horizon": horizon,
            "common_fixture_count": len(common),
            "closing_reference_horizon": closing_horizon,
            "brier_score_common_set": mean_brier,
            "paired_brier_difference_vs_closing": difference,
            "paired_difference_95ci_low": low,
            "paired_difference_95ci_high": high,
            "bootstrap_unit": "fixture",
            "bootstrap_replicates": bootstrap_replicates,
            "status": "ok",
        })
    return results


def snapshot_coverage(forecasts: list[dict[str, Any]], leagues: list[str]) -> list[dict[str, Any]]:
    coverage = []
    horizons = sorted({str(row.get("forecast_horizon")) for row in forecasts})
    for league in leagues:
        for horizon in horizons:
            group = [
                row for row in forecasts
                if row["league"] == league and row.get("forecast_horizon") == horizon
            ]
            if not group:
                continue
            coverage.append({
                "league": league,
                "forecast_horizon": horizon,
                "selected_fixture_count": len(group),
                "available_forecasts": sum(row.get("price_observation_status") == "selected" for row in group),
                "eligible_forecasts": sum(bool(row.get("analysis_eligible")) for row in group),
                "missing_prices": sum(row.get("price_observation_status") in {
                    "no_price_before_cutoff", "price_exceeds_maximum_age", "invalid_probability"
                } for row in group),
                "missing_timing_or_cutoff": sum(row.get("price_observation_status") in {
                    "missing_timing", "timing_not_verified_in_progress", "timing_uncertain"
                } for row in group),
                "unmatched_or_uncertain_fixtures": sum(not row.get("fixture_result_eligible") for row in group),
                "unresolved_outcomes": sum(row.get("verified_draw") not in (0, 1) for row in group),
                "uncertain_match_timing": sum(
                    row.get("horizon_type") == "in_game_match_clock"
                    and row.get("timing_status") not in {
                        "match_clock_aligned",
                        "halftime_proxy_last_first_half_play",
                    }
                    for row in group
                ),
                "approximate_match_timing": sum(
                    row.get("horizon_type") == "in_game_match_clock"
                    and row.get("timing_precision") in {
                        "interpolated_between_commentary_plays",
                        "approximate",
                    }
                    for row in group
                ),
            })
    return coverage


def validate_analysis_rows(rows: list[dict[str, Any]], forecasts: list[dict[str, Any]]) -> dict[str, Any]:
    listing_keys = [(row["league"], row["event_id"], row["market_id"]) for row in rows]
    if len(listing_keys) != len(set(listing_keys)):
        raise ValueError("Duplicate event/market rows in validation evidence")
    for row in rows:
        if row["orientation_valid"]:
            if row["yes_index"] >= len(row["token_ids"]) or row["token_ids"][row["yes_index"]] != row["yes_token_id"]:
                raise ValueError(f"Outcome/token orientation mismatch for market {row['market_id']}")
            if row["outcome_type"] != "draw":
                raise ValueError(f"Unexpected outcome label for draw market {row['market_id']}")
    analysis_keys = [
        (row["fixture_id"], row["outcome_type"], row["forecast_horizon"])
        for row in forecasts
    ]
    if len(analysis_keys) != len(set(analysis_keys)):
        raise ValueError("More than one analysis forecast per fixture/outcome/horizon")
    for row in forecasts:
        if row.get("price_observation_status") != "selected":
            continue
        requested = parse_iso(row.get("requested_timestamp_utc"))
        selected = parse_iso(row.get("selected_observation_timestamp_utc"))
        probability = row.get("forecast_probability")
        if requested is None or selected is None or selected > requested:
            raise ValueError(f"Future-observation leakage in market {row['market_id']}")
        if probability is None or not 0 <= probability <= 1:
            raise ValueError(f"Invalid forecast probability in market {row['market_id']}")
        if row.get("observation_age_minutes", math.inf) > row.get("max_lookback_minutes", 0):
            raise ValueError(f"Forecast exceeds configured observation age in market {row['market_id']}")
        if row.get("analysis_eligible") and row.get("token_mapping_status") != "matched":
            raise ValueError(f"Token outcome mapping not cross-verified for market {row['market_id']}")
        if row.get("analysis_eligible") and row.get("settlement_crosscheck_status") != "matched":
            raise ValueError(f"Settlement labels not cross-verified for market {row['market_id']}")
        if row.get("analysis_eligible") and row.get("horizon_type") == "in_game_match_clock":
            if row.get("timing_status") not in {
                "match_clock_aligned", "halftime_proxy_last_first_half_play"
            } or row.get("in_progress_check") != "passed":
                raise ValueError(f"In-game forecast timing is not verified for market {row['market_id']}")
    return {
        "validation_evidence_unique_event_market_rows": len(listing_keys),
        "analysis_fixture_outcome_horizon_rows": len(analysis_keys),
        "duplicate_analysis_rows": 0,
        "future_observation_leaks": 0,
        "invalid_probabilities": 0,
        "orientation_failures_selected": sum(
            row.get("price_observation_status") == "selected"
            and row.get("token_mapping_status") != "matched"
            for row in forecasts
        ),
        "settlement_crosscheck_failures_selected": sum(
            row.get("price_observation_status") == "selected"
            and row.get("settlement_crosscheck_status") != "matched"
            for row in forecasts
        ),
        "selected_snapshot_count": sum(row.get("price_observation_status") == "selected" for row in forecasts),
        "analysis_eligible_snapshot_count": sum(bool(row.get("analysis_eligible")) for row in forecasts),
    }


def create_reliability_plot(calibration: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    pooled = [
        row for row in calibration
        if row["league"] == "pooled"
    ]
    horizons = sorted({str(row["forecast_horizon"]) for row in pooled})
    if not horizons:
        horizons = ["no_eligible_horizons"]
    columns = min(3, len(horizons))
    rows_count = math.ceil(len(horizons) / columns)
    fig, axes = plt.subplots(rows_count, columns, figsize=(5 * columns, 4.5 * rows_count), squeeze=False)
    for index, horizon in enumerate(horizons):
        axis = axes[index // columns][index % columns]
        axis.plot([0, 1], [0, 1], linestyle="--", color="black", label="Perfect calibration")
        horizon_rows = [row for row in pooled if row["forecast_horizon"] == horizon]
        if horizon_rows:
            x = [row["mean_forecast_probability"] for row in horizon_rows]
            y = [row["observed_frequency"] for row in horizon_rows]
            lower = [max(0.0, value - row["observed_frequency_95ci_low"]) for value, row in zip(y, horizon_rows)]
            upper = [max(0.0, row["observed_frequency_95ci_high"] - value) for value, row in zip(y, horizon_rows)]
            axis.errorbar(x, y, yerr=[lower, upper], fmt="o", capsize=3, label="Pooled")
            for row in horizon_rows:
                axis.annotate(f"n={row['n']}", (row["mean_forecast_probability"], row["observed_frequency"]), xytext=(4, 4), textcoords="offset points")
            axis.set_title(horizon)
        else:
            axis.text(0.5, 0.45, "No eligible forecasts", ha="center", va="center", wrap=True)
            axis.set_title(horizon)
        axis.set(xlim=(0, 1), ylim=(0, 1), xlabel="Mean quoted draw probability", ylabel="Observed draw frequency")
        axis.grid(alpha=0.25)
        if horizon_rows:
            axis.legend(loc="upper left")
    for index in range(len(horizons), rows_count * columns):
        axes[index // columns][index % columns].set_visible(False)
    fig.tight_layout()
    (PROCESSED / "reliability.png").parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PROCESSED / "reliability.png", dpi=160)
    plt.close(fig)


def write_report(
    listings: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    forecasts: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    fixture_candidates: list[dict[str, Any]],
    bin_edges: list[float],
    token_audits: list[dict[str, Any]],
) -> None:
    per_league = Counter(item["league"] for item in listings)
    fixture_ids: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        fixture_ids[row["league"]].add(row["fixture_id"])
    valid_outcomes = sum(row["api_settled_draw"] is not None for row in rows)
    count_text = "\n".join(
        f"| {league} | {per_league[league]} | {len(fixture_ids[league])} | "
        f"{sum(row['league'] == league and row['api_settled_draw'] is not None for row in rows)} | "
        f"{sum(row['league'] == league and row.get('analysis_eligible') for row in forecasts)} |"
        for league in LEAGUES
    )
    metrics_text = "\n".join(
        f"| {m['league']} | {m['horizon']} | {m['n']} | {m['brier_score']:.4f} | {m['log_loss']:.4f} |"
        for m in metrics
    ) or "| none | — | 0 | — | — |"
    exclusion_counter = Counter(
        (row["league"], row.get("exclusion_reason") or "eligible")
        for row in forecasts
    )
    result_source_counts = Counter(row.get("independent_result_source_status") for row in forecasts)
    settlement_comparisons = Counter(str(row.get("settlement_matches_independent_result")) for row in forecasts)
    secondary_comparisons = Counter(str(row.get("secondary_matches_official_result")) for row in forecasts)
    token_mapping_counts = Counter(row.get("token_mapping_status") for row in token_audits)
    token_settlement_counts = Counter(row.get("settlement_crosscheck_status") for row in token_audits)
    exclusion_text = "\n".join(
        f"| {league} | {reason} | {count} |"
        for (league, reason), count in sorted(exclusion_counter.items())
    ) or "| none | none | 0 |"
    same_score_event_pairs = sum(bool(row.get("same_espn_event")) for row in fixture_candidates)
    one_score_query_matches = sum(
        bool(row.get("left_espn_event_id")) != bool(row.get("right_espn_event_id"))
        for row in fixture_candidates
    )
    no_score_query_matches = sum(
        not row.get("left_espn_event_id") and not row.get("right_espn_event_id")
        for row in fixture_candidates
    )
    issue_rows = []
    for league in LEAGUES:
        league_rows = [row for row in rows if row["league"] == league]
        issue_rows.append(
            f"| {league} | "
            f"{sum(row['draw_market_count'] > 1 for row in league_rows)} | "
            f"{sum(not row['orientation_valid'] for row in league_rows)} | "
            f"{sum(row['api_settled_draw'] is None for row in league_rows)} | "
            f"{sum(not row.get('kickoff_utc') for row in league_rows)} | "
            f"{sum(row.get('nearby_same_teams_candidate', False) for row in league_rows)} |"
        )
    age_text = "\n".join(
        f"| {minutes} | {sum(row.get('status') == 'selected' and row.get('observation_age_minutes', math.inf) <= minutes for row in forecasts)} |"
        for minutes in (30, 60, 180, 360)
    )
    trace_rows = []
    for league in LEAGUES:
        league_pilot = [row for row in forecasts if row["league"] == league]
        if league_pilot:
            trace_rows.append(league_pilot[0])
    trace_text = "\n".join(
        f"| {row['league']} | {row['event_id']} / {row['market_id']} | "
        f"{row.get('requested_timestamp_utc') or '—'} | "
        f"{row.get('selected_observation_timestamp_utc') or '—'} | "
        f"{row.get('forecast_probability') if row.get('forecast_probability') is not None else '—'} | "
        f"{row.get('api_settled_draw') if row.get('api_settled_draw') is not None else '—'} | "
        f"{row.get('independent_result_source_status')} | {row.get('eligibility_status')} | "
        f"`{row.get('market_raw_ref')}` |"
        for row in trace_rows
    ) or "| none | — | — | — | — | — | — | — | — |"
    rule_categories = [
        "regulation_or_90_minutes", "stoppage_time", "extra_time", "penalties",
        "postponement", "cancellation", "void",
    ]
    rule_text = "\n".join(
        f"| {league} | {sum(row['league'] == league for row in forecasts)} | "
        + " | ".join(
            str(sum(
                row["league"] == league
                and row.get(f"rule_mentions_{category}") == "yes"
                for row in forecasts
            ))
            for category in rule_categories
        ) + " |"
        for league in LEAGUES
    )
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        "# Validation pilot report\n\n"
        f"Generated {utc_now()}. This report is rebuilt by `python3 scripts/validate_pipeline.py`.\n\n"
        "## Coverage\n\n"
        "The first count is live series-event listings retrieved, not confirmed unique fixtures. "
        "Fixture IDs below are conservative exact keys built from league, normalized ordered team names, "
        "and UTC kickoff date; nearby same-team dates are flagged, not merged, because they may be "
        "rescheduled fixtures or separate meetings.\n\n"
        "| League | Retrieved listings | Exact fixture keys with draw listing | API-binary resolved draw markets | Analysis-eligible forecasts |\n"
        "|---|---:|---:|---:|---:|\n" + count_text + "\n\n"
        "### Listing-level validation flags\n\n"
        "| League | Ambiguous draw-market listings | Orientation failures | Missing/unresolved API settlement | Missing timezone-aware kickoff | Nearby same-team date candidates |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        + "\n".join(issue_rows) + "\n\n"
        f"Total draw listings: {len(rows)}. API-binary resolved draw markets: {valid_outcomes}. "
        f"Eligible independent forecasts: {sum(bool(r.get('analysis_eligible')) for r in forecasts)}. "
        f"Conservative exact fixture keys: {sum(len(fixture_ids[league]) for league in LEAGUES)}. "
        f"Possible nearby reschedule/duplicate pairs flagged, not merged: {len(fixture_candidates)} "
        f"(same ESPN event IDs across both date queries: {same_score_event_pairs}). "
        "The historical notebook's 2,334 remains a listing count and is not overwritten by this run.\n\n"
        "| Audit gap | Status | Evidence and limitation |\n|---|---|---|\n"
        "| Listing-to-fixture identity | unresolved | Conservative league/team/date keys are available; nearby pairs are not merged without authoritative reschedule evidence. |\n"
        "| Outcome market and token orientation | verified for pilot | Gamma arrays match named CLOB outcome-token mappings and resolved winners/prices for "
        f"{sum(row.get('token_mapping_status') == 'matched' and row.get('settlement_crosscheck_status') == 'matched' for row in token_audits)} of {len(token_audits)} pilot markets; the full listing universe is not cross-checked. |\n"
        f"| Kickoff, teams, and sporting outcome | verified for eligible subset; blocked elsewhere | Matched completed ESPN scores are accepted; official competition scores are an optional cross-check. {sum(row.get('analysis_eligible') for row in forecasts)} pilot forecasts currently pass all checks. |\n"
        "| Contract settlement rules | partially verified | Listing-specific rule text and API resolution metadata are preserved, but unspecific edge cases and external settlement authority remain unresolved. |\n"
        "| Historical price meaning | unresolved | Docs and bounded/overlap experiments establish API behavior but not whether prices are trades, midpoints, or timestamped original events. |\n"
        "| One-hour forecast selection | verified for pilot | Latest observation at/before cutoff selected with no future leakage; interpretation of observation age remains limited by undocumented sampling semantics. |\n\n"
        "### Forecast-pilot exclusions and age sensitivity\n\n"
        "| League | Exclusion reason | Rows |\n|---|---|---:|\n"
        + exclusion_text + "\n\n"
        "Coverage at one-hour-before-kickoff by maximum observation age, among the fixed pilot sample:\n\n"
        "| Maximum age (minutes) | Forecast observations available |\n|---:|---:|\n"
        + age_text + "\n\n"
        "This is observation-age sensitivity only; eligibility still requires independent outcome validation.\n\n"
        "Pilot result-source classes: "
        + ", ".join(f"{source}={count}" for source, count in sorted(result_source_counts.items()))
        + ". Market-settlement comparisons (True/False/None): "
        + ", ".join(f"{result}={count}" for result, count in sorted(settlement_comparisons.items()))
        + ". Secondary/official result comparisons (True/False/None): "
        + ", ".join(f"{result}={count}" for result, count in sorted(secondary_comparisons.items()))
        + ". A reliably matched completed ESPN score is eligible without an official competition feed; discrepancies remain excluded for review.\n\n"
        f"Gamma/CLOB token orientation check in the {len(token_audits)}-market pilot: {dict(token_mapping_counts)}. "
        f"Settlement-value cross-check: {dict(token_settlement_counts)}. "
        "The CLOB `tokens[].outcome`, `token_id`, `price`, and `winner` records independently verify the array pairing for these sampled markets, not all listings.\n\n"
        "## Traceable pilot examples\n\n"
        "| League | Event / market | Requested cutoff UTC | Selected observation UTC | Probability | API draw settlement | Independent source class | Eligibility | Raw market response |\n"
        "|---|---|---|---|---:|---:|---|---|---|\n"
        + trace_text + "\n\n"
        "### Rule text in the selected pilot (mentions, not inferred rule outcomes)\n\n"
        "| League | Pilot rows | 90 minutes/regulation | Stoppage time | Extra time | Penalties | Postponement | Cancellation | Void |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        + rule_text + "\n\n"
        "A zero means the sampled description did not state the topic; it does not mean the market permits or disallows it. Preserve full descriptions in `validation_evidence.csv` and `forecasts.csv`.\n\n"
        "## Calibration (exploratory)\n\n"
        f"Only rows that pass the implemented independent-result eligibility checks enter this table. "
        "With small samples, intervals are Wilson 95% intervals for observed frequency, translated to "
        "a gap interval by subtracting mean forecast. These intervals do not account for sampling bias. "
        f"Forecast bins use edges {bin_edges}, [left, right), with the final bin including 1. Log loss clips probabilities "
        "to [1e-6, 1-1e-6].\n\n"
        "| League | Horizon | n | Brier | Log loss |\n|---|---|---:|---:|---:|\n"
        + metrics_text + "\n\n"
        "## Interpretation limits and source status\n\n"
        "- Gamma series membership is used as the league assignment; legacy events lacking verified series metadata are not added by title heuristic.\n"
        "- Home/away order uses the structured event-team `ordering` field only when both `home` and `away` are explicit. Otherwise the event title supplies order and structured team names can corroborate the unordered pair. Known ESPN naming variants use a small explicit alias map; this improves matching but does not make secondary-feed matches authoritative.\n"
        f"- Exact fixture matching uses competition, ordered canonical team names (including a small explicit alias map), and UTC calendar date. Different dates are never automatically merged. Same ordered teams within 21 days produce a review candidate. `fixture_candidates.csv` contains {len(fixture_candidates)} nearby-date pairs: {same_score_event_pairs} share an ESPN event ID across both date queries, {one_score_query_matches} match on only one queried date, and {no_score_query_matches} match neither date. No candidate is confirmed as a duplicate/reschedule. Unrecognized aliases or longer reschedule gaps may remain. The old notebook's ambiguous Real Madrid–Athletic Club row had no market/event ID and was not recoverable as that exact observation; current live listing scan detected zero events with multiple full-match draw markets.\n"
        "- `market.closed` alone is not settlement proof. The pipeline requires `umaResolutionStatus=resolved`, exactly aligned outcomes/prices, and complementary binary prices before recording an API settlement.\n"
        "- Independent score validation uses ESPN for all five leagues, explicitly secondary and not league-authoritative. The official Premier League feed is additionally queried for Premier League rows. Other leagues need official competition data before they can qualify as authoritative.\n"
        "- Official Premier League API times are UTC epoch milliseconds; ESPN provides ISO timestamps. Naive source timestamps are left unresolved. The official feed has a fixture `kickoff` value and preserves `provisionalKickoff`; it does not provide an actual whistle timestamp in the fields used here.\n"
        "- Market descriptions provide listing-specific rules. In the 25-row pilot, postponement and cancellation are mentioned in every description; regulation/90 minutes and stoppage time appear in only some; extra time, penalties, and void are not stated in the sampled descriptions. The historical Real Madrid–Atletico market description also has a cancellation/delay cutoff; its complete text is preserved in `data/processed/price_api_experiments.json`. These wording differences are contract-specific, not league-wide rules.\n"
        "- Current Polymarket docs describe `/prices-history` as historical price data, with `t` integer timestamps and `p` numeric values; they do not establish whether `p` is a trade, midpoint, or sampled-bucket statistic. No pagination parameter is documented. Observation timestamp is not proven to be an original trade time, so computed age is API-observation age only.\n"
        "- The implementation runs a bounded/unbounded and overlapping-window comparison on the prior Real Madrid–Atletico market; raw responses and equality counts are in `data/processed/price_api_experiments.json`.\n"
        "- Numeric event and market identifiers are not interchangeable: the live `/events/521526` and `/markets/521526` lookups returned different objects. The market lookup is the correct source for that prior draw contract.\n"
        "- ESPN scoreboard results are used across leagues when completed scores and team matching are available. The official Premier League Football API is an optional additional cross-check; official competition feeds are not required for eligibility.\n"
        "- Historical docs/API links: https://docs.polymarket.com/api-reference/markets/get-prices-history ; https://docs.polymarket.com/market-data/overview ; https://docs.polymarket.com/concepts/resolution . Premier League source: https://footballapi.pulselive.com/football/fixtures.\n"
        "\n## Decision before calibration\n\n"
        "Do not interpret the pooled calibration table as representative unless the sample is sufficiently large, selected reproducibly, and fixture identity and sporting outcomes are independently matched. "
        "First adjudicate duplicate/rescheduled candidates, review settlement discrepancies and contract-specific rules, and decide whether API `p` is an acceptable forecast measure after its semantics are documented.\n",
        encoding="utf-8",
    )


def run(args: argparse.Namespace) -> None:
    api = API(refresh=args.refresh, delay=args.rate_delay)
    listings, page_refs = fetch_listings(api, max_pages=args.max_pages)
    rows, fixture_rows = audit_rows(listings)
    price_experiments = run_price_api_experiments(api)
    candidate_pairs = fixture_candidate_pairs(fixture_rows)
    candidate_evidence = audit_fixture_candidates(api, candidate_pairs, rows)
    write_csv(PROCESSED / "listings.csv", [
        {"league": item["league"], "event_id": item["event"].get("id"),
         "title": item["event"].get("title"), "slug": item["event"].get("slug"),
         "closed": item["event"].get("closed"), "event_date": item["event"].get("eventDate"),
         "start_time": item["event"].get("startTime"), "series": item["event"].get("series"),
         "raw_ref": item["raw_ref"]}
        for item in listings
    ])
    write_csv(PROCESSED / "fixtures.csv", fixture_rows)
    write_csv(PROCESSED / "validation_evidence.csv", rows)

    pilots = select_pilot(rows, args.pilot_per_league, args.seed)
    forecasts: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    token_audits: list[dict[str, Any]] = []
    for row in pilots:
        token_audit = audit_token_mapping(api, row)
        token_audits.append(token_audit)
        independent, error = fetch_espn_result(api, row)
        if row["league"] == "Premier League":
            official = fetch_pl_official(api, row)
        else:
            official = {"source": "not queried for this competition", "status": "no official connector"}
        prices, selected = collect_price(api, row, args.max_lookback_minutes)
        observations.extend(prices)
        verified_draw = None
        result_authority = "unverified"
        score_result_match = None
        if official.get("score_home") is not None and official.get("score_away") is not None:
            verified_draw = int(official["score_home"] == official["score_away"])
            result_authority = "official_competition_feed"
            score_result_match = "matched"
        elif independent and independent.get("score_match_status") == "matched" and independent.get("completed"):
            verified_draw = int(independent["score_home"] == independent["score_away"])
            result_authority = "secondary_only"
            score_result_match = "matched_secondary"
        settlement_matches_result = (
            verified_draw == row["api_settled_draw"]
            if verified_draw is not None and row["api_settled_draw"] is not None
            else None
        )
        secondary_draw = None
        if independent and independent.get("score_match_status") == "matched" and independent.get("completed"):
            secondary_draw = int(independent["score_home"] == independent["score_away"])
        secondary_matches_official = (
            secondary_draw == verified_draw
            if secondary_draw is not None and result_authority == "official_competition_feed"
            else None
        )
        result_kickoff = parse_iso(official.get("kickoff_utc") or (independent or {}).get("event_date"))
        api_kickoff = parse_iso(row.get("market_kickoff_utc"))
        provisional = official.get("provisional_kickoff")
        provisional_millis = provisional.get("millis") if isinstance(provisional, dict) else None
        provisional_kickoff = (
            datetime.fromtimestamp(float(provisional_millis) / 1000, tz=timezone.utc)
            if provisional_millis else None
        )
        kickoff_difference_minutes = (
            (result_kickoff - api_kickoff).total_seconds() / 60
            if result_kickoff and api_kickoff else None
        )
        completed_result = (
            official.get("status") == "C"
            if result_authority == "official_competition_feed"
            else bool(independent and independent.get("completed"))
        )
        eligible = (
            result_authority in {"official_competition_feed", "secondary_only"}
            and score_result_match in {"matched", "matched_secondary"}
            and settlement_matches_result is True
            and completed_result
            and kickoff_difference_minutes is not None
            and abs(kickoff_difference_minutes) <= 15
            and secondary_matches_official is not False
            and token_audit.get("token_mapping_status") == "matched"
            and token_audit.get("settlement_crosscheck_status") == "matched"
            and selected.get("status") == "selected"
            and selected.get("leakage_check")
            and selected.get("observation_age_minutes", math.inf) <= args.max_lookback_minutes
        )
        if eligible:
            eligibility_status, exclusion_reason = "eligible_exploratory", None
        elif verified_draw is None:
            eligibility_status, exclusion_reason = "excluded", "independent sporting outcome not verified"
        elif settlement_matches_result is False:
            eligibility_status, exclusion_reason = "excluded", "API settlement conflicts with independent sporting result"
        elif token_audit.get("token_mapping_status") != "matched":
            eligibility_status, exclusion_reason = "excluded", "Gamma outcome/token pairing not confirmed by CLOB market metadata"
        elif token_audit.get("settlement_crosscheck_status") != "matched":
            eligibility_status, exclusion_reason = "excluded", "CLOB market settlement values do not confirm Gamma outcome labels"
        elif secondary_matches_official is False:
            eligibility_status, exclusion_reason = "excluded", "secondary and official sporting outcomes conflict"
        elif selected.get("status") != "selected":
            eligibility_status, exclusion_reason = "excluded", f"forecast unavailable: {selected.get('status')}"
        elif not completed_result:
            eligibility_status, exclusion_reason = "excluded", "independent score source does not report completed status"
        elif kickoff_difference_minutes is None or abs(kickoff_difference_minutes) > 15:
            eligibility_status, exclusion_reason = "excluded", "independently reported kickoff missing or differs by more than 15 minutes"
        else:
            eligibility_status, exclusion_reason = "excluded", "kickoff or source validation incomplete"
        forecast_row = {
            "league": row["league"],
            "event_id": row["event_id"],
            "fixture_id": row["fixture_id"],
            "outcome_type": "draw",
            "market_id": row["market_id"],
            "condition_id": row["condition_id"],
            "token_id": row["yes_token_id"],
            "token_mapping_status": token_audit.get("token_mapping_status"),
            "settlement_crosscheck_status": token_audit.get("settlement_crosscheck_status"),
            "token_metadata_raw_ref": token_audit.get("raw_ref"),
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "api_kickoff_utc": row["market_kickoff_utc"],
            "independent_kickoff_utc": official.get("kickoff_utc") or (independent or {}).get("event_date"),
            "independent_kickoff_timezone": official.get("kickoff_timezone") or "UTC ISO timestamp",
            "official_provisional_kickoff_utc": provisional_kickoff.isoformat() if provisional_kickoff else None,
            "kickoff_difference_minutes": kickoff_difference_minutes,
            "independent_score_home": official.get("score_home") if official.get("score_home") is not None else (independent or {}).get("score_home"),
            "independent_score_away": official.get("score_away") if official.get("score_away") is not None else (independent or {}).get("score_away"),
            "independent_result_source_status": result_authority,
            "independent_result_match_status": score_result_match or error,
            "secondary_feed_match_status": (independent or {}).get("score_match_status"),
            "secondary_event_id": (independent or {}).get("event_id"),
            "secondary_event_date": (independent or {}).get("event_date"),
            "secondary_event_status": (independent or {}).get("status"),
            "secondary_score_home": (independent or {}).get("score_home"),
            "secondary_score_away": (independent or {}).get("score_away"),
            "secondary_team_match_method": (independent or {}).get("team_match_method"),
            "official_fixture_status": official.get("status"),
            "official_provisional_kickoff": official.get("provisional_kickoff"),
            "api_settled_draw": row["api_settled_draw"],
            "verified_draw": verified_draw,
            "secondary_observed_draw": secondary_draw,
            "settlement_matches_independent_result": settlement_matches_result,
            "secondary_matches_official_result": secondary_matches_official,
            **selected,
            "analysis_eligible": eligible,
            "eligibility_status": eligibility_status,
            "exclusion_reason": exclusion_reason,
            "market_rules": row["market_rules_text"],
            **rule_features(row["market_rules_text"]),
            "market_resolution_source": row["market_resolution_source"],
            "market_raw_ref": row["source_raw_ref"],
            "independent_source_raw_ref": official.get("source_ref") or official.get("raw_ref") or (independent or {}).get("raw_ref"),
            "official_source_raw_ref": official.get("season_ref"),
        }
        forecasts.append(forecast_row)
        row["pilot_sample"] = True
        row["eligibility_status"] = eligibility_status
        row["exclusion_reason"] = exclusion_reason
        row["independent_result_source_status"] = result_authority
        row["market_resolution_matches_result"] = settlement_matches_result
        row["independent_score_home"] = forecast_row["independent_score_home"]
        row["independent_score_away"] = forecast_row["independent_score_away"]
        row["market_raw_ref"] = row["source_raw_ref"]
        row["independent_source_raw_ref"] = forecast_row["independent_source_raw_ref"]
        row["official_source_raw_ref"] = forecast_row["official_source_raw_ref"]

    check_results = validate_analysis_rows(rows, forecasts)
    write_csv(PROCESSED / "validation_evidence.csv", rows)
    write_csv(PROCESSED / "forecasts.csv", forecasts)
    write_csv(PROCESSED / "price_observations.csv", observations)
    write_csv(PROCESSED / "token_orientation_audit.csv", token_audits)
    age_thresholds = (30, 60, 180, 360)
    write_csv(PROCESSED / "forecast_age_sensitivity.csv", [
        {
            "league": league,
            "maximum_observation_age_minutes": threshold,
            "pilot_n": sum(row["league"] == league for row in forecasts),
            "forecast_observations_available": sum(
                row["league"] == league
                and row.get("status") == "selected"
                and row.get("observation_age_minutes", math.inf) <= threshold
                for row in forecasts
            ),
        }
        for league in LEAGUES
        for threshold in age_thresholds
    ])
    write_csv(PROCESSED / "fixture_candidates.csv", candidate_evidence)
    calibration, metrics = calibration_rows(forecasts, args.bin_edges)
    write_csv(PROCESSED / "calibration.csv", calibration)
    write_csv(PROCESSED / "calibration_metrics.csv", metrics)
    create_reliability_plot(calibration)
    (PROCESSED / "price_api_experiments.json").write_text(
        json.dumps(price_experiments, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (PROCESSED / "validation_checks.json").write_text(
        json.dumps(check_results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (PROCESSED / "token_orientation_counts.json").write_text(
        json.dumps({
            "pilot_market_count": len(token_audits),
            "token_mapping_status": dict(Counter(row.get("token_mapping_status") for row in token_audits)),
            "settlement_crosscheck_status": dict(Counter(row.get("settlement_crosscheck_status") for row in token_audits)),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(PROCESSED / "exclusions.csv", [
        {
            "league": row["league"],
            "event_id": row["event_id"],
            "market_id": row["market_id"],
            "fixture_id": row["fixture_id"],
            "eligibility_status": row.get("eligibility_status"),
            "exclusion_reason": row.get("exclusion_reason") or row.get("listing_validation_issue"),
            "raw_ref": row["source_raw_ref"],
        }
        for row in rows
        if row.get("eligibility_status") == "excluded" or row.get("listing_validation_issue")
    ])
    listing_status_counts: dict[tuple[str, str], int] = Counter(
        (row["league"], row["listing_validation_issue"])
        for row in rows
        if row.get("listing_validation_issue")
    )
    write_csv(PROCESSED / "listing_status_counts.csv", [
        {"league": league, "validation_issue": issue, "count": count}
        for (league, issue), count in sorted(listing_status_counts.items())
    ])
    by_league: dict[str, dict[str, int]] = {}
    for league in LEAGUES:
        league_rows = [row for row in rows if row["league"] == league]
        listing_events = {item["event"].get("id") for item in listings if item["league"] == league}
        by_league[league] = {
            "retrieved_listings": len(listing_events),
            "draw_listings": len(league_rows),
            "exact_fixture_keys": len({row["fixture_id"] for row in league_rows}),
            "verified_api_settlements": sum(row["api_settled_draw"] is not None for row in league_rows),
            "pilot_forecasts": sum(row["league"] == league for row in forecasts),
            "eligible_forecasts": sum(row["league"] == league and row["analysis_eligible"] for row in forecasts),
            "exclusions": sum(row["league"] == league and row["eligibility_status"] == "excluded" for row in forecasts),
        }
    PROCESSED.mkdir(parents=True, exist_ok=True)
    (PROCESSED / "coverage.json").write_text(json.dumps({
        "generated_at_utc": utc_now(),
        "listings_are_not_unique_fixtures": True,
        "pagination_sources": page_refs,
        "by_league": by_league,
        "validation_flags_by_league": {
            league: {
                "ambiguous_draw_listings": sum(row["league"] == league and row["draw_market_count"] > 1 for row in rows),
                "orientation_failures": sum(row["league"] == league and not row["orientation_valid"] for row in rows),
                "unresolved_settlements": sum(row["league"] == league and row["api_settled_draw"] is None for row in rows),
                "missing_timezone_aware_kickoff": sum(row["league"] == league and not row.get("market_kickoff_utc") for row in rows),
                "nearby_same_team_candidates": sum(row["league"] == league and row.get("nearby_same_teams_candidate", False) for row in rows),
            }
            for league in LEAGUES
        },
        "settlement_status_by_league": {
            league: dict(Counter(
                row["settlement_status"] for row in rows if row["league"] == league
            ))
            for league in LEAGUES
        },
        "pilot_size_per_league": args.pilot_per_league,
        "pilot_seed": args.seed,
        "max_lookback_minutes": args.max_lookback_minutes,
        "probability_bin_edges": args.bin_edges,
        "pilot_token_mapping_status": dict(Counter(row.get("token_mapping_status") for row in token_audits)),
        "pilot_settlement_crosscheck_status": dict(Counter(row.get("settlement_crosscheck_status") for row in token_audits)),
        "fixture_key_warning": "exact conservative identity key; possible reschedules are not merged",
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_report(
        listings, rows, forecasts, metrics, candidate_evidence,
        args.bin_edges, token_audits,
    )
    print(json.dumps({"coverage": by_league, "pilot": len(forecasts), "eligible": sum(bool(r["analysis_eligible"]) for r in forecasts), "calibration_bins": len(calibration), "report": str(REPORT.relative_to(ROOT))}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Refresh live API responses and preserve prior snapshots")
    parser.add_argument("--max-pages", type=int, help="Optional per-league pagination cap for a smoke run")
    parser.add_argument("--pilot-per-league", type=int, default=5)
    parser.add_argument("--seed", default="capstone-validation-v1")
    parser.add_argument("--max-lookback-minutes", type=int, default=360, help="Maximum historical-observation age allowed before the one-hour-before-kickoff target")
    parser.add_argument(
        "--bin-edges",
        default=",".join(str(edge) for edge in PRICE_BINS),
        help="Comma-separated increasing probability-bin edges from 0 to 1",
    )
    parser.add_argument("--rate-delay", type=float, default=RATE_DELAY_SECONDS)
    args = parser.parse_args()
    if args.pilot_per_league < 1 or args.max_lookback_minutes < 1 or args.rate_delay < 0:
        parser.error("pilot size/lookback must be positive and rate delay nonnegative")
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
        parser.error("--bin-edges must be strictly increasing and start at 0 and end at 1")
    try:
        run(args)
    except Exception as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
