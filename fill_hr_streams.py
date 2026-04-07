#!/usr/bin/env python3
"""
fill_hr_streams.py — Backfill per-second HR streams for all cached rides.

Scans .cache/detail_*.json for activity IDs, subtracts already-cached HR streams,
and fetches the remainder in batches to stay within Strava's rate limits.

Usage:
    python fill_hr_streams.py              # fetch up to 80 uncached streams
    python fill_hr_streams.py --batch=30   # smaller batch
    python fill_hr_streams.py --dry-run    # show what would be fetched
"""

import os
import glob
import time
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import strava_client

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def _all_activity_ids():
    """Collect activity IDs that have a cached power stream (skips commutes/no-power rides)."""
    ids = set()
    for path in glob.glob(os.path.join(CACHE_DIR, "stream_*.json")):
        basename = os.path.basename(path)
        try:
            ids.add(int(basename.replace("stream_", "").replace(".json", "")))
        except ValueError:
            pass
    return ids


def _cached_hr_ids():
    """Return set of activity IDs that already have a cached HR stream."""
    ids = set()
    for path in glob.glob(os.path.join(CACHE_DIR, "hr_*.json")):
        basename = os.path.basename(path)
        try:
            ids.add(int(basename.replace("hr_", "").replace(".json", "")))
        except ValueError:
            pass
    return ids


def main():
    parser = argparse.ArgumentParser(description="Backfill HR streams for cached rides")
    parser.add_argument("--batch", type=int, default=80,
                        help="Max streams to fetch per run (default: 80)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be fetched without making API calls")
    args = parser.parse_args()

    strava_client.refresh_access_token()

    all_ids = _all_activity_ids()
    cached_ids = _cached_hr_ids()
    needed = sorted(all_ids - cached_ids)
    total = len(all_ids)
    already_done = len(cached_ids & all_ids)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] HR stream cache: {already_done}/{total} activities cached")

    if not needed:
        print(f"All caught up ({already_done}/{total} cached). Nothing to do.")
        return

    batch = needed[:args.batch]
    print(f"Fetching {len(batch)} of {len(needed)} remaining "
          f"(batch={args.batch}, dry_run={args.dry_run})")

    if args.dry_run:
        for aid in batch:
            print(f"  would fetch: {aid}")
        return

    fetched = skipped = no_monitor = 0
    for i, aid in enumerate(batch):
        result = strava_client.fetch_hr_stream(aid)
        if result is None:
            # Check if it was rate-limited (no cache file written) or no monitor ([] cached)
            cache_path = os.path.join(CACHE_DIR, f"hr_{aid}.json")
            if os.path.exists(cache_path):
                no_monitor += 1
                print(f"  [{i+1}/{len(batch)}] {aid} — no HR monitor")
            else:
                skipped += 1
                print(f"  [{i+1}/{len(batch)}] {aid} — rate limited, skipped")
        else:
            fetched += 1
            print(f"  [{i+1}/{len(batch)}] {aid} — {len(result)}s of HR data")
        time.sleep(1)

    remaining = len(needed) - fetched - no_monitor
    print(f"Done. Fetched {fetched}, no monitor {no_monitor}, skipped {skipped}. "
          f"~{max(0, remaining)} streams still remaining.")


if __name__ == "__main__":
    main()
