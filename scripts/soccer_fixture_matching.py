"""Match Polymarket fixtures to FotMob fixtures.

Polymarket uses full registered club names ("BV Borussia 09 Dortmund"),
FotMob uses short names ("Borussia Dortmund"). Each team name is resolved
against the team list of the same FotMob league season, then the fixture
must exist with that exact home/away pair near kickoff.
"""
from __future__ import annotations

import csv
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

if __package__:
    from . import validate_pipeline as pipeline
else:
    import validate_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]
CROSSWALK_PATH = ROOT / "data" / "reference" / "team_name_crosswalk.csv"

KICKOFF_WINDOW_SECONDS = 3 * 3600
MIN_SIDE_SIMILARITY = 0.85   # every real correct match scored 0.89 or higher
MIN_MARGIN = 0.15            # best team must beat the runner-up by this much



#cleans team name using normailzer
@lru_cache(maxsize=65536) #decorator that tells us to check if we already ran this function or not
def _normalized(name: str) -> str:
    """Clean a team name with the project's shared normalizer (cached)."""
    return pipeline.normalize_team(name)


#reads hand written name pairs takes normalized polymarkte one and aps to nornalize FotNob bane 
def load_crosswalk(path: Path = CROSSWALK_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    mapping = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            source = _normalized(row.get("polymarket_name") or "")
            target = _normalized(row.get("fotmob_name") or "")
            if source and target:
                mapping[source] = target
    return mapping



#Clean a name, then swap it for its crosswalk entry if it has one.
def canonical(name: str, crosswalk: dict[str, str]) -> str:
    
    normalized = _normalized(name)
    return crosswalk.get(normalized, normalized) #returns value of key exists if not then returns key

#scores two names based on how similar they are 1 being exact 0 being nothing alike 
@lru_cache(maxsize=65536)
def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 4 and short in long_:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()



def resolve_team(name: str, season_teams: list[str], crosswalk: dict[str, str]) -> tuple[str | None, str]:
    """Pick the FotMob team for one Polymarket name, from that league-season's team list.

    Returns (fotmob_name, method). method is exact, crosswalk, similarity,
    ambiguous (two teams too close to call) or none (nothing close enough).
    """
    raw = _normalized(name)
    target = crosswalk.get(raw, raw)
    via_crosswalk = raw in crosswalk

    # 1. An identical cleaned name wins outright.
    exact = {team for team in season_teams if _normalized(team) == target}
    if len(exact) == 1:
        return exact.pop(), ("crosswalk" if via_crosswalk else "exact")
    if len(exact) > 1:
        return None, "ambiguous"

    # 2. Otherwise score every team in the season, best first.
    scored = sorted(
        ((name_similarity(target, _normalized(team)), team) for team in set(season_teams)),
        reverse=True,
    )
    if not scored or scored[0][0] < MIN_SIDE_SIMILARITY:
        return None, "none"

    # 3. The best must clearly beat the runner-up.
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if scored[0][0] - runner_up < MIN_MARGIN:
        return None, "ambiguous"
    return scored[0][1], ("crosswalk" if via_crosswalk else "similarity")


def _team(match: dict[str, Any], side: str) -> str:
    """Team name on one side of a FotMob fixture ('home' or 'away')."""
    return str((match.get(side) or {}).get("name") or "")


@lru_cache(maxsize=65536)
def _utc(value: str) -> pd.Timestamp | None:
    """Parse a timestamp as UTC; None if it is missing or unreadable."""
    parsed = pd.to_datetime(value or None, utc=True, errors="coerce")
    return None if pd.isna(parsed) else parsed


def _in_time_window(kickoff: pd.Timestamp | None, event_date: str, match: dict[str, Any]) -> bool:
    """True if the FotMob fixture is on the same date, or within 3 hours of kickoff."""
    match_time = _utc(str((match.get("status") or {}).get("utcTime") or ""))
    if match_time is None:
        return False
    same_date = match_time.date().isoformat() == event_date
    near = kickoff is not None and abs((match_time - kickoff).total_seconds()) <= KICKOFF_WINDOW_SECONDS
    return same_date or near

def match_fotmob_fixture(fixture: dict[str, Any], matches: list[dict[str, Any]],
                         crosswalk: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], str]:
    """Find the FotMob fixture for one Polymarket fixture.
matches is one FotMob league-season. Returns (candidates, method):
    exactly one candidate means a usable match.
    """
    crosswalk = load_crosswalk() if crosswalk is None else crosswalk
    season_teams = sorted({_team(m, side) for m in matches for side in ("home", "away")} - {""})

    home, home_method = resolve_team(str(fixture.get("home_team") or ""), season_teams, crosswalk)
    away, away_method = resolve_team(str(fixture.get("away_team") or ""), season_teams, crosswalk)
    methods = {home_method, away_method}
    if "ambiguous" in methods:
        return [], "ambiguous"
    if home is None or away is None:
        return [], "none"

    kickoff = _utc(str(fixture.get("kickoff_utc") or fixture.get("scheduled_kickoff_utc") or ""))
    event_date = str(fixture.get("event_date") or fixture.get("match_date") or "")[:10]
    found = [m for m in matches
             if _team(m, "home") == home and _team(m, "away") == away
             and _in_time_window(kickoff, event_date, m)]

    if len(found) > 1:
        return found, "ambiguous"
    label = "+".join(sorted(methods))
    if found:
        return found, label

    # Nothing near the listed kickoff. A home/away pair is played once per
    # league season, so if it appears exactly once it is this game, moved.
    same_pair = [m for m in matches if _team(m, "home") == home and _team(m, "away") == away]
    if len(same_pair) != 1:
        return [], "none"
    if (same_pair[0].get("status") or {}).get("finished") is not True:
        return [], "postponed_not_played"
    return same_pair, label + "+rescheduled"