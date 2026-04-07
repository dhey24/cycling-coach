"""
segment_scout.py — Standalone Tier 3 segment discovery tool.

Tiles the home area with Strava Explore API to find KOM-worthy segments
David may never have ridden. Applies hard filter + profile-learned grade range.

Usage:
    python segment_scout.py [--min-achievability=5] [--limit=20]

Output:
    Prints ranked table to stdout.
    Saves full results to data/segment_candidates.json.
"""

import os
import sys
import json
import argparse
from datetime import date
from dotenv import load_dotenv

load_dotenv()

import strava_client
import metrics

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _estimate_duration_s(seg):
    """Rough elapsed time estimate from distance + grade (no David effort needed)."""
    dist_m = seg.get("distance", 0)
    grade = seg.get("avg_grade", 0)
    if dist_m <= 0:
        return 9999
    # Assume 5–12 km/h depending on grade
    speed_ms = max(1.4, 3.3 - grade * 0.15)
    return dist_m / speed_ms


def main():
    parser = argparse.ArgumentParser(description="Cycling segment discovery scout")
    parser.add_argument("--min-achievability", type=int, default=5,
                        help="Minimum achievability score (1-10) to include (default: 5)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max segments to display (default: 20)")
    args = parser.parse_args()

    strava_client.refresh_access_token()

    # Load home coordinates from preferences.md
    home_lat, home_lng = metrics.load_home_coords()
    if not home_lat or not home_lng:
        print("ERROR: HOME_LAT and HOME_LNG not set in preferences.md")
        sys.exit(1)
    print(f"Home: {home_lat}, {home_lng}")

    ftp_outdoor, _ = metrics.load_ftps()

    # Fetch starred segments for profile learning
    print("Fetching starred segments...")
    starred = strava_client.fetch_starred_segments()
    print(f"Starred: {len(starred)}")

    profile = metrics.infer_climb_profile(starred)
    if profile:
        print(f"Climb profile from stars: grade {profile['grade_lo']}–{profile['grade_hi']}%, "
              f"dist {profile['dist_lo']}–{profile['dist_hi']}m")
    else:
        print("Not enough starred segments for profile inference; using grade >= 5% only.")

    # Discover nearby segments via Explore API (tiles, cached 30d)
    print(f"\nScanning {home_lat:.3f}, {home_lng:.3f} ±15km...")
    nearby = strava_client.fetch_nearby_segments(home_lat, home_lng, radius_km=15)
    print(f"Segments found (after hard grade filter): {len(nearby)}")

    # Apply profile grade filter if available
    if profile:
        nearby = [s for s in nearby
                  if profile["grade_lo"] <= s.get("avg_grade", 0) <= profile["grade_hi"]]
        print(f"After profile grade filter ({profile['grade_lo']}–{profile['grade_hi']}%): {len(nearby)}")

    # Duration filter using estimate (60–600s)
    nearby = [s for s in nearby if 60 <= _estimate_duration_s(s) <= 600]
    print(f"After duration filter (60–600s estimate): {len(nearby)}")

    # Fetch leaderboards (cap at 20 new API calls)
    print(f"\nFetching leaderboards (cap: 20 calls)...")
    candidates = []
    fetches = 0

    for seg in nearby:
        if fetches >= 20:
            break
        seg_id = seg.get("id")
        if not seg_id:
            continue

        lb = strava_client.fetch_segment_leaderboard(seg_id)
        fetches += 1

        entries = lb.get("entries", [])
        total = lb.get("total_athletes", 0)
        athlete_entry = lb.get("athlete_entry")

        # Use KOM time for a better duration check
        if entries:
            kom_time = min(e.get("elapsed_time", 9999) for e in entries)
            if not (60 <= kom_time <= 600):
                continue
        else:
            kom_time = round(_estimate_duration_s(seg))

        top10_entry = min(entries, key=lambda e: e.get("elapsed_time", 9999)) if entries else None
        top10_time = top10_entry.get("elapsed_time") if top10_entry else None

        rank = athlete_entry.get("rank") if athlete_entry else None

        # Achievability: conservative estimate without David's actual effort
        achievability = metrics.estimate_achievability(None, None, total)

        candidates.append({
            "segment_id":              seg_id,
            "segment_name":            seg.get("name", ""),
            "avg_grade":               seg.get("avg_grade", 0),
            "distance_m":              seg.get("distance", 0),
            "elapsed_time_estimate_s": round(_estimate_duration_s(seg)),
            "kom_time_s":              top10_time,
            "rank":                    rank,
            "total_athletes":          total,
            "achievability":           achievability,
            "tier":                    3,
        })

    candidates.sort(key=lambda s: (s["achievability"], s.get("total_athletes", 0)), reverse=True)

    filtered = [c for c in candidates if c["achievability"] >= args.min_achievability]
    limited  = filtered[:args.limit]

    # Print table
    print(f"\n{'Segment':<35} {'Grade':>6} {'Dist':>6} {'KOM':>6} {'Rank':>10} {'Score':>6}")
    print("─" * 73)
    for c in limited:
        km       = round(c["distance_m"] / 1000, 1)
        kom_str  = _fmt_elapsed(c.get("kom_time_s"))
        rank_str = (f"{c['rank']}/{c['total_athletes']}"
                    if c.get("rank") else f"—/{c['total_athletes']}")
        print(f"{c['segment_name'][:34]:<35} {c['avg_grade']:>5.1f}% "
              f"{km:>5.1f}km {kom_str:>6} {rank_str:>10} {c['achievability']:>5}/10")

    print(f"\n{len(limited)} segments shown (min achievability: {args.min_achievability})")

    # Save full results
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "segment_candidates.json")
    with open(out_path, "w") as f:
        json.dump({
            "generated":  date.today().isoformat(),
            "home_lat":   home_lat,
            "home_lng":   home_lng,
            "candidates": candidates,
        }, f, indent=2)
    print(f"Saved {len(candidates)} candidates → {out_path}")


def _fmt_elapsed(seconds):
    if not seconds:
        return "—"
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


if __name__ == "__main__":
    main()
