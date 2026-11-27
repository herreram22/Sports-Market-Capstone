"""Step 1: link every Polymarket fixture to its FotMob match.

Loads FotMob's fixture list for each league-season (cached in
data/raw/fotmob_league_fixtures, downloaded once if missing), runs
match_fotmob_fixture on every fixture in fixtures.csv, and writes:

- data/processed/fixture_links.csv: one row per Polymarket fixture, with the
  FotMob match id and FotMob's kickoff time. This is the key table every
  later step reads. fotmob_kickoff_utc is the corrected kickoff (it follows
  postponements); polymarket_kickoff_utc is the original listing.
- data/processed/fotmob_team_matches.csv: one row per team name and season,
  with the FotMob team it resolved to (the sanity-check table)

    python scripts/build_fixture_links.py
"""
from __future__ import annotations

import pandas as pd

if __package__:
    from . import build_soccer_goal_event_study_data as builder
    from . import soccer_fixture_matching as matching
else:
    import build_soccer_goal_event_study_data as builder
    import soccer_fixture_matching as matching

LINKS_PATH = builder.PROCESSED / "fixture_links.csv"
TEAMS_PATH = builder.PROCESSED / "fotmob_team_matches.csv"


def link_row(fixture: dict, league: str, season: str, found: list, method: str) -> dict:
    """One row of the key table for one Polymarket fixture."""
    match = found[0] if len(found) == 1 else {}
    status = match.get("status") or {}
    polymarket_kickoff = pd.to_datetime(fixture.get("kickoff_utc"), utc=True, errors="coerce")
    fotmob_kickoff = pd.to_datetime(status.get("utcTime"), utc=True, errors="coerce")
    diff = (fotmob_kickoff - polymarket_kickoff).total_seconds() / 3600 if match else None
    return {
        "fixture_id": fixture.get("fixture_id"),
        "fotmob_match_id": match.get("id"),
        "league": league,
        "season": season,
        "home_team": fixture.get("home_team"),
        "away_team": fixture.get("away_team"),
        "fotmob_home_team": matching._team(match, "home") or None,
        "fotmob_away_team": matching._team(match, "away") or None,
        "polymarket_kickoff_utc": polymarket_kickoff.isoformat() if pd.notna(polymarket_kickoff) else None,
        "fotmob_kickoff_utc": fotmob_kickoff.isoformat() if match and pd.notna(fotmob_kickoff) else None,
        "kickoff_diff_hours": round(diff, 2) if diff is not None and pd.notna(diff) else None,
        "match_method": method,
        "rescheduled": method.endswith("+rescheduled"),
        "fotmob_finished": status.get("finished") if match else None,
    }


def drop_stale_listings(table: pd.DataFrame) -> pd.DataFrame:
    """Unlink rescheduled links whose FotMob match another listing already claims.

    When a game moves, Polymarket sometimes lists it again at the new time. The
    new listing matches normally; the old one would match the same FotMob game
    through the rescheduled rule. Keep the normal match and unlink the old one.
    If only rescheduled links claim a FotMob match and there are several, keep
    none, since there is no way to tell which listing traded the game.
    """
    table = table.copy()
    claimed = table.dropna(subset=["fotmob_match_id"]).groupby("fotmob_match_id")
    for match_id, group in claimed:
        if len(group) == 1:
            continue
        normal = group[~group["rescheduled"]]
        drop = group.index[group["rescheduled"]] if len(normal) == 1 else group.index
        label = "stale_listing" if len(normal) == 1 else "ambiguous_listing"
        table.loc[drop, "match_method"] = label
        table.loc[drop, "rescheduled"] = False
        for column in ("fotmob_match_id", "fotmob_home_team", "fotmob_away_team",
                       "fotmob_kickoff_utc", "kickoff_diff_hours", "fotmob_finished"):
            table.loc[drop, column] = None
    return table


def main() -> None:
    fixtures = pd.read_csv(builder.FIXTURES_PATH, dtype=str, keep_default_na=False).to_dict("records")
    crosswalk = matching.load_crosswalk()
    seasons: dict[tuple[str, str], list] = {}
    links, teams = [], {}

    for fixture in fixtures:
        league = fixture.get("league", "")
        season = builder.fixture_season(fixture)
        if (league, season) not in seasons:
            seasons[(league, season)] = builder.read_cached_season(league, season, refresh=False)[0]
            print(f"loaded {league} {season}: {len(seasons[(league, season)])} FotMob fixtures")
        matches = seasons[(league, season)]

        found, method = matching.match_fotmob_fixture(fixture, matches, crosswalk)
        links.append(link_row(fixture, league, season, found, method))

        season_teams = sorted({matching._team(m, side) for m in matches for side in ("home", "away")} - {""})
        for name in (fixture.get("home_team", ""), fixture.get("away_team", "")):
            if (league, season, name) not in teams:
                resolved, team_method = matching.resolve_team(name, season_teams, crosswalk)
                teams[(league, season, name)] = {"league": league, "season": season, "polymarket_name": name,
                                                 "fotmob_team": resolved, "method": team_method}

    table = drop_stale_listings(pd.DataFrame(links))
    table.to_csv(LINKS_PATH, index=False)
    pd.DataFrame(teams.values()).sort_values(["method", "league", "season", "polymarket_name"]).to_csv(
        TEAMS_PATH, index=False)

    linked = table["fotmob_match_id"].notna()
    print(f"\nlinked {linked.sum():,} of {len(table):,} fixtures ({linked.mean():.1%}); "
          f"{table['rescheduled'].sum()} of them rescheduled\n")
    print(pd.crosstab(table["league"], table["match_method"], margins=True).to_string())
    print("\nrescheduled:")
    print(table.loc[table["rescheduled"], ["league", "home_team", "away_team", "polymarket_kickoff_utc",
                                           "fotmob_kickoff_utc", "kickoff_diff_hours"]].to_string(index=False))
    print("\nnot linked:")
    print(table.loc[~linked, ["league", "polymarket_kickoff_utc", "home_team", "away_team",
                              "match_method"]].to_string(index=False))
    print(f"\nwrote {LINKS_PATH}\nwrote {TEAMS_PATH}")


if __name__ == "__main__":
    main()