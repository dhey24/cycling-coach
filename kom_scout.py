#!/usr/bin/env python3
"""
kom_scout.py — One-time KOM scouting report.

Surfaces all tracked targets (uncapped), famous SF/Marin segments with
competition signals, and Tier 3 undiscovered candidates.

Usage:
    python kom_scout.py                        # full report
    python kom_scout.py --backfill             # warm cache + populate segment_history DB
    python kom_scout.py --min-efforts=3        # filter by effort count
    python kom_scout.py --keyword="twin peaks" # filter by name
    python kom_scout.py --min-grade=7 --max-radius=20

Opens:  /tmp/kom_scout.html
Saves:  data/reports/kom_scout_YYYY-MM-DD.html
Saves:  data/reports/kom_scout_YYYY-MM-DD.json
"""

import os
import json
import time
import argparse
import webbrowser
from datetime import date
from glob import glob

from dotenv import load_dotenv
load_dotenv()

import strava_client
import metrics
import db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "reports")
CANDIDATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "segment_candidates.json")
STREAM_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")

# Known iconic SF/Marin segment IDs (community-famous seed list)
SEED_SEGMENT_IDS = [
    229781,   # Hawk Hill — 72k+ athletes, Strava's most iconic climb
]

# Signal 3 threshold: segments with this many athletes are community landmarks
HIGH_TRAFFIC_THRESHOLD = 2000


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _fmt_time(seconds):
    """Format seconds as M:SS or H:MM:SS."""
    if seconds is None:
        return "—"
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _fmt_gap(gap_s):
    if gap_s is None:
        return "—"
    if gap_s <= 0:
        return "AT KOM"
    return f"+{_fmt_time(gap_s)}"


def _power_pct_color(pct):
    """Color for +% power metric: green ≤10%, amber 11–25%, red >25%."""
    if pct is None:
        return "#6b7280"
    if pct <= 0:
        return "#22c55e"
    if pct <= 10:
        return "#22c55e"
    if pct <= 25:
        return "#f59e0b"
    return "#ef4444"


def _phenotype_match(kom_time_s, phenotype):
    """
    Return 'ideal' | 'ok' | 'mismatch' | None based on segment KOM duration vs athlete phenotype.
    'ideal' = within the phenotype's best range; 'ok' = within 60-150% of range endpoints.
    """
    if not kom_time_s or not phenotype or phenotype.get("primary") in ("unknown", "all-rounder"):
        return None
    lo, hi = phenotype.get("best_kom_duration_range_s", [0, 9999])
    if lo <= kom_time_s <= hi:
        return "ideal"
    if lo * 0.6 <= kom_time_s <= hi * 1.5:
        return "ok"
    return "mismatch"


def _compute_athlete_percentiles():
    """Load all cached seg_stats and compute p25/p75/p90 of total_athletes."""
    counts = []
    for f in glob(os.path.join(STREAM_CACHE_DIR, "seg_stats_*.json")):
        with open(f) as fh:
            n = json.load(fh).get("data", {}).get("total_athletes", 0)
        if n:
            counts.append(n)
    counts.sort()
    if len(counts) < 2:
        return 1000, 10000, 50000  # fallback if no cache
    def pct(data, p):
        idx = (p / 100) * (len(data) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(data) - 1)
        return data[lo] + (data[hi] - data[lo]) * (idx - lo)
    return pct(counts, 25), pct(counts, 75), pct(counts, 90)


def _competition_badge(total_athletes, p25, p75, p90):
    """Return (label, color) based on percentile cutoffs computed from local cache."""
    if not total_athletes:
        return "Unknown", "#6b7280"
    if total_athletes > p90:
        return "World-class", "#ef4444"
    if total_athletes > p75:
        return "Elite", "#f97316"
    if total_athletes > p25:
        return "Competitive", "#f59e0b"
    return "Local", "#6b7280"


# ---------------------------------------------------------------------------
# Full uncapped tier builder
# ---------------------------------------------------------------------------

def _build_full_tracker(starred, ride_segs, fetch_leaderboard_fn, curve_data,
                        home_lat, home_lng, landmark_ids=None, leaderboard_cap=40):
    """
    Like metrics.build_segment_tracker but with no :5/:1 cap.
    Returns (tier1_list, tier2_list) sorted by pct_power_increase asc.
    landmark_ids: set of segment IDs that are community-famous (gets ★ badge).
    """
    landmark_ids = landmark_ids or set()
    weight_kg = metrics.load_rider_weight()

    def _is_local(seg):
        if not home_lat or not home_lng:
            return True
        latlng = seg.get("start_latlng")
        if not latlng or len(latlng) < 2:
            return True
        return metrics.haversine_km(home_lat, home_lng, latlng[0], latlng[1]) <= 80

    starred_ids = {s["id"] for s in (starred or []) if _is_local(s)}

    tier1_ids = set(starred_ids)
    tier2_ids = set()
    for seg_id, entry in (ride_segs or {}).items():
        if entry["effort_count"] >= 3:
            tier1_ids.add(seg_id)
        elif 1 <= entry["effort_count"] <= 2:
            tier2_ids.add(seg_id)
    tier2_ids -= tier1_ids

    fetches = 0
    tier1_result = []
    tier2_result = []

    # Process cached tier1 segments first so cap doesn't block free cache reads
    def _is_cached(seg_id):
        return os.path.exists(os.path.join(STREAM_CACHE_DIR, f"seg_stats_{seg_id}.json"))

    tier1_ordered = sorted(tier1_ids, key=lambda sid: (0 if _is_cached(sid) else 1, sid))

    for seg_id in tier1_ordered:
        ride_entry = (ride_segs or {}).get(seg_id, {})
        starred_info = next((s for s in (starred or []) if s["id"] == seg_id), {})
        seg_name = ride_entry.get("segment_name") or starred_info.get("name", f"Segment {seg_id}")
        avg_grade = ride_entry.get("avg_grade") or starred_info.get("avg_grade", 0)
        distance_m = ride_entry.get("distance_m") or starred_info.get("distance", 0)
        start_latlng = starred_info.get("start_latlng") or ride_entry.get("start_latlng")

        # Check cache first (free); only count uncached fetches against cap
        stats = {}
        cache_path = os.path.join(STREAM_CACHE_DIR, f"seg_stats_{seg_id}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cached_data = json.load(f)
            stats = cached_data.get("data", {})
        elif fetches < leaderboard_cap:
            stats = fetch_leaderboard_fn(seg_id) or {}
            fetches += 1

        pr_time = stats.get("pr_elapsed_time")
        kom_time_s = stats.get("kom_time_s")
        total_athletes = stats.get("total_athletes", 0)
        david_rank = stats.get("david_rank") or ride_entry.get("best_kom_rank")

        best_effort = ride_entry.get("best_effort") or {}
        ride_elapsed = best_effort.get("elapsed_time_s")
        avg_watts = best_effort.get("avg_watts")
        elapsed_time_s = ride_elapsed or pr_time

        time_gap_s = (elapsed_time_s - kom_time_s) if elapsed_time_s and kom_time_s else None

        power_gap_w = None
        if elapsed_time_s and kom_time_s and elapsed_time_s > 0:
            w_ref = avg_watts or metrics._interpolate_power(elapsed_time_s, curve_data)
            if w_ref:
                power_gap_w = max(0, round(w_ref * (elapsed_time_s / max(kom_time_s, 1)) - w_ref))

        pct_power_increase, power_tier = metrics.compute_power_metric(
            elapsed_time_s, avg_watts, kom_time_s,
            avg_grade, distance_m, curve_data, weight_kg
        )

        tier1_result.append({
            "segment_id":         seg_id,
            "segment_name":       seg_name,
            "avg_grade":          avg_grade,
            "elapsed_time_s":     elapsed_time_s,
            "avg_watts":          avg_watts,
            "kom_time_s":         kom_time_s,
            "time_gap_s":         time_gap_s,
            "power_gap_w":        power_gap_w,
            "david_rank":         david_rank,
            "total_athletes":     total_athletes,
            "tier":               1,
            "pct_power_increase": pct_power_increase,
            "power_tier":         power_tier,
            "effort_count":       ride_entry.get("effort_count", 0),
            "is_famous":          seg_id in landmark_ids,
            "start_latlng":       start_latlng,
        })

    for seg_id in tier2_ids:
        ride_entry = (ride_segs or {}).get(seg_id, {})
        starred_info_t2 = next((s for s in (starred or []) if s["id"] == seg_id), {})
        start_latlng_t2 = starred_info_t2.get("start_latlng") or ride_entry.get("start_latlng")

        # Prefer cached stats; only fetch if uncached and under cap
        stats = {}
        cache_path = os.path.join(STREAM_CACHE_DIR, f"seg_stats_{seg_id}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cached = json.load(f)
            stats = cached.get("data", {})
        elif fetches < leaderboard_cap:
            stats = fetch_leaderboard_fn(seg_id) or {}
            fetches += 1

        total_athletes = stats.get("total_athletes", 100)
        best_effort = ride_entry.get("best_effort") or {}
        elapsed_time_s = best_effort.get("elapsed_time_s")
        avg_watts = best_effort.get("avg_watts")
        avg_grade = ride_entry.get("avg_grade")
        distance_m = ride_entry.get("distance_m", 0)
        kom_time_s = stats.get("kom_time_s")
        time_gap_s = (elapsed_time_s - kom_time_s) if elapsed_time_s and kom_time_s else None

        power_gap_w = None
        if elapsed_time_s and kom_time_s and elapsed_time_s > 0:
            w_ref = avg_watts or metrics._interpolate_power(elapsed_time_s, curve_data)
            if w_ref:
                power_gap_w = max(0, round(w_ref * (elapsed_time_s / max(kom_time_s, 1)) - w_ref))

        pct_power_increase, power_tier = metrics.compute_power_metric(
            elapsed_time_s, avg_watts, kom_time_s,
            avg_grade, distance_m, curve_data, weight_kg
        )

        tier2_result.append({
            "segment_id":         seg_id,
            "segment_name":       ride_entry.get("segment_name", f"Segment {seg_id}"),
            "avg_grade":          avg_grade,
            "elapsed_time_s":     elapsed_time_s,
            "avg_watts":          avg_watts,
            "kom_time_s":         kom_time_s,
            "time_gap_s":         time_gap_s,
            "power_gap_w":        power_gap_w,
            "total_athletes":     total_athletes,
            "tier":               2,
            "pct_power_increase": pct_power_increase,
            "power_tier":         power_tier,
            "effort_count":       ride_entry.get("effort_count", 0),
            "is_famous":          seg_id in landmark_ids,
            "start_latlng":       start_latlng_t2,
        })

    tier1_result.sort(key=lambda s: s["pct_power_increase"] if s["pct_power_increase"] is not None else 9999)
    tier2_result.sort(key=lambda s: s["pct_power_increase"] if s["pct_power_increase"] is not None else 9999)
    return tier1_result, tier2_result


# ---------------------------------------------------------------------------
# Landmark discovery
# ---------------------------------------------------------------------------

def _discover_landmarks(starred, ride_segs, home_lat, home_lng):
    """
    3-signal landmark discovery. Returns set of segment IDs.

    Signal 1: hardcoded seed IDs (community-famous segments)
    Signal 2: Strava Explore API (Cat 1+ climbs near home)
    Signal 3: high-traffic filter from starred + ride history (total_athletes > threshold)
    """
    landmark_ids = set(SEED_SEGMENT_IDS)

    # Signal 2: Explore API tiles
    try:
        nearby = strava_client.fetch_nearby_segments(home_lat, home_lng, radius_km=25)
        for seg in nearby:
            if seg.get("climb_category", 0) >= 1:
                landmark_ids.add(seg["id"])
    except Exception as e:
        print(f"  Explore API warning: {e}")

    # Signal 3: high-traffic from starred segments (check cached leaderboard data)
    for seg in (starred or []):
        seg_id = seg.get("id")
        if not seg_id:
            continue
        cache_path = os.path.join(STREAM_CACHE_DIR, f"seg_stats_{seg_id}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cached = json.load(f)
            total = cached.get("data", {}).get("total_athletes", 0)
            if total >= HIGH_TRAFFIC_THRESHOLD:
                landmark_ids.add(seg_id)

    # Signal 3: high-traffic from ride segments
    for seg_id in (ride_segs or {}):
        cache_path = os.path.join(STREAM_CACHE_DIR, f"seg_stats_{seg_id}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cached = json.load(f)
            total = cached.get("data", {}).get("total_athletes", 0)
            if total >= HIGH_TRAFFIC_THRESHOLD:
                landmark_ids.add(seg_id)

    return landmark_ids


def _enrich_landmarks(landmark_ids, fetch_leaderboard_fn,
                      ride_segs, curve_data, ftp_outdoor, starred, leaderboard_cap=20,
                      weight_kg=77):
    """Fetch and enrich each landmark with leaderboard data."""
    results = []
    starred_ids = {s["id"] for s in (starred or [])}
    starred_by_id = {s["id"]: s for s in (starred or [])}

    # Sort: cached segments first (free), uncached last (costs API calls)
    def _is_cached(seg_id):
        return os.path.exists(os.path.join(STREAM_CACHE_DIR, f"seg_stats_{seg_id}.json"))

    ordered = sorted(landmark_ids, key=lambda sid: (0 if _is_cached(sid) else 1, sid))
    fetches = 0

    for seg_id in ordered:
        cached = _is_cached(seg_id)
        if not cached and fetches >= leaderboard_cap:
            continue  # skip uncached if cap reached
        if not cached:
            fetches += 1
        stats = fetch_leaderboard_fn(seg_id) or {}

        kom_time_s = stats.get("kom_time_s")
        total_athletes = stats.get("total_athletes", 0)
        effort_count = stats.get("effort_count", 0)
        pr_time = stats.get("pr_elapsed_time")

        ride_entry = (ride_segs or {}).get(seg_id, {})
        best_effort = ride_entry.get("best_effort") or {}
        ride_elapsed = best_effort.get("elapsed_time_s")
        elapsed_time_s = ride_elapsed or pr_time
        never_ridden = elapsed_time_s is None

        time_gap_s = (elapsed_time_s - kom_time_s) if elapsed_time_s and kom_time_s else None

        # What power David can hold at KOM duration per his curve
        david_curve_watts = metrics._interpolate_power(kom_time_s, curve_data) if kom_time_s else None

        power_gap_w = None
        if elapsed_time_s and kom_time_s and elapsed_time_s > 0:
            w_ref = best_effort.get("avg_watts") or metrics._interpolate_power(elapsed_time_s, curve_data)
            if w_ref:
                power_gap_w = max(0, round(w_ref * (elapsed_time_s / max(kom_time_s, 1)) - w_ref))

        seg_name = ride_entry.get("segment_name")
        if not seg_name:
            seg_name = starred_by_id.get(seg_id, {}).get("name", f"Segment {seg_id}")

        avg_grade = ride_entry.get("avg_grade") or starred_by_id.get(seg_id, {}).get("avg_grade", 0)
        distance_m = ride_entry.get("distance_m") or starred_by_id.get(seg_id, {}).get("distance", 0)

        pct_power_increase, power_tier = metrics.compute_power_metric(
            elapsed_time_s, best_effort.get("avg_watts"), kom_time_s,
            avg_grade, distance_m, curve_data, weight_kg
        )

        results.append({
            "segment_id":         seg_id,
            "segment_name":       seg_name,
            "avg_grade":          avg_grade,
            "total_athletes":     total_athletes,
            "effort_count":       effort_count,
            "kom_time_s":         kom_time_s,
            "elapsed_time_s":     elapsed_time_s,
            "time_gap_s":         time_gap_s,
            "david_curve_watts":  david_curve_watts,
            "power_gap_w":        power_gap_w,
            "pct_power_increase": pct_power_increase,
            "power_tier":         power_tier,
            "never_ridden":       never_ridden,
            "is_starred":         seg_id in starred_ids,
            "effort_count_david": ride_entry.get("effort_count", 0),
        })

    # Most famous first (total_athletes desc), then achievability
    results.sort(key=lambda s: (-(s["total_athletes"] or 0),
                                s["pct_power_increase"] if s["pct_power_increase"] is not None else 9999))
    return results


# ---------------------------------------------------------------------------
# Executive summary builder
# ---------------------------------------------------------------------------

def _build_exec_summary(tier1, tier2, landmarks, curve_data, ftp_outdoor):
    # Exclude segments already in top 10 — David is aware of those, not useful as targets
    not_top10 = [s for s in tier1 if not s.get("david_rank")]

    # A: Near-miss targets — tier1 with valid KOM and pct_power_increase <= 35%
    near_miss = sorted(
        [s for s in not_top10
         if s.get("kom_time_s")
         and s.get("pct_power_increase") is not None
         and s["pct_power_increase"] <= 35],
        key=lambda s: s.get("time_gap_s") or 9999
    )[:3]

    # B: Prime targets — pct_power_increase <= 20% (highly achievable)
    prime = [s for s in not_top10
             if s.get("pct_power_increase") is not None
             and s["pct_power_increase"] <= 20
             and s.get("kom_time_s")][:3]

    # C: Famous context — landmarks you've ridden, sorted by total_athletes desc
    famous_ridden = sorted(
        [lm for lm in landmarks if not lm.get("never_ridden") and lm.get("kom_time_s")],
        key=lambda s: -(s.get("total_athletes") or 0)
    )[:3]

    return {"near_miss": near_miss, "prime": prime, "famous_context": famous_ridden}


# ---------------------------------------------------------------------------
# Tier 3 loader
# ---------------------------------------------------------------------------

def _load_tier3_candidates():
    """Returns list if file exists, None if not run yet."""
    if not os.path.exists(CANDIDATES_PATH):
        return None
    with open(CANDIDATES_PATH) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("candidates", [])


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------

def _save_json(tier1, tier2, landmarks, tier3, curve_data, ftp_outdoor, ftp_indoor):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = date.today().isoformat()
    path = os.path.join(REPORTS_DIR, f"kom_scout_{today}.json")
    with open(path, "w") as f:
        json.dump({
            "generated":   today,
            "ftp_outdoor": ftp_outdoor,
            "ftp_indoor":  ftp_indoor,
            "power_curve": curve_data,
            "tier1":       tier1,
            "tier2":       tier2,
            "landmarks":   landmarks,
            "tier3":       tier3,
        }, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #0f0f0f; color: #e5e7eb; font-size: 14px; line-height: 1.5;
}
.container { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 28px; font-weight: 800; color: #f9fafb; margin-bottom: 4px; }
.subtitle { color: #9ca3af; margin-bottom: 16px; font-size: 13px; }

.header-stats { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 32px; }
.stat-pill {
  background: #1f2937; border: 1px solid #374151; border-radius: 20px;
  padding: 4px 14px; font-size: 12px; color: #d1d5db;
}
.stat-pill strong { color: #f9fafb; }

section { margin-bottom: 48px; }
h2 {
  font-size: 20px; font-weight: 700; color: #f3f4f6; margin-bottom: 6px;
  border-bottom: 1px solid #374151; padding-bottom: 8px;
}
.section-desc { color: #9ca3af; font-size: 13px; margin-bottom: 16px; }

/* Executive summary */
.exec-summary {
  background: #111827; border: 1px solid #374151; border-radius: 12px;
  padding: 20px; display: flex; flex-direction: column; gap: 16px;
}
.exec-block {}
.exec-label {
  font-size: 10px; color: #6b7280; text-transform: uppercase;
  letter-spacing: 0.8px; font-weight: 600; margin-bottom: 4px;
}
.exec-text { color: #d1d5db; font-size: 14px; line-height: 1.6; }
.exec-text strong { color: #f9fafb; }

/* Tables */
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; padding: 8px 10px; color: #6b7280; font-weight: 600;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
  border-bottom: 1px solid #374151; white-space: nowrap;
}
td { padding: 8px 10px; border-bottom: 1px solid #1f2937; vertical-align: middle; }
tr:hover td { background: #1a2332; }
.seg-link { color: #e5e7eb; text-decoration: none; font-weight: 500; }
.seg-link:hover { color: #fc4c02; }
.strava-link-small { color: #fc4c02; font-size: 16px; text-decoration: none; }

.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-gray  { background: #374151; color: #9ca3af; }
.badge-gold  { background: #78350f; color: #fcd34d; }

/* Callout */
.callout {
  background: #1f2937; border: 1px solid #374151; border-left: 3px solid #f59e0b;
  border-radius: 8px; padding: 16px 20px; color: #9ca3af;
}
.callout code {
  background: #374151; padding: 2px 6px; border-radius: 4px;
  font-family: monospace; font-size: 12px; color: #f9fafb;
}

.footer {
  border-top: 1px solid #374151; padding-top: 16px; color: #6b7280;
  font-size: 12px; margin-top: 32px; line-height: 2;
}

/* Filter bar */
.filter-bar {
  position: sticky; top: 0; z-index: 100;
  background: #0f0f0f; border-bottom: 1px solid #374151;
  padding: 10px 16px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
}
.filter-group { display: flex; align-items: center; gap: 6px; }
.filter-group label { font-size: 11px; color: #9ca3af; white-space: nowrap; }
.filter-group input[type=number] {
  width: 60px; background: #1f2937; border: 1px solid #374151; border-radius: 6px;
  color: #f9fafb; font-size: 12px; padding: 3px 6px; text-align: center;
}
.filter-count { font-size: 11px; color: #6b7280; margin-left: auto; }
.filter-reset {
  font-size: 11px; color: #9ca3af; background: #1f2937; border: 1px solid #374151;
  border-radius: 6px; padding: 3px 10px; cursor: pointer;
}
.filter-reset:hover { color: #f9fafb; }
tr.filtered-out { display: none; }

@media (max-width: 600px) {
  h1 { font-size: 22px; }
  .filter-bar { gap: 8px; }
  .filter-group input[type=number] { width: 52px; }
}
"""


def _segment_row(seg, p25, p75, p90):
    seg_id = seg["segment_id"]
    url = f"https://www.strava.com/segments/{seg_id}"
    grade = seg.get("avg_grade")
    grade_str = f"{grade:.1f}%" if grade else "—"
    gap_s = seg.get("time_gap_s")
    gap_str = f'+{_fmt_time(gap_s)}' if gap_s and gap_s > 0 else "—"
    power_gap = seg.get("power_gap_w")
    power_str = f"+{power_gap}w" if power_gap else "—"

    pct = seg.get("pct_power_increase")
    power_tier = seg.get("power_tier")
    pct_color = _power_pct_color(pct)
    if pct is None:
        pct_str = "—"
    elif pct <= 0:
        pct_str = "KOM 🏆"
    else:
        prefix = "~" if power_tier in ("B", "C") else ""
        suffix = " PR" if power_tier == "B" else (" est" if power_tier == "C" else "")
        pct_str = f"{prefix}+{pct:.0f}%{suffix}"

    famous_badge = (' <span class="badge badge-gold">★ Famous</span>'
                    if seg.get("is_famous") else "")
    match = seg.get("phenotype_match")
    match_badge = (' <span class="badge" style="background:#14532d33;color:#4ade80;font-size:10px">✓ match</span>'
                   if match == "ideal" else "")

    comp_label, comp_color = _competition_badge(seg.get("total_athletes", 0), p25, p75, p90)
    comp_html = (f'<span class="badge" style="background:{comp_color}22;color:{comp_color}">'
                 f'{comp_label}</span>')

    david_rank = seg.get("david_rank")
    total_a = seg.get("total_athletes") or 0
    if david_rank and total_a:
        pct_val = round(david_rank / total_a * 100, 2)
        rank_str = f"#{david_rank} (top {pct_val}%)"
    else:
        rank_str = "—"

    # data-* attributes for JS filtering
    latlng = seg.get("start_latlng") or []
    lat_attr = f' data-lat="{latlng[0]}"' if len(latlng) >= 2 else ""
    lng_attr = f' data-lng="{latlng[1]}"' if len(latlng) >= 2 else ""
    data_attrs = (
        f' data-grade="{grade or 0}"'
        f' data-pct="{pct if pct is not None else 9999}"'
        f' data-athletes="{total_a}"'
        f' data-efforts="{seg.get("effort_count") or 0}"'
        f'{lat_attr}{lng_attr}'
    )

    return f"""
      <tr{data_attrs}>
        <td><a href="{url}" target="_blank" class="seg-link">{seg["segment_name"]}</a>{famous_badge}{match_badge}</td>
        <td>{grade_str}</td>
        <td>{seg.get("effort_count") or "—"}</td>
        <td>{_fmt_time(seg.get("elapsed_time_s"))}</td>
        <td>{_fmt_time(seg.get("kom_time_s"))}</td>
        <td>{gap_str}</td>
        <td>{power_str}</td>
        <td>{comp_html}</td>
        <td style="white-space:nowrap;font-size:12px">{rank_str}</td>
        <td style="color:{pct_color};font-weight:700">{pct_str}</td>
        <td><a href="{url}" target="_blank" class="strava-link-small">↗</a></td>
      </tr>"""


def _famous_unridden_row(lm, p25, p75, p90):
    seg_id = lm["segment_id"]
    url = f"https://www.strava.com/segments/{seg_id}"
    total_a = lm.get("total_athletes", 0)
    total_str = f"{total_a:,}" if total_a else "—"
    kom_str = _fmt_time(lm.get("kom_time_s"))

    comp_label, comp_color = _competition_badge(total_a, p25, p75, p90)
    comp_html = (f'<span class="badge" style="background:{comp_color}22;color:{comp_color}">'
                 f'{comp_label}</span>')

    return f"""
      <tr>
        <td><a href="{url}" target="_blank" class="seg-link">{lm["segment_name"]}</a></td>
        <td>{total_str}</td>
        <td>{kom_str}</td>
        <td>{comp_html}</td>
        <td><a href="{url}" target="_blank" class="strava-link-small">↗</a></td>
      </tr>"""


def _tier3_row(c):
    seg_id = c.get("segment_id", "")
    url = f"https://www.strava.com/segments/{seg_id}"
    grade = c.get("avg_grade")
    grade_str = f"{grade:.1f}%" if grade else "—"
    dist = c.get("distance_m", 0)
    dist_str = f"{dist/1000:.1f} km" if dist else "—"
    total_a = c.get("total_athletes", 0)
    total_str = f"{total_a:,}" if total_a else "—"
    # Support both new pct_power_increase and legacy achievability
    pct = c.get("pct_power_increase")
    if pct is not None:
        score_color = _power_pct_color(pct)
        score_str = f"+{pct:.0f}%" if pct > 0 else "KOM 🏆"
    else:
        ach = c.get("achievability", 5)
        score_color = ("#22c55e" if ach >= 7 else "#f59e0b" if ach >= 4 else "#ef4444")
        score_str = f"{ach}/10"
    return f"""
      <tr>
        <td><a href="{url}" target="_blank" class="seg-link">{c.get("segment_name", seg_id)}</a></td>
        <td>{grade_str}</td>
        <td>{dist_str}</td>
        <td>{total_str}</td>
        <td style="color:{score_color};font-weight:700">{score_str}</td>
      </tr>"""


TABLE_HEAD = """
      <thead><tr>
        <th>Segment</th><th>Grade</th><th>Efforts</th>
        <th>Your PR</th><th>KOM</th><th>Gap</th><th>+Watts</th>
        <th>Competition</th><th>Rank</th><th>+% Power</th><th>Strava</th>
      </tr></thead>"""


def _render_exec_summary(summary):
    prime = summary["prime"]
    near_miss = summary["near_miss"]
    famous = summary["famous_context"]

    parts = []

    if prime:
        top = prime[0]
        gap_str = _fmt_gap(top.get("time_gap_s"))
        pw = top.get("power_gap_w")
        pw_str = f", needs +{pw}w" if pw else ""
        n = len(prime)
        parts.append(
            f'<div class="exec-block">'
            f'<div class="exec-label">Prime Targets</div>'
            f'<div class="exec-text">You have <strong>{n}</strong> segment{"s" if n > 1 else ""} '
            f'where achievability ≥ 8. Top pick: <strong>{top["segment_name"]}</strong> — '
            f'{gap_str} back{pw_str}.</div>'
            f'</div>'
        )

    if near_miss:
        top = near_miss[0]
        gap_str = _fmt_gap(top.get("time_gap_s"))
        parts.append(
            f'<div class="exec-block">'
            f'<div class="exec-label">Near-Miss</div>'
            f'<div class="exec-text">Closest to KOM by time: '
            f'<strong>{top["segment_name"]}</strong> at {gap_str}. '
            f'One strong effort could flip this.</div>'
            f'</div>'
        )

    if famous:
        lm = famous[0]
        total_a = lm.get("total_athletes", 0)
        pr_str = _fmt_time(lm.get("elapsed_time_s"))
        kom_str = _fmt_time(lm.get("kom_time_s"))
        dcw = lm.get("david_curve_watts")
        dcw_str = f" Your curve projects ~{dcw}w at KOM duration." if dcw else ""
        parts.append(
            f'<div class="exec-block">'
            f'<div class="exec-label">Famous Benchmark</div>'
            f'<div class="exec-text">'
            f'<strong>{lm["segment_name"]}</strong> ({total_a:,} athletes): '
            f'your PR is {pr_str}, KOM is {kom_str}.{dcw_str}'
            f'</div>'
            f'</div>'
        )

    if not parts:
        return ""

    return f"""
    <section>
      <h2>Executive Summary</h2>
      <div class="exec-summary">{"".join(parts)}</div>
    </section>"""


def _render_html(tier1, tier2, landmarks, tier3, curve_data, ftp_outdoor, ftp_indoor, p25, p75, p90,
                 home_lat=None, home_lng=None, phenotype=None):
    today = date.today().isoformat()

    # Header power stats
    pills = [
        f'<div class="stat-pill">FTP Outdoor: <strong>{ftp_outdoor}w</strong></div>',
        f'<div class="stat-pill">FTP Indoor: <strong>{ftp_indoor}w</strong></div>',
    ]
    for dur, label in [(60, "1-min"), (300, "5-min"), (600, "10-min"), (1200, "20-min")]:
        best = (curve_data.get(dur) or {}).get("best")
        if best:
            pills.append(f'<div class="stat-pill">{label}: <strong>{best}w</strong></div>')
    if phenotype and phenotype.get("primary") not in ("unknown", None):
        pills.append(
            f'<div class="stat-pill">Phenotype: <strong>{phenotype["primary"]}</strong>'
            f' — {phenotype["best_kom_label"]}</div>'
        )
    header_stats = f'<div class="header-stats">{"".join(pills)}</div>'

    # Executive summary
    exec_summary = _build_exec_summary(tier1, tier2, landmarks, curve_data, ftp_outdoor)
    s0 = _render_exec_summary(exec_summary)

    # Section 1: Tier 1
    if tier1:
        rows = "\n".join(_segment_row(s, p25, p75, p90) for s in tier1)
        s1 = f"""
    <section>
      <h2>Tier 1: My KOM Targets ({len(tier1)})</h2>
      <p class="section-desc">All starred + segments ridden ≥3×, sorted by achievability. ★ Famous = community landmark.</p>
      <div class="table-wrap">
        <table>{TABLE_HEAD}<tbody>{rows}</tbody></table>
      </div>
    </section>"""
    else:
        s1 = ""

    # Section 2: Tier 2
    if tier2:
        rows = "\n".join(_segment_row(s, p25, p75, p90) for s in tier2)
        s2 = f"""
    <section>
      <h2>Tier 2: Early Encounters ({len(tier2)})</h2>
      <p class="section-desc">Segments ridden 1–2×. Ride more to build confidence and unlock full leaderboard context.</p>
      <div class="table-wrap">
        <table>{TABLE_HEAD}<tbody>{rows}</tbody></table>
      </div>
    </section>"""
    else:
        s2 = ""

    # Section 3: Famous Climbs: Unridden
    unridden = [lm for lm in landmarks if lm.get("never_ridden")]
    if unridden:
        rows = "\n".join(_famous_unridden_row(lm, p25, p75, p90) for lm in unridden)
        s3 = f"""
    <section>
      <h2>Famous Climbs: Unridden ({len(unridden)})</h2>
      <p class="section-desc">Community-famous segments you haven't ridden yet. Future targets.</p>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Segment</th><th>Athletes</th><th>KOM</th>
            <th>Competition</th><th>Strava</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""
    else:
        s3 = ""

    # Section 4: Tier 3
    if tier3 is None:
        s4 = """
    <section>
      <div class="callout">
        <strong>Tier 3 — Undiscovered candidates not available.</strong>
        Run <code>python segment_scout.py</code> first to discover nearby climb candidates.
      </div>
    </section>"""
    elif tier3:
        rows = "\n".join(_tier3_row(c) for c in tier3)
        s4 = f"""
    <section>
      <h2>Tier 3: Fresh Discoveries ({len(tier3)})</h2>
      <p class="section-desc">Nearby segments matching your climb profile, sorted by community size. Haven't ridden these yet.</p>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Segment</th><th>Grade</th><th>Distance</th><th>Athletes</th><th>Match Score</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""
    else:
        s4 = """
    <section>
      <div class="callout">
        <strong>Tier 3 — No candidates found.</strong>
        Try <code>python segment_scout.py --min-achievability=3</code> with a lower threshold.
      </div>
    </section>"""

    # Filter bar — radius input only shown if home coords available
    radius_input = ""
    if home_lat and home_lng:
        radius_input = f"""
      <div class="filter-group">
        <label>Radius ≤</label>
        <input type="number" id="f-radius" min="0" max="200" placeholder="km">
        <label>km</label>
      </div>"""

    filter_bar = f"""
  <div class="filter-bar" id="filter-bar">
    <div class="filter-group">
      <label>Grade</label>
      <input type="number" id="f-grade-min" min="0" max="30" placeholder="min%">
      <label>–</label>
      <input type="number" id="f-grade-max" min="0" max="30" placeholder="max%">
    </div>
    <div class="filter-group">
      <label>% Power ≤</label>
      <input type="number" id="f-pct-max" min="0" max="200" placeholder="%">
    </div>
    <div class="filter-group">
      <label>Athletes</label>
      <input type="number" id="f-ath-min" min="0" placeholder="min">
      <label>–</label>
      <input type="number" id="f-ath-max" placeholder="max">
    </div>
    <div class="filter-group">
      <label>Efforts ≥</label>
      <input type="number" id="f-efforts-min" min="0" placeholder="#">
    </div>{radius_input}
    <button class="filter-reset" onclick="resetFilters()">Reset</button>
    <span class="filter-count" id="filter-count"></span>
  </div>"""

    filter_js = f"""
<script>
const HOME_LAT = {home_lat or 'null'};
const HOME_LNG = {home_lng or 'null'};

function haversine(lat1, lng1, lat2, lng2) {{
  const R = 6371;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLng = (lng2 - lng1) * Math.PI / 180;
  const a = Math.sin(dLat/2)**2 + Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*Math.sin(dLng/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}}

function applyFilters() {{
  const gradeMin  = parseFloat(document.getElementById('f-grade-min').value)  || null;
  const gradeMax  = parseFloat(document.getElementById('f-grade-max').value)  || null;
  const pctMax    = parseFloat(document.getElementById('f-pct-max').value)    || null;
  const athMin    = parseFloat(document.getElementById('f-ath-min').value)    || null;
  const athMax    = parseFloat(document.getElementById('f-ath-max').value)    || null;
  const effortsMin= parseFloat(document.getElementById('f-efforts-min').value)|| null;
  const radiusEl  = document.getElementById('f-radius');
  const radiusMax = radiusEl ? parseFloat(radiusEl.value) || null : null;

  const rows = document.querySelectorAll('tr[data-grade]');
  let visible = 0;
  rows.forEach(row => {{
    const grade   = parseFloat(row.dataset.grade);
    const pct     = parseFloat(row.dataset.pct);
    const athletes= parseFloat(row.dataset.athletes);
    const efforts = parseFloat(row.dataset.efforts);
    const lat     = row.dataset.lat ? parseFloat(row.dataset.lat) : null;
    const lng     = row.dataset.lng ? parseFloat(row.dataset.lng) : null;

    let hide = false;
    if (gradeMin  !== null && grade    < gradeMin)   hide = true;
    if (gradeMax  !== null && grade    > gradeMax)   hide = true;
    if (pctMax    !== null && pct      > pctMax)     hide = true;
    if (athMin    !== null && athletes < athMin)     hide = true;
    if (athMax    !== null && athletes > athMax)     hide = true;
    if (effortsMin!== null && efforts  < effortsMin) hide = true;
    if (radiusMax !== null && HOME_LAT && lat) {{
      const dist = haversine(HOME_LAT, HOME_LNG, lat, lng);
      if (dist > radiusMax) hide = true;
    }}

    row.classList.toggle('filtered-out', hide);
    if (!hide) visible++;
  }});

  const countEl = document.getElementById('filter-count');
  if (rows.length > 0) countEl.textContent = visible + ' / ' + rows.length + ' segments';
}}

function resetFilters() {{
  ['f-grade-min','f-grade-max','f-pct-max','f-ath-min','f-ath-max','f-efforts-min','f-radius'].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.value = '';
  }});
  applyFilters();
}}

document.addEventListener('DOMContentLoaded', () => {{
  document.querySelectorAll('.filter-bar input').forEach(el => {{
    el.addEventListener('input', applyFilters);
  }});
}});
</script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>KOM Scouting Report — {today}</title>
  <style>{CSS}</style>
</head>
<body>
  {filter_bar}
  <div class="container">
    <h1>KOM Scouting Report</h1>
    <p class="subtitle">Generated {today} · All tiers uncapped · Deep dive</p>
    {header_stats}
    {s0}{s1}{s2}{s3}{s4}
    <div class="footer">
      <p>Click <strong>Strava ↗</strong> to view and star any segment directly.</p>
      <p>Competition badge thresholds are data-driven from your local segment cache (p25/p75/p90).</p>
      <p>Data freshness: leaderboards cached 7 days · explore tiles cached 30 days</p>
    </div>
  </div>
  {filter_js}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Save + open
# ---------------------------------------------------------------------------

def _save_and_open(html):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = date.today().isoformat()
    tmp_path = "/tmp/kom_scout.html"
    persistent = os.path.join(REPORTS_DIR, f"kom_scout_{today}.html")

    with open(tmp_path, "w") as f:
        f.write(html)
    with open(persistent, "w") as f:
        f.write(html)

    print(f"Saved: {persistent}")
    print("Opening preview...")
    webbrowser.open(f"file://{tmp_path}")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _backfill():
    """
    One-time: warm the detail cache for all outdoor rides (3 years) and
    populate the segment_history DuckDB table.
    """
    print("Backfill: fetching 3 years of activity list...")
    activities = strava_client.fetch_activities(weeks=156, force_refresh=True)

    outdoor_rides = [a for a in activities
                     if a.get("type") == "Ride" and not a.get("trainer", False)]
    print(f"Found {len(outdoor_rides)} outdoor rides")

    cached = [r for r in outdoor_rides
              if os.path.exists(os.path.join(STREAM_CACHE_DIR, f"detail_{r['id']}.json"))]
    uncached = [r for r in outdoor_rides
                if not os.path.exists(os.path.join(STREAM_CACHE_DIR, f"detail_{r['id']}.json"))]

    print(f"Already cached: {len(cached)} | Fetching uncached: {len(uncached)}...")
    for i, ride in enumerate(uncached):
        strava_client.fetch_activity_description(ride["id"])
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(uncached)} fetched...")
        time.sleep(4.5)  # Strava: 200 req/15min → ~4.5s/req

    print("Cache warmed. Extracting segment efforts from all rides...")
    all_efforts = []
    for ride in outdoor_rides:
        try:
            rd = date.fromisoformat(ride.get("start_date_local", "")[:10])
        except ValueError:
            continue
        efforts = strava_client.fetch_activity_segments(ride["id"])
        if not efforts:
            continue
        for effort in efforts:
            seg = effort.get("segment", {})
            seg_id = seg.get("id")
            if not seg_id:
                continue
            avg_grade = seg.get("average_grade", 0)
            elapsed_time = effort.get("elapsed_time", 0)
            if avg_grade < 5 or avg_grade <= 0:
                continue
            if not (60 <= elapsed_time <= 600):
                continue
            all_efforts.append({
                "activity_id":   ride["id"],
                "activity_date": rd,
                "segment_id":    seg_id,
                "segment_name":  seg.get("name", ""),
                "elapsed_time_s": elapsed_time,
                "avg_watts":     effort.get("average_watts"),
                "avg_grade":     avg_grade,
                "distance_m":    seg.get("distance", 0),
            })

    db.write_segment_history(all_efforts)
    print(f"Backfilled {len(outdoor_rides)} rides, {len(all_efforts)} segment efforts written to DB")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _apply_filters(segments, keyword=None, min_grade=None, max_grade=None,
                   max_time=None, min_efforts=None, max_radius=None,
                   home_lat=None, home_lng=None):
    """Apply CLI filters to a list of segment dicts. Returns filtered list."""
    result = []
    for seg in segments:
        if keyword and keyword.lower() not in seg.get("segment_name", "").lower():
            continue
        grade = seg.get("avg_grade") or 0
        if min_grade is not None and grade < min_grade:
            continue
        if max_grade is not None and grade > max_grade:
            continue
        if max_time is not None:
            best_t = seg.get("elapsed_time_s")
            if best_t is not None and best_t > max_time:
                continue
        if min_efforts is not None and seg.get("effort_count", 0) < min_efforts:
            continue
        if max_radius is not None and home_lat and home_lng:
            latlng = seg.get("start_latlng")
            if latlng and len(latlng) >= 2:
                dist = metrics.haversine_km(home_lat, home_lng, latlng[0], latlng[1])
                if dist > max_radius:
                    continue
        result.append(seg)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="KOM Scouting Report")
    parser.add_argument("--backfill", action="store_true",
                        help="Warm detail cache for 3 years of rides and populate segment_history DB")
    parser.add_argument("--keyword", type=str, default=None,
                        help="Filter segments by name substring (case-insensitive)")
    parser.add_argument("--min-grade", type=float, default=None,
                        help="Exclude segments with avg_grade < N%%")
    parser.add_argument("--max-grade", type=float, default=None,
                        help="Exclude segments with avg_grade > N%%")
    parser.add_argument("--max-time", type=int, default=None,
                        help="Exclude segments where David's best elapsed_time_s > N")
    parser.add_argument("--min-efforts", type=int, default=None,
                        help="Exclude segments with fewer than N efforts")
    parser.add_argument("--max-radius", type=float, default=None,
                        help="Exclude segments > N km from home (uses start_latlng)")
    args = parser.parse_args()

    if args.backfill:
        strava_client.refresh_access_token()
        _backfill()
        return

    # 1. Bootstrap
    strava_client.refresh_access_token()
    activities = strava_client.fetch_activities(weeks=8, force_refresh=False)
    ftp_outdoor, ftp_indoor, *_ = metrics.load_ftps()
    home_lat, home_lng = metrics.load_home_coords()

    print("Computing power curve...")
    curve_data = metrics.power_curve(
        activities, strava_client.fetch_power_stream,
        ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor
    )
    phenotype = metrics.compute_power_phenotype(curve_data)
    print(f"Phenotype: {phenotype['primary']} — {phenotype['best_kom_label']}")

    print("Fetching starred segments...")
    starred = strava_client.fetch_starred_segments()
    print(f"Starred segments: {len(starred)}")

    print("Extracting ride segments...")
    ride_segs = metrics.extract_ride_segments(
        activities, strava_client.fetch_activity_segments, weeks=8
    )
    print(f"Ride segments: {len(ride_segs)} unique")

    # 2. Famous segment discovery (needed before tier builder for is_famous flag)
    print("Discovering landmark segments...")
    landmark_ids = _discover_landmarks(starred, ride_segs, home_lat, home_lng)
    landmarks = _enrich_landmarks(
        landmark_ids,
        strava_client.fetch_segment_leaderboard,
        ride_segs, curve_data, ftp_outdoor, starred,
        weight_kg=metrics.load_rider_weight()
    )
    print(f"Landmark segments discovered: {len(landmarks)} (seed + explore + high-traffic)")

    # 3. Full uncapped tier 1/2 (with is_famous overlay)
    print("Building full tier tracker (uncapped)...")
    tier1, tier2 = _build_full_tracker(
        starred, ride_segs,
        strava_client.fetch_segment_leaderboard,
        curve_data, home_lat, home_lng,
        landmark_ids=landmark_ids,
        leaderboard_cap=40
    )
    print(f"Tier 1: {len(tier1)} | Tier 2: {len(tier2)}")

    # 3b. Apply CLI filters
    filter_kwargs = dict(
        keyword=args.keyword,
        min_grade=args.min_grade,
        max_grade=args.max_grade,
        max_time=args.max_time,
        min_efforts=args.min_efforts,
        max_radius=args.max_radius,
        home_lat=home_lat,
        home_lng=home_lng,
    )
    if any(v is not None for v in [args.keyword, args.min_grade, args.max_grade,
                                    args.max_time, args.min_efforts, args.max_radius]):
        tier1 = _apply_filters(tier1, **filter_kwargs)
        tier2 = _apply_filters(tier2, **filter_kwargs)
        print(f"After filters — Tier 1: {len(tier1)} | Tier 2: {len(tier2)}")

    # 4. Tier 3
    tier3 = _load_tier3_candidates()
    if tier3 is None:
        print("Tier 3: not found — run segment_scout.py first")
    else:
        print(f"Tier 3: {len(tier3)} candidates loaded")

    # 4b. Annotate segments with phenotype match
    for seg in tier1 + tier2:
        seg["phenotype_match"] = _phenotype_match(seg.get("kom_time_s"), phenotype)

    # 5. Competition percentiles (data-driven from cache)
    p25, p75, p90 = _compute_athlete_percentiles()
    print(f"Competition thresholds — Local:<{p25:.0f} | Competitive:{p25:.0f}–{p75:.0f} | "
          f"Elite:{p75:.0f}–{p90:.0f} | World-class:>{p90:.0f}")

    # 6. Persist + render
    today = date.today().isoformat()
    _save_json(tier1, tier2, landmarks, tier3, curve_data, ftp_outdoor, ftp_indoor)
    print(f"Saved: data/reports/kom_scout_{today}.json")

    html = _render_html(tier1, tier2, landmarks, tier3, curve_data, ftp_outdoor, ftp_indoor, p25, p75, p90,
                        home_lat=home_lat, home_lng=home_lng, phenotype=phenotype)
    _save_and_open(html)


if __name__ == "__main__":
    main()
