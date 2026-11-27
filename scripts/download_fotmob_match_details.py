"""Step 1b: download FotMob match details for every linked fixture.

Reads data/processed/fixture_links.csv and, for each linked and finished game,
fetches FotMob's matchDetails with the project's existing fetch function
(soccer_goal_response_pilot.fetch_match_details). Each response is cached as
data/raw/fotmob_match_details/<fotmob_match_id>.json, so a rerun only
downloads what is missing and an interrupted run can simply be started again.

Writes data/processed/fotmob_match_details_manifest.csv: one row per linked
game saying whether its details are on disk, where, and whether FotMob calls
the match finished.

    python scripts/download_fotmob_match_details.py --limit 20   # small test
    python scripts/download_fotmob_match_details.py              # everything
"""
from __future__ import annotations

import argparse
import time

import pandas as pd

if __package__:
    from . import soccer_goal_response_pilot as pilot
else:
    import soccer_goal_response_pilot as pilot

LINKS_PATH = pilot.ROOT / "data" / "processed" / "fixture_links.csv"
MANIFEST_PATH = pilot.ROOT / "data" / "processed" / "fotmob_match_details_manifest.csv"
DETAILS_DIR = pilot.RAW / "fotmob_match_details"
DELAY_SECONDS = 0.3   # pause after each real download, to be polite to FotMob


def fetch_one(match_id: str) -> dict:
    """Make sure one match's details are on disk; report what happened."""
    already_cached = (DETAILS_DIR / f"{match_id}.json").exists()
    try:
        details, raw_ref = pilot.fetch_match_details(match_id, refresh=False)
    except Exception as error:
        return {"status": "error", "raw_ref": None, "details_finished": None,
                "error": f"{type(error).__name__}: {error}"}
    if not already_cached:
        time.sleep(DELAY_SECONDS)
    status = (details.get("header") or {}).get("status") or {}
    return {"status": "cached" if already_cached else "downloaded", "raw_ref": raw_ref,
            "details_finished": status.get("finished"), "error": None}



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="only the first N games (for testing)")
    args = parser.parse_args()

    links = pd.read_csv(LINKS_PATH, dtype={"fixture_id": str, "fotmob_match_id": str})
    todo = links[links["fotmob_match_id"].notna() & links["fotmob_finished"].eq(True)]
    if args.limit:
        todo = todo.head(args.limit)
    print(f"{len(todo):,} linked, finished games to check")

    rows = []
    for number, link in enumerate(todo.to_dict("records"), start=1):
        result = fetch_one(link["fotmob_match_id"])
        rows.append({"fixture_id": link["fixture_id"], "fotmob_match_id": link["fotmob_match_id"], **result})
        if number % 100 == 0 or number == len(todo):
            print(f"  {number:,}/{len(todo):,}")

    manifest = pd.DataFrame(rows)
    manifest.to_csv(MANIFEST_PATH, index=False)
    print("\n" + manifest["status"].value_counts().to_string())
    errors = manifest[manifest["status"].eq("error")]
    if not errors.empty:
        print(f"\n{len(errors)} errors (rerun the script to retry them):")
        print(errors[["fixture_id", "fotmob_match_id", "error"]].head(10).to_string(index=False))
    print(f"\nwrote {MANIFEST_PATH}")


if __name__ == "__main__":
    main()