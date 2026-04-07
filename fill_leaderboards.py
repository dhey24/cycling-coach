#!/usr/bin/env python3
"""
fill_leaderboards.py — Drains the seg_stats cache backlog in rate-limit-safe batches.

Scans ride detail cache for climb segment IDs, subtracts already-cached leaderboards,
and fetches the remainder in small batches to stay within Strava's 200 req/15min limit.

Usage:
    python fill_leaderboards.py              # fetch up to 80 uncached segments
    python fill_leaderboards.py --batch=50   # smaller batch
    python fill_leaderboards.py --dry-run    # show what would be fetched, no API calls
"""

import os
import sys
import json
import glob
import time
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import strava_client

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def _collect_climb_segment_ids():
    """Scan all cached ride details for segment IDs matching the climb filter."""
    seg_ids = set()
    for path in glob.glob(os.path.join(CACHE_DIR, "detail_*.json")):
        with open(path) as f:
            d = json.load(f)
        for effort in d.get("segment_efforts", []):
            seg = effort.get("segment", {})
            sid = seg.get("id")
            grade = seg.get("average_grade", 0)
            elapsed = effort.get("elapsed_time", 0)
            if sid and grade >= 5 and 60 <= elapsed <= 600:
                seg_ids.add(sid)
    return seg_ids


def _cached_seg_stat_ids():
    """Return set of segment IDs that already have a seg_stats cache file."""
    ids = set()
    for path in glob.glob(os.path.join(CACHE_DIR, "seg_stats_*.json")):
        basename = os.path.basename(path)
        try:
            ids.add(int(basename.replace("seg_stats_", "").replace(".json", "")))
        except ValueError:
            pass
    return ids


def _fmt_kom(result):
    kom = result.get("kom_time_s")
    if not kom:
        return "no KOM"
    m, s = divmod(int(kom), 60)
    return f"KOM {m}:{s:02d}"


def main():
    parser = argparse.ArgumentParser(description="Fill seg_stats leaderboard cache")
    parser.add_argument("--batch", type=int, default=80,
                        help="Max segments to fetch per run (default: 80)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be fetched without making API calls")
    args = parser.parse_args()

    strava_client.refresh_access_token()

    climb_ids = _collect_climb_segment_ids()
    cached_ids = _cached_seg_stat_ids()
    needed = sorted(climb_ids - cached_ids)
    total_climb = len(climb_ids)
    total_cached = len(cached_ids & climb_ids)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Leaderboard cache: {total_cached}/{total_climb} climb segments cached")

    if not needed:
        print(f"All caught up ({total_cached}/{total_climb} cached). Nothing to do.")
        return

    batch = needed[:args.batch]
    print(f"Fetching {len(batch)} of {len(needed)} remaining segments "
          f"(batch={args.batch}, dry_run={args.dry_run})")

    if args.dry_run:
        for seg_id in batch:
            print(f"  would fetch: {seg_id}")
        return

    fetched = 0
    skipped = 0
    for i, seg_id in enumerate(batch):
        result = strava_client.fetch_segment_leaderboard(seg_id)
        if result:
            fetched += 1
            print(f"  [{i+1}/{len(batch)}] {seg_id} — {_fmt_kom(result)}")
        else:
            skipped += 1
            print(f"  [{i+1}/{len(batch)}] {seg_id} — skipped (rate limited or not found)")
        time.sleep(1)  # extra gap beyond strava_client's internal 0.5s delay

    remaining_after = len(needed) - fetched
    print(f"Done. Fetched {fetched}, skipped {skipped}. "
          f"~{remaining_after} segments still remaining.")


if __name__ == "__main__":
    main()
