#!/usr/bin/env python3
"""
segment_similarity.py — Find segments most similar to David's proven top-10 KOM segments.

Algorithm:
  Build a 3-dimensional standardized feature vector per segment:
    v = [grade_z, log_elapsed_z, log_athletes_z]
  Z-score normalization uses population stats from ALL local segments combined —
  no arbitrary weights, each dimension contributes equally.

  For each candidate, find the nearest proven segment (min euclidean distance).
  Lower similarity_dist = more similar.

Geographic distance is a display column, not a similarity dimension.
Hard 15km pre-filter removes non-local noise.

Usage:
    python segment_similarity.py              # top 25, opens HTML
    python segment_similarity.py --top=50
    python segment_similarity.py --no-open   # save but don't open browser
    python segment_similarity.py --max-dist=2.0
"""

import os
import re
import json
import math
import argparse
import webbrowser
from datetime import date
from glob import glob

import numpy as np

from dotenv import load_dotenv
load_dotenv()

import requests
import metrics
import strava_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR  = os.path.join(BASE_DIR, "data", "reports")
CACHE_DIR    = os.path.join(BASE_DIR, ".cache")
CANDS_PATH   = os.path.join(BASE_DIR, "data", "segment_candidates.json")
LOCAL_RADIUS = 15.0  # km hard pre-filter

# Power model constants
# RIDER_KG: update when David's weight changes significantly (preferences.md: RIDER_WEIGHT_KG)
RIDER_KG       = 77     # rider weight kg (170 lbs, current)
# BIKE_KG: Specialized Secteur Elite (aluminum/carbon) ~9.1–10 kg + water bottle
BIKE_KG        = 10     # bike + bottle + minimal kit (upper end = conservative planning)
WEIGHT_KG      = RIDER_KG + BIKE_KG   # total system mass for physics
# CRR: calibrated against David's PRs on SF segments; SF streets are rougher than smooth tarmac
CRR            = 0.005  # rolling resistance (calibrated; smooth tarmac=0.004, rough=0.006)
CDA            = 0.35   # drag area (m²), road cyclist on hoods
RHO            = 1.2    # air density kg/m³ at ~sea level / 15°C
DRIVETRAIN_EFF = 0.97   # crank-to-wheel efficiency; divide to get crank power


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_latest_report():
    files = sorted(glob(os.path.join(REPORTS_DIR, "kom_scout_*.json")))
    if not files:
        raise FileNotFoundError(f"No kom_scout_*.json reports found in {REPORTS_DIR}")
    with open(files[-1]) as f:
        return json.load(f)


def _build_detail_seg_cache():
    """
    Scan all .cache/detail_*.json files.
    Returns {segment_id: {"latlng": [lat, lng], "distance_m": float}}.
    """
    cache = {}
    for fname in glob(os.path.join(CACHE_DIR, "detail_*.json")):
        try:
            with open(fname) as f:
                d = json.load(f)
        except Exception:
            continue
        for effort in d.get("segment_efforts", []):
            seg = effort.get("segment", {})
            sid = seg.get("id")
            if not sid or sid in cache:
                continue
            cache[sid] = {
                "latlng":     seg.get("start_latlng"),
                "distance_m": seg.get("distance"),
            }
    return cache


def _build_david_pr_cache():
    """
    Scan all .cache/detail_*.json files to find David's best effort per segment.
    Best effort = lowest elapsed_time per segment_id.
    Returns {segment_id: {pr_time_s, pr_watts, pr_rank, distance_m, avg_grade, start_latlng}}.
    pr_rank is the leaderboard rank at the time that effort was recorded (may be stale).
    """
    best = {}
    for fname in glob(os.path.join(CACHE_DIR, "detail_*.json")):
        try:
            with open(fname) as f:
                d = json.load(f)
        except Exception:
            continue
        for effort in d.get("segment_efforts", []):
            if effort.get("hidden"):
                continue
            watts  = effort.get("average_watts")
            time_s = effort.get("elapsed_time")
            if not watts or not time_s:
                continue
            seg  = effort.get("segment", {})
            sid  = seg.get("id")
            if not sid:
                continue
            if sid not in best or time_s < best[sid]["pr_time_s"]:
                best[sid] = {
                    "pr_time_s":    time_s,
                    "pr_watts":     watts,
                    "pr_rank":      effort.get("pr_rank"),
                    "distance_m":   seg.get("distance"),
                    "avg_grade":    seg.get("average_grade"),
                    "start_latlng": seg.get("start_latlng"),
                }
    return best


def _build_explore_latlng_cache():
    """Scan all .cache/explore_*.json files → {segment_id: [lat, lng]}."""
    cache = {}
    for fname in glob(os.path.join(CACHE_DIR, "explore_*.json")):
        try:
            with open(fname) as f:
                d = json.load(f)
        except Exception:
            continue
        for seg in d.get("segments", []):
            sid = seg.get("id")
            ll  = seg.get("start_latlng")
            if sid and ll and len(ll) == 2 and sid not in cache:
                cache[sid] = ll
    return cache


# ---------------------------------------------------------------------------
# Feature vector construction
# ---------------------------------------------------------------------------

def _compute_pop_stats(segments):
    """
    Compute population z-score parameters from all local segments.
    Returns dict with grade_mu, grade_sig, logt_mu, logt_sig, loga_mu, loga_sig.
    """
    grades, log_ts, log_as = [], [], []
    for s in segments:
        g = s.get("avg_grade")
        e = s.get("elapsed_s")
        a = s.get("total_athletes")
        if g is not None:
            grades.append(g)
        if e and e > 0:
            log_ts.append(math.log(e))
        if a and a > 0:
            log_as.append(math.log(a + 1))

    def safe_stats(vals):
        if not vals:
            return 0.0, 1.0
        mu  = float(np.mean(vals))
        sig = float(np.std(vals))
        return mu, max(sig, 1e-9)

    gm, gs = safe_stats(grades)
    tm, ts = safe_stats(log_ts)
    am, as_ = safe_stats(log_as)
    return {"grade_mu": gm, "grade_sig": gs,
            "logt_mu": tm,  "logt_sig": ts,
            "loga_mu": am,  "loga_sig": as_}


def _make_vec(grade, elapsed, athletes, stats):
    """
    Build standardized 3D feature vector. NaN for missing dimensions.
    v = [grade_z, log_elapsed_z, log_athletes_z]
    """
    grade_z = ((grade - stats["grade_mu"]) / stats["grade_sig"]
               if grade is not None else float("nan"))
    logt_z  = ((math.log(elapsed) - stats["logt_mu"]) / stats["logt_sig"]
               if elapsed and elapsed > 0 else float("nan"))
    loga_z  = ((math.log(athletes + 1) - stats["loga_mu"]) / stats["loga_sig"]
               if athletes and athletes > 0 else float("nan"))
    return np.array([grade_z, logt_z, loga_z])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _nn_distance(cand_vec, proven_vecs):
    """
    Find nearest proven segment by euclidean distance in standardized space.
    Returns (distance, nearest_proven_dict). Computes only on non-NaN dimensions.
    """
    best_dist   = float("inf")
    best_proven = None
    for proven_seg, pv in proven_vecs:
        valid = ~(np.isnan(cand_vec) | np.isnan(pv))
        if valid.sum() < 2:
            continue
        d = float(np.linalg.norm(cand_vec[valid] - pv[valid]))
        if d < best_dist:
            best_dist   = d
            best_proven = proven_seg
    return best_dist, best_proven


def _dim_contributions(cand_vec, proven_vec):
    """
    Returns per-dimension signed z-score differences: [dgrade, dlogt, dloga].
    Positive = candidate is higher than proven on that dimension.
    """
    return cand_vec - proven_vec  # NaN propagates for missing dims


DIM_LABELS = ["grade", "elapsed", "athletes"]


def _build_explanation(diffs):
    """
    Convert per-dimension z-score diffs to a short natural-language explanation.
    diffs: [dgrade, dlogt, dloga]
    """
    parts = []
    dg, dt, da = diffs[0], diffs[1], diffs[2]
    if not math.isnan(dg):
        if dg > 0.5:
            parts.append("steeper")
        elif dg < -0.5:
            parts.append("less steep")
    if not math.isnan(dt):
        if dt > 0.5:
            parts.append("longer effort")
        elif dt < -0.5:
            parts.append("shorter burst")
    if not math.isnan(da):
        if da > 0.5:
            parts.append("more competitive field")
        elif da < -0.5:
            parts.append("smaller field")
    if not parts:
        return "very close match"
    return ", ".join(parts)


def _score_candidates(pool, proven_vecs, pop_stats, proven_latlngs):
    """
    Score all candidates. Returns list of result dicts sorted by similarity_dist asc.
    """
    results = []
    for cand in pool:
        cv = _make_vec(cand.get("avg_grade"), cand.get("elapsed_s"),
                       cand.get("total_athletes"), pop_stats)
        dist, nearest = _nn_distance(cv, proven_vecs)
        if nearest is None:
            continue

        pv = next(pv for ps, pv in proven_vecs if ps is nearest)
        diffs = _dim_contributions(cv, pv)
        explanation = _build_explanation(diffs)

        # Geographic distance to nearest proven segment
        geo_dist = None
        cand_ll = cand.get("start_latlng")
        if cand_ll and proven_latlngs:
            geo_dist = min(
                metrics.haversine_km(cand_ll[0], cand_ll[1], ll[0], ll[1])
                for ll in proven_latlngs
            )

        results.append({
            "segment_id":          cand["segment_id"],
            "segment_name":        cand["segment_name"],
            "avg_grade":           cand.get("avg_grade"),
            "elapsed_s":           cand.get("elapsed_s"),
            "elapsed_is_estimate": cand.get("elapsed_is_estimate", False),
            "total_athletes":      cand.get("total_athletes"),
            "distance_m":          cand.get("distance_m"),
            "kom_time_s":          cand.get("kom_time_s"),
            "tier":                cand.get("tier"),
            "david_rank":          cand.get("david_rank"),
            "similarity_dist":     dist,
            "most_like":           nearest["segment_name"],
            "geo_dist_km":         geo_dist,
            "explanation":         explanation,
        })

    results.sort(key=lambda r: r["similarity_dist"])
    return results


# ---------------------------------------------------------------------------
# Power analysis
# ---------------------------------------------------------------------------

def _fetch_proven_segment_ids(athlete_id=11887293):
    """
    Scrape David's KOM and top-10 leader pages on Strava for the authoritative
    list of segments where he holds a top-10 position.

    Returns dict: {segment_id: rank} where rank=1 means KOM.
    Falls back to {} if cookie is missing or pages are inaccessible.
    """
    cookie = os.environ.get("STRAVA_WEB_COOKIE", "").strip()
    if not cookie:
        return {}

    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    }
    proven = {}

    for url, is_kom in [
        (f"https://www.strava.com/athletes/{athlete_id}/segments/leader", True),
        (f"https://www.strava.com/athletes/{athlete_id}/segments/leader?top_tens=true", False),
    ]:
        try:
            resp = requests.get(
                url, headers=headers,
                cookies={"_strava4_session": cookie},
                timeout=15, allow_redirects=True,
            )
        except Exception:
            continue
        if resp.status_code != 200 or "login" in resp.url:
            continue
        # Extract segment IDs from links like href="/segments/12345"
        for sid_str, _name in re.findall(r'href="/segments/(\d+)"[^>]*>([^<]+)<', resp.text):
            sid = int(sid_str)
            if sid not in proven:
                proven[sid] = 1 if is_kom else None  # rank=1 for KOM, None=unknown top-10

    return proven


def _parse_leaderboard_html(html):
    """
    Extract elapsed_time values (seconds) from Strava's leaderboard HTML response.
    Strava returns an HTML fragment (not JSON) for the /leaderboard XHR endpoint.
    Returns sorted list of ints, or [].
    """
    # Decode HTML entities
    html = (html
            .replace("&quot;", '"').replace("&amp;", "&")
            .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))

    # Pattern 1: data-tracking JSON on each entry containing elapsed_time
    matches = re.findall(r'data-tracking=["\']([^"\']+)["\']', html)
    times = []
    for m in matches:
        try:
            obj = json.loads(m)
            t = obj.get("elapsed_time")
            if t and isinstance(t, int) and 30 <= t <= 7200:
                times.append(t)
        except Exception:
            pass
    if times:
        return sorted(set(times))

    # Pattern 2: "elapsed_time": N in any embedded JSON blob
    matches = re.findall(r'"elapsed_time"\s*:\s*(\d+)', html)
    if matches:
        times = [int(m) for m in matches if 30 <= int(m) <= 7200]
        if times:
            return sorted(set(times))

    # Pattern 3: M:SS time values in the leaderboard context
    if "leader" in html.lower():
        matches = re.findall(r'\b(\d{1,2}):(\d{2})\b', html)
        if matches:
            times = [int(m)*60 + int(s) for m, s in matches if 30 <= int(m)*60+int(s) <= 7200]
            if times:
                return sorted(set(times))

    return []


def _fetch_top10_web(segment_id):
    """
    Fetch top-10 leaderboard times via Strava web session cookie.

    The OAuth API requires partner access (returns 403). Strava's web leaderboard
    endpoint returns an HTML fragment (not JSON) for XHR requests.
    Set STRAVA_WEB_COOKIE in .env to the _strava4_session browser cookie value.

    Cached 7 days to .cache/seg_top10_{segment_id}.json.
    Returns {"top10_times_s": [...], "kom_time_s": int, "tenth_time_s": int}
    or {} if unavailable.
    """
    from datetime import datetime
    cache_path = os.path.join(CACHE_DIR, f"seg_top10_{segment_id}.json")

    # 7-day cache (skip if data is present and fresh)
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        age = (datetime.now().date() -
               datetime.fromisoformat(cached.get("fetched", "2000-01-01")).date()).days
        if age < 7 and cached.get("data"):
            return cached["data"]

    cookie = os.environ.get("STRAVA_WEB_COOKIE", "").strip()
    if not cookie:
        return {}

    try:
        resp = requests.get(
            f"https://www.strava.com/segments/{segment_id}/leaderboard",
            params={"per_page": 10},
            headers={
                "Accept": "application/json, text/javascript, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://www.strava.com/segments/{segment_id}",
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
            },
            cookies={"_strava4_session": cookie},
            timeout=10,
            allow_redirects=False,
        )
    except Exception:
        return {}

    if resp.status_code != 200:
        if resp.status_code == 302:
            print(f"    Warning: leaderboard for {segment_id} redirected to login "
                  "— STRAVA_WEB_COOKIE may have expired")
        return {}

    # Strava returns an HTML fragment; parse it for elapsed_time values
    times = _parse_leaderboard_html(resp.text)

    result = {}
    if times:
        result["top10_times_s"] = times
        result["kom_time_s"]    = times[0]
        result["tenth_time_s"]  = times[-1]
        if times[0] > 0:
            result["spread_pct"] = round((times[-1] - times[0]) / times[0] * 100, 1)

    with open(cache_path, "w") as f:
        json.dump({"fetched": datetime.now().date().isoformat(), "data": result}, f)
    return result


def _implied_power(distance_m, grade_pct, time_s, weight_kg=WEIGHT_KG):
    """
    Estimate the power required to ride distance_m at grade_pct% in time_s seconds.
    Uses total system weight (rider + bike) and accounts for drivetrain losses.

    Returns a dict with three estimates:
      lower_w  — gravity + rolling only (best case: tailwind / smooth surface)
      point_w  — gravity + rolling + aero in still air (honest physics estimate)
      upper_w  — gravity + rolling + 1.5× aero (light ~10 km/h headwind)

    Returns None if inputs are insufficient (missing, zero, or grade < 3%).
    """
    if not distance_m or not time_s or not grade_pct or grade_pct < 3:
        return None
    g      = 9.81
    grade  = grade_pct / 100
    v      = distance_m / time_s
    P_gr   = weight_kg * g * (grade + CRR) * v   # gravity + rolling at wheel
    P_aero = 0.5 * CDA * RHO * v ** 3            # aero drag in still air
    # Divide by drivetrain efficiency to get crank power (what power meters measure)
    return {
        "lower_w": int(round(P_gr / DRIVETRAIN_EFF)),
        "point_w": int(round((P_gr + P_aero) / DRIVETRAIN_EFF)),
        "upper_w": int(round((P_gr + P_aero * 1.5) / DRIVETRAIN_EFF)),
    }


def _fetch_seg_altitude_stream(segment_id):
    """
    Fetch the altitude + distance stream for a segment from the Strava API.
    Used to enable profile-based power simulation (more accurate than avg-grade model).

    Cached indefinitely to .cache/seg_alt_{segment_id}.json — segment geometry never changes.
    Returns {"distance": [...], "altitude": [...]} or {} if unavailable.
    """
    cache_path = os.path.join(CACHE_DIR, f"seg_alt_{segment_id}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    try:
        resp = requests.get(
            f"https://www.strava.com/api/v3/segments/{segment_id}/streams",
            params={"keys": "distance,altitude"},
            headers=strava_client._headers(),
            timeout=10,
        )
    except Exception:
        return {}

    if resp.status_code != 200:
        return {}

    try:
        streams = {s["type"]: s["data"] for s in resp.json()}
        result = {
            "distance": streams.get("distance", []),
            "altitude": streams.get("altitude", []),
        }
        if len(result["distance"]) < 2:
            return {}
        with open(cache_path, "w") as f:
            json.dump(result, f)
        return result
    except Exception:
        return {}


def _implied_power_profile(distance_arr, altitude_arr, time_s, weight_kg=WEIGHT_KG):
    """
    Profile-based power estimate using the actual elevation profile.
    Finds the constant power P such that simulated ride time over the grade profile
    equals time_s. More accurate than avg-grade model for variable-grade segments.

    Physics: at each interval, solve 0.5*CDA*RHO*v³ + weight*g*(grade+CRR)*v = P for v.
    Descents capped at -5% (rider coasts/limits effort on descent).
    Applies drivetrain efficiency correction to convert wheel power → crank power.

    Returns same dict format as _implied_power() (lower/point/upper_w),
    or None if stream data is insufficient.
    """
    if len(distance_arr) < 2 or len(altitude_arr) < 2:
        return None

    def _v_at_power(P, g_frac, cda_mult=1.0):
        lo, hi = 0.01, 25.0
        for _ in range(50):
            m = (lo + hi) / 2
            if 0.5 * cda_mult * CDA * RHO * m**3 + weight_kg * 9.81 * (g_frac + CRR) * m < P:
                lo = m
            else:
                hi = m
        return (lo + hi) / 2

    def _sim_time(P, cda_mult=1.0):
        t = 0.0
        for i in range(1, len(distance_arr)):
            dd = distance_arr[i] - distance_arr[i - 1]
            dh = altitude_arr[i] - altitude_arr[i - 1]
            gf = max(dh / dd, -0.05) if dd > 0 else 0.0
            t += dd / _v_at_power(P, gf, cda_mult)
        return t

    def _find_P(cda_mult=1.0):
        lo_p, hi_p = 50.0, 1200.0
        for _ in range(60):
            m = (lo_p + hi_p) / 2
            if _sim_time(m, cda_mult) > time_s:
                lo_p = m
            else:
                hi_p = m
        return (lo_p + hi_p) / 2

    try:
        P_point = _find_P(cda_mult=1.0)   # still air
        P_lower = _find_P(cda_mult=0.0)   # no aero (tailwind / best case)
        P_upper = _find_P(cda_mult=1.5)   # 1.5× aero (light headwind)
    except Exception:
        return None

    return {
        "lower_w": int(round(P_lower / DRIVETRAIN_EFF)),
        "point_w": int(round(P_point / DRIVETRAIN_EFF)),
        "upper_w": int(round(P_upper / DRIVETRAIN_EFF)),
    }


def _grade_bias_correction(avg_grade_pct):
    """
    Empirical correction for known model underestimation on steep SF streets.

    Calibrated from 63 local climbing genuine-effort PRs (profile-based model):
      5–12% grade  : mean error +0.2% — no correction needed
      12%+  grade  : mean error -6.8% — model underestimates (n=5)

    Physical cause: standing climbing on steep SF streets increases rolling
    resistance and drivetrain inefficiency beyond what CRR=0.005 captures.
    Returns a multiplier applied to all three power variants (lower/point/upper).
    """
    if avg_grade_pct is None:
        return 1.0
    if avg_grade_pct >= 12.0:
        return 1.07   # +7% to correct confirmed -6.8% underestimate
    return 1.0


def _duration_zone(duration_s):
    """
    Return (label, css_class) for the physiological zone of a given effort duration.
    This is the interpretive key for the power gap — the zone tells you how much
    day-to-day variability exists and whether the gap is closeable without training.
    """
    if duration_s is None:
        return ("—", "zone-unknown")
    if duration_s < 120:
        return ("Anaerobic", "zone-anaerobic")   # W' dependent; ±10–15% day-to-day
    if duration_s < 480:
        return ("VO2max", "zone-vo2max")          # ~5–8% day-to-day variability
    return ("Threshold", "zone-threshold")        # stable; gap requires training weeks


def _enrich_power(results, power_curve, proven, weight_kg=77):
    """
    Add power analysis columns to each result dict (in-place).

    For each result:
    - Fetches KOM time via web leaderboard scrape (if not already on result)
    - Computes implied KOM power from physics: P = m*g*(grade+Crr)*(d/t)
    - Computes implied 10th-place power (actual if scraped; estimated if not)
    - Computes David's estimated power at those durations via power curve interpolation
    - Adds: kom_time_s, tenth_time_s, tenth_is_estimate, kom_power_w,
            tenth_power_w, david_power_at_kom_w, david_power_at_10th_w,
            kom_gap_w, tenth_gap_w

    power_curve: {"60": {"best": 520}, "300": ...} (string keys from JSON)
    proven: list of proven segment dicts (used to calibrate 10th-place spread model)
    """
    # Convert power curve keys to int for _interpolate_power
    curve = {int(k): v for k, v in power_curve.items()}

    # Calibrate 10th-place spread from proven rank=10 segments
    # spread = (david_time - kom_time) / kom_time at position 10
    rank10_spreads = []
    for s in proven:
        if s.get("david_rank") == 10 and s.get("elapsed_time_s") and s.get("kom_time_s"):
            spread = (s["elapsed_time_s"] - s["kom_time_s"]) / s["kom_time_s"]
            rank10_spreads.append((spread, s.get("total_athletes", 100)))
    avg_rank10_spread = (sum(sp for sp, _ in rank10_spreads) / len(rank10_spreads)
                         if rank10_spreads else 0.18)

    # David's actual PRs from activity detail caches — used to validate model
    david_prs = _build_david_pr_cache()

    # Refresh OAuth token once before any altitude stream fetches
    try:
        strava_client.refresh_access_token()
    except Exception:
        pass

    has_web_cookie = bool(os.environ.get("STRAVA_WEB_COOKIE", "").strip())
    if has_web_cookie:
        print(f"  Fetching leaderboard data for {len(results)} segments via web session...")
    else:
        print("  No STRAVA_WEB_COOKIE set — 10th place times will be estimated.")
        print("  (Set STRAVA_WEB_COOKIE in .env to your _strava4_session browser cookie)")

    for r in results:
        seg_id     = r["segment_id"]
        grade      = r.get("avg_grade")
        distance_m = r.get("distance_m")
        kom_time_s = r.get("kom_time_s")

        # Fetch leaderboard: try web scrape, fall back to already-cached API data
        top10 = _fetch_top10_web(seg_id)
        if top10.get("kom_time_s"):
            kom_time_s = top10["kom_time_s"]

        # Strip David's own entry from leaderboard times.
        # Strava always appends the authenticated user's row to the leaderboard HTML
        # when they're not in the top 10 — so the scraped list has 9 genuine entries
        # + David's "your time" row, making times[-1] his PR, not actual 10th place.
        # pr_rank in the detail cache is stale (may show an old KOM rank), so we
        # detect David's appended entry purely by time match at the tail.
        times_list = list(top10.get("top10_times_s") or [])
        pr_pre = david_prs.get(seg_id)
        if (times_list and pr_pre and pr_pre.get("pr_time_s")
                and times_list[-1] == pr_pre["pr_time_s"]):
            # Only strip if his time is NOT faster than any other entry — i.e., he's
            # genuinely the slowest in the list (= not a real top-10 finisher here).
            # If he were truly 10th, his time would equal the 10th-place entry and
            # we'd still show the correct time (worst case: we show 9th instead of 10th,
            # a conservative ~1-2s difference).
            times_list = times_list[:-1]

        tenth_time_s      = times_list[-1] if times_list else None
        tenth_is_estimate = tenth_time_s is None

        # Estimate 10th place if not scraped
        if tenth_is_estimate and kom_time_s:
            tenth_time_s = round(kom_time_s * (1 + avg_rank10_spread))

        # Fetch segment altitude stream for profile-based simulation
        # Falls back to avg-grade model (_implied_power) if stream unavailable
        alt_stream = _fetch_seg_altitude_stream(seg_id)
        dist_arr = alt_stream.get("distance", [])
        alt_arr  = alt_stream.get("altitude", [])
        use_profile = len(dist_arr) >= 2

        grade_mult = _grade_bias_correction(grade)

        def _power_estimate(time_s):
            if time_s is None:
                return None
            if use_profile:
                pw = _implied_power_profile(dist_arr, alt_arr, time_s, weight_kg)
                if pw and grade_mult != 1.0:
                    pw = {k: int(round(v * grade_mult)) for k, v in pw.items()}
                if pw:
                    return pw
            pw = _implied_power(distance_m, grade, time_s, weight_kg)
            if pw and grade_mult != 1.0:
                pw = {k: int(round(v * grade_mult)) for k, v in pw.items()}
            return pw

        # Implied power range dicts for KOM and 10th place
        kom_pw   = _power_estimate(kom_time_s)
        tenth_pw = _power_estimate(tenth_time_s)

        # David's estimated power at those durations from his power curve
        david_at_kom  = metrics._interpolate_power(kom_time_s, curve) if kom_time_s else None
        david_at_10th = metrics._interpolate_power(tenth_time_s, curve) if tenth_time_s else None

        # Gap = point estimate minus David's best (positive = David needs more power)
        def _gap(pw_dict, david_w):
            if not pw_dict or not david_w:
                return None, None
            gap_w   = pw_dict["point_w"] - david_w
            gap_pct = round(gap_w / david_w * 100, 1)
            return gap_w, gap_pct

        kom_gap_w,   kom_gap_pct   = _gap(kom_pw, david_at_kom)
        tenth_gap_w, tenth_gap_pct = _gap(tenth_pw, david_at_10th)

        # David's actual PR on this segment (from activity detail caches)
        pr = david_prs.get(r["segment_id"])
        david_pr_time_s  = pr["pr_time_s"]  if pr else None
        david_pr_watts   = pr["pr_watts"]   if pr else None
        david_pr_rank    = pr["pr_rank"]    if pr else None

        # Model validation: run physics estimate at David's actual PR time.
        # Only compute for efforts where Strava registered a leaderboard rank (pr_rank set),
        # which indicates David was actually pushing — not just riding through the segment.
        # model_error_pct > 0 = model overestimates (inflated KOM/10th estimates too)
        model_error_pct = None
        if pr and david_pr_time_s and david_pr_rank is not None:
            pr_model_pw = _power_estimate(david_pr_time_s)
            if pr_model_pw and david_pr_watts:
                model_error_pct = round(
                    (pr_model_pw["point_w"] - david_pr_watts) / david_pr_watts * 100, 1
                )

        # Leaderboard sanity checks — flag if any of:
        # 1. David's actual watts > 10th-place estimate but his rank > 10 (circular/bad data)
        # 2. Spread > 40%: KOM to 10th spread >40% is implausible on a real competitive segment
        # 3. Fewer than 8 entries: thin leaderboard, 10th-place time is unreliable
        # Recompute spread from cleaned times_list (David's entry already stripped)
        n_entries = len(times_list)
        if n_entries >= 2 and times_list[0] > 0:
            spread_pct = round((times_list[-1] - times_list[0]) / times_list[0] * 100, 1)
        else:
            spread_pct = top10.get("spread_pct", 0) or 0
        leaderboard_suspect = bool(
            (david_pr_watts and tenth_pw and david_pr_rank and
             david_pr_rank > 10 and david_pr_watts > tenth_pw["point_w"])
            or spread_pct > 40
            or (n_entries > 0 and n_entries < 8)
        )

        r.update({
            "kom_time_s":              kom_time_s,
            "tenth_time_s":            tenth_time_s,
            "tenth_is_estimate":       tenth_is_estimate,
            # Power ranges (lower = no aero, point = still air, upper = light headwind)
            "kom_power_lower_w":       kom_pw["lower_w"] if kom_pw else None,
            "kom_power_point_w":       kom_pw["point_w"] if kom_pw else None,
            "kom_power_upper_w":       kom_pw["upper_w"] if kom_pw else None,
            "tenth_power_lower_w":     tenth_pw["lower_w"] if tenth_pw else None,
            "tenth_power_point_w":     tenth_pw["point_w"] if tenth_pw else None,
            "tenth_power_upper_w":     tenth_pw["upper_w"] if tenth_pw else None,
            # David's power at each duration (from power curve)
            "david_power_at_kom_w":    david_at_kom,
            "david_power_at_10th_w":   david_at_10th,
            # Gap vs David's best (point estimate basis)
            "kom_gap_w":               kom_gap_w,
            "kom_gap_pct":             kom_gap_pct,
            "tenth_gap_w":             tenth_gap_w,
            "tenth_gap_pct":           tenth_gap_pct,
            # Physiological zone labels
            "kom_zone":                _duration_zone(kom_time_s),
            "tenth_zone":              _duration_zone(tenth_time_s),
            # David's actual PR on this segment
            "david_pr_time_s":         david_pr_time_s,
            "david_pr_watts":          david_pr_watts,
            "david_pr_rank":           david_pr_rank,
            "model_error_pct":         model_error_pct,
            "leaderboard_suspect":     leaderboard_suspect,
        })


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
.badge-gray { background: #374151; color: #9ca3af; }
.tag-tier3    { background: #1e3a5f; color: #60a5fa; padding: 2px 7px; border-radius: 8px; font-size: 11px; }
.tag-kom      { background: #713f12; color: #fde68a; padding: 2px 7px; border-radius: 8px; font-size: 11px; font-weight: 700; }
.tag-top10    { background: #064e3b; color: #6ee7b7; padding: 2px 7px; border-radius: 8px; font-size: 11px; font-weight: 600; }
.tag-ridden   { background: #1f2937; color: #9ca3af; padding: 2px 7px; border-radius: 8px; font-size: 11px; }
.tag-unridden { background: #111827; color: #6b7280; padding: 2px 7px; border-radius: 8px; font-size: 11px; border: 1px solid #374151; }
.explain { font-size: 11px; color: #6b7280; }
.power-gap-good  { color: #4ade80; font-weight: 600; }
.power-gap-close { color: #fcd34d; font-weight: 600; }
.power-gap-hard  { color: #9ca3af; font-weight: 600; }
.power-note { font-size: 10px; color: #6b7280; }
.model-ok    { color: #6b7280; font-size: 10px; }
.model-warn  { color: #fcd34d; font-size: 10px; font-weight: 600; }
.model-bad   { color: #f87171; font-size: 10px; font-weight: 600; }
.lb-suspect  { color: #f87171; font-size: 10px; font-weight: 600; }
.zone-anaerobic  { background: #4c1d95; color: #c4b5fd; padding: 2px 6px; border-radius: 8px; font-size: 10px; font-weight: 600; }
.zone-vo2max     { background: #1e3a5f; color: #93c5fd; padding: 2px 6px; border-radius: 8px; font-size: 10px; font-weight: 600; }
.zone-threshold  { background: #14532d; color: #86efac; padding: 2px 6px; border-radius: 8px; font-size: 10px; font-weight: 600; }
.zone-unknown    { background: #374151; color: #9ca3af; padding: 2px 6px; border-radius: 8px; font-size: 10px; }

/* Proven profile block */
.proven-block {
  background: #111827; border: 1px solid #374151; border-radius: 12px;
  padding: 20px; margin-bottom: 32px;
}
.proven-title {
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.8px;
  color: #6b7280; font-weight: 600; margin-bottom: 12px;
}
.proven-stats { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }

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

.footer {
  border-top: 1px solid #374151; padding-top: 16px; color: #6b7280;
  font-size: 12px; margin-top: 32px; line-height: 2;
}

@media (max-width: 600px) {
  h1 { font-size: 22px; }
  .filter-bar { gap: 8px; }
  .filter-group input[type=number] { width: 52px; }
}
"""


def _fmt_time(seconds):
    if seconds is None:
        return "—"
    s = int(seconds)
    m = s // 60
    sec = s % 60
    return f"{m}:{sec:02d}"


def _sim_color(dist):
    """Color for similarity distance badge."""
    if dist < 1.0:
        return "#14532d", "#4ade80"   # dark green bg, green text
    if dist < 2.0:
        return "#78350f", "#fcd34d"   # amber
    return "#450a0a", "#f87171"       # red


def _tier_badge(tier, david_rank, has_pr=False):
    if tier == 3:
        return '<span class="tag-tier3">Tier 3</span>'
    if david_rank == 1:
        return '<span class="tag-kom">KOM</span>'
    if david_rank and david_rank <= 10:
        return f'<span class="tag-top10">Top-10 #{david_rank}</span>'
    if david_rank or has_pr:
        rank_str = f"#{david_rank}" if david_rank else "PR"
        return f'<span class="tag-ridden">Ridden {rank_str}</span>'
    return '<span class="tag-unridden">Unridden</span>'


def _render_html(results, proven_segs, pop_stats, top_n):
    today = date.today().isoformat()
    shown = results[:top_n]

    # --- Proven profile summary ---
    grades = [s["avg_grade"] for s in proven_segs if s.get("avg_grade")]
    elapseds = [s["elapsed_time_s"] for s in proven_segs if s.get("elapsed_time_s")]
    grade_rng = f"{min(grades):.1f}–{max(grades):.1f}%" if grades else "—"
    dur_rng   = f"{_fmt_time(min(elapseds))}–{_fmt_time(max(elapseds))}" if elapseds else "—"

    proven_rows = ""
    for s in proven_segs:
        proven_rows += f"""
        <tr>
          <td><a class="seg-link" href="https://www.strava.com/segments/{s['segment_id']}" target="_blank">{s['segment_name']}</a></td>
          <td>{f"{s['avg_grade']:.1f}%" if s.get('avg_grade') is not None else '—'}</td>
          <td>{_fmt_time(s.get('elapsed_time_s'))}</td>
          <td>{'#'+str(s['david_rank']) if s.get('david_rank') else '<span title="Confirmed on Strava top-10 leader page; exact rank not scraped">top-10*</span>'}</td>
          <td>{s.get('total_athletes', '—')}</td>
        </tr>"""

    # --- Candidate rows ---
    def _gap_html(gap_w, gap_pct):
        """Render a watt+% gap with color. Negative = David is already faster."""
        if gap_w is None or gap_pct is None:
            return "—"
        sign = "+" if gap_w > 0 else "−"
        abs_w   = abs(gap_w)
        abs_pct = abs(gap_pct)
        if gap_w <= 0:
            return f'<span class="power-gap-good">{sign}{abs_w}w ({sign}{abs_pct:.0f}%) ✓</span>'
        cls = "power-gap-close" if abs_pct <= 10 else "power-gap-hard"
        return f'<span class="{cls}">{sign}{abs_w}w ({sign}{abs_pct:.0f}%)</span>'

    def _power_cell(point_w, lower_w, upper_w, david_w, gap_w, gap_pct, time_s, zone, is_est=False):
        """Render a power column: point [lower–upper] @ time [zone] / David: Xw → gap."""
        if point_w is None:
            return "—"
        zone_label, zone_cls = zone
        range_str  = f"[{lower_w}–{upper_w}w]" if lower_w and upper_w else ""
        est_note   = ' <span class="power-note">(est)</span>' if is_est else ""
        david_str  = f"David: {david_w}w" if david_w else ""
        gap_str    = _gap_html(gap_w, gap_pct)
        return (
            f'<strong>{point_w}w</strong> '
            f'<span style="font-size:11px;color:#6b7280">{range_str}</span> '
            f'@ {_fmt_time(time_s)}{est_note} '
            f'<span class="{zone_cls}">{zone_label}</span><br>'
            f'<span style="font-size:11px;color:#9ca3af">{david_str} → {gap_str}</span>'
        )

    def _pr_cell(pr_time_s, pr_watts, pr_rank, model_error_pct, leaderboard_suspect):
        """Render David's actual PR: time, watts, rank, model error, and leaderboard flag."""
        if pr_time_s is None:
            return '<span style="color:#4b5563;font-size:11px">no data</span>'
        rank_str = f"rank {pr_rank}" if pr_rank else "rank ?"
        # Model error indicator
        if model_error_pct is None:
            err_html = ""
        elif abs(model_error_pct) <= 8:
            err_html = f'<span class="model-ok"> model {model_error_pct:+.0f}%</span>'
        elif abs(model_error_pct) <= 18:
            err_html = f'<span class="model-warn"> model {model_error_pct:+.0f}%</span>'
        else:
            err_html = f'<span class="model-bad"> model {model_error_pct:+.0f}%</span>'
        # Leaderboard sanity flag
        lb_html = ' <span class="lb-suspect">⚠ lb?</span>' if leaderboard_suspect else ""
        return (
            f'<strong>{int(round(pr_watts))}w</strong> @ {_fmt_time(pr_time_s)}<br>'
            f'<span style="font-size:11px;color:#9ca3af">{rank_str}{err_html}{lb_html}</span>'
        )

    cand_rows = ""
    for r in shown:
        seg_id = r["segment_id"]
        url    = f"https://www.strava.com/segments/{seg_id}"
        grade_str = f"{r['avg_grade']:.1f}%" if r.get("avg_grade") is not None else "—"
        # Show KOM time as the representative competitive duration.
        # elapsed_s is David's PR time (used only for similarity scoring internally).
        kom_display_time = r.get("kom_time_s") or r.get("elapsed_s")
        elapsed_str = _fmt_time(kom_display_time)
        athletes_str = f"{r['total_athletes']:,}" if r.get("total_athletes") else "—"
        dist = r["similarity_dist"]
        bg, fg = _sim_color(dist)
        sim_badge = f'<span class="badge" style="background:{bg};color:{fg}">{dist:.2f}</span>'
        most_like = r.get("most_like", "—")
        explain   = r.get("explanation", "")
        geo_str   = f"{r['geo_dist_km']:.1f} km" if r.get("geo_dist_km") is not None else "—"
        tier_badge = _tier_badge(r["tier"], r.get("david_rank"), has_pr=r.get("david_pr_watts") is not None)

        kom_zone   = r.get("kom_zone")   or ("—", "zone-unknown")
        tenth_zone = r.get("tenth_zone") or ("—", "zone-unknown")
        kom_cell   = _power_cell(
            r.get("kom_power_point_w"), r.get("kom_power_lower_w"), r.get("kom_power_upper_w"),
            r.get("david_power_at_kom_w"), r.get("kom_gap_w"), r.get("kom_gap_pct"),
            r.get("kom_time_s"), kom_zone,
        )
        tenth_cell = _power_cell(
            r.get("tenth_power_point_w"), r.get("tenth_power_lower_w"), r.get("tenth_power_upper_w"),
            r.get("david_power_at_10th_w"), r.get("tenth_gap_w"), r.get("tenth_gap_pct"),
            r.get("tenth_time_s"), tenth_zone,
            is_est=r.get("tenth_is_estimate", True),
        )
        pr_cell = _pr_cell(
            r.get("david_pr_time_s"), r.get("david_pr_watts"), r.get("david_pr_rank"),
            r.get("model_error_pct"), r.get("leaderboard_suspect", False),
        )

        cand_rows += f"""
        <tr data-dist="{dist:.4f}" data-grade="{r.get('avg_grade', 0):.1f}" data-tier="{r['tier']}">
          <td>
            <a class="seg-link" href="{url}" target="_blank">{r['segment_name']}</a><br>
            <span class="explain">{explain}</span>
          </td>
          <td>{tier_badge}</td>
          <td>{grade_str}</td>
          <td>{elapsed_str}</td>
          <td>{athletes_str}</td>
          <td>{sim_badge}</td>
          <td style="color:#9ca3af;font-size:12px">{most_like}</td>
          <td style="color:#9ca3af;font-size:12px">{geo_str}</td>
          <td>{pr_cell}</td>
          <td>{kom_cell}</td>
          <td>{tenth_cell}</td>
          <td><a class="strava-link-small" href="{url}" target="_blank">↗</a></td>
        </tr>"""

    pop_info = (f"Population: {pop_stats['grade_mu']:.1f}% avg grade, "
                f"{math.exp(pop_stats['logt_mu']):.0f}s avg duration, "
                f"{math.exp(pop_stats['loga_mu']):.0f} avg athletes (local segments)")

    # Calibration summary across all shown results with PR data and model errors
    validated = [r for r in shown if r.get("model_error_pct") is not None]
    if validated:
        errors = [r["model_error_pct"] for r in validated]
        mean_err = sum(errors) / len(errors)
        mae = sum(abs(e) for e in errors) / len(errors)
        calibration_note = (
            f"Model calibration: {len(validated)} validated segments — "
            f"mean error {mean_err:+.1f}%, MAE {mae:.1f}% "
            f"(WEIGHT_KG={WEIGHT_KG}, CRR={CRR}, profile simulation; +7% correction applied at ≥12% grade)"
        )
    else:
        calibration_note = (
            f"Model: WEIGHT_KG={WEIGHT_KG} kg, CRR={CRR}, profile simulation where available; "
            f"+7% correction at ≥12% grade (calibrated from 63 local PRs)"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Similar KOM Segments — {today}</title>
  <style>{CSS}</style>
</head>
<body>

<div class="filter-bar" id="filterBar">
  <div class="filter-group">
    <label>Max dist</label>
    <input type="number" id="maxDist" value="10" min="0" step="0.5">
  </div>
  <div class="filter-group">
    <label>Min grade %</label>
    <input type="number" id="minGrade" value="0" min="0" step="1">
  </div>
  <div class="filter-group">
    <label>
      <input type="checkbox" id="tier3Only"> Tier 3 only
    </label>
  </div>
  <button class="filter-reset" onclick="resetFilters()">Reset</button>
  <span class="filter-count" id="filterCount"></span>
</div>

<div class="container">
  <h1>Similar KOM Segments</h1>
  <p class="subtitle">Generated {today} &mdash; segments most similar to David's 6 proven top-10 KOM efforts</p>

  <div class="proven-block">
    <div class="proven-title">Proven Profile ({len(proven_segs)} segments)</div>
    <div class="proven-stats">
      <span class="stat-pill"><strong>Grade range:</strong> {grade_rng}</span>
      <span class="stat-pill"><strong>Duration range:</strong> {dur_rng}</span>
      <span class="stat-pill" style="font-size:11px;color:#6b7280">{pop_info}</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Proven Segment</th><th>Grade</th><th>David's Time</th>
            <th>David's Rank</th><th>Athletes</th>
          </tr>
        </thead>
        <tbody>{proven_rows}</tbody>
      </table>
    </div>
  </div>

  <section>
    <h2>Similar Segments</h2>
    <p class="section-desc">
      Ranked by nearest-neighbor euclidean distance in standardized [grade, log(duration), log(athletes)] space.
      Similarity &lt;1.0 = very close match; &lt;2.0 = similar; higher = diverges. Geographic distance is from nearest proven segment.
    </p>
    <div class="table-wrap">
      <table id="simTable">
        <thead>
          <tr>
            <th>Segment</th><th>Tier</th><th>Grade</th><th>KOM Time</th>
            <th>Athletes</th><th>Similarity ↑</th><th>Most Like</th>
            <th>Dist to KOM Turf</th><th>David's PR</th><th>KOM Power Needed</th><th>10th Power Needed</th><th></th>
          </tr>
        </thead>
        <tbody>{cand_rows}</tbody>
      </table>
    </div>
  </section>

  <div class="footer">
    Algorithm: standardized euclidean nearest-neighbor &mdash; no arbitrary weights.<br>
    Features: avg_grade, log(elapsed_s), log(total_athletes), z-scored over all local segments.<br>
    {calibration_note}<br>
    Generated by segment_similarity.py
  </div>
</div>

<script>
function applyFilters() {{
  const maxDist  = parseFloat(document.getElementById('maxDist').value) || 99;
  const minGrade = parseFloat(document.getElementById('minGrade').value) || 0;
  const tier3Only = document.getElementById('tier3Only').checked;
  const rows = document.querySelectorAll('#simTable tbody tr');
  let visible = 0;
  rows.forEach(row => {{
    const dist  = parseFloat(row.dataset.dist);
    const grade = parseFloat(row.dataset.grade);
    const tier  = parseInt(row.dataset.tier);
    const hide  = dist > maxDist || grade < minGrade || (tier3Only && tier !== 3);
    row.classList.toggle('filtered-out', hide);
    if (!hide) visible++;
  }});
  document.getElementById('filterCount').textContent = visible + ' of ' + rows.length + ' shown';
}}
function resetFilters() {{
  document.getElementById('maxDist').value  = '10';
  document.getElementById('minGrade').value = '0';
  document.getElementById('tier3Only').checked = false;
  applyFilters();
}}
['maxDist','minGrade'].forEach(id => document.getElementById(id).addEventListener('input', applyFilters));
document.getElementById('tier3Only').addEventListener('change', applyFilters);
window.addEventListener('load', applyFilters);
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def run_calibration():
    """
    Fetch altitude streams for all local climbing segments where David has a genuine
    PR (pr_rank set), then run the physics model for each and report error distribution.

    Filters: local (≤15 km), avg_grade ≥ 3%, elapsed 60–900s, pr_rank not None.
    Streams are cached permanently; subsequent runs are fast.
    """
    import time as _time

    home_lat, home_lng = metrics.load_home_coords()

    def _is_local(ll):
        if not ll or len(ll) < 2:
            return False
        return metrics.haversine_km(home_lat, home_lng, ll[0], ll[1]) <= 15.0

    all_prs = _build_david_pr_cache()
    candidates = {
        sid: pr for sid, pr in all_prs.items()
        if pr.get("pr_rank") is not None
        and (pr.get("avg_grade") or 0) >= 3.0
        and 60 <= (pr.get("pr_time_s") or 0) <= 900
        and _is_local(pr.get("start_latlng"))
    }
    print(f"Calibration set: {len(candidates)} local climbing genuine efforts")

    # Refresh token once before batch fetch
    try:
        strava_client.refresh_access_token()
    except Exception:
        pass

    # Fetch missing altitude streams
    need_fetch = [sid for sid in candidates
                  if not os.path.exists(os.path.join(CACHE_DIR, f"seg_alt_{sid}.json"))]
    if need_fetch:
        print(f"Fetching {len(need_fetch)} altitude streams (≈{len(need_fetch)*0.5:.0f}s)...")
        for i, sid in enumerate(need_fetch):
            _fetch_seg_altitude_stream(sid)
            if i % 20 == 19:
                print(f"  {i+1}/{len(need_fetch)} fetched...")
            _time.sleep(0.5)
        print(f"  Done. All streams cached.")
    else:
        print("All streams already cached.")

    # Run model for each and collect errors
    errors = []
    skipped = 0
    for sid, pr in candidates.items():
        stream = _fetch_seg_altitude_stream(sid)
        if not stream:
            skipped += 1
            continue
        pw = _implied_power_profile(stream["distance"], stream["altitude"], pr["pr_time_s"])
        if not pw or not pr.get("pr_watts"):
            skipped += 1
            continue
        # Apply the same grade bias correction used in the live report
        mult = _grade_bias_correction(pr.get("avg_grade"))
        corrected_w = int(round(pw["point_w"] * mult))
        err_pct = (corrected_w - pr["pr_watts"]) / pr["pr_watts"] * 100
        errors.append({
            "segment_id": sid,
            "pr_time_s":  pr["pr_time_s"],
            "pr_watts":   pr["pr_watts"],
            "model_w":    corrected_w,
            "err_pct":    err_pct,
            "avg_grade":  pr.get("avg_grade"),
        })

    if not errors:
        print("No validated segments — check stream fetch.")
        return

    errs = [e["err_pct"] for e in errors]
    mean_err = sum(errs) / len(errs)
    mae      = sum(abs(e) for e in errs) / len(errs)
    p25      = sorted(errs)[len(errs)//4]
    p75      = sorted(errs)[3*len(errs)//4]

    print(f"\n{'='*55}")
    print(f"CALIBRATION RESULTS  (n={len(errors)}, skipped={skipped})")
    print(f"  WEIGHT_KG={WEIGHT_KG}, CRR={CRR}, DRIVETRAIN_EFF={DRIVETRAIN_EFF}")
    print(f"{'='*55}")
    print(f"  Mean error : {mean_err:+.1f}%  (+ = model overestimates, - = underestimates)")
    print(f"  MAE        : {mae:.1f}%")
    print(f"  p25–p75    : {p25:+.1f}% to {p75:+.1f}%")
    print(f"  Min / Max  : {min(errs):+.1f}% / {max(errs):+.1f}%")

    # Break down by grade and duration buckets
    print(f"\n  By grade (correction: +7% applied at ≥12%):")
    for lo, hi, label in [(3,7,'3–7%'), (7,12,'7–12%'), (12,99,'12%+')]:
        bucket = [e for e in errors if lo <= (e['avg_grade'] or 0) < hi]
        if bucket:
            me = sum(e['err_pct'] for e in bucket) / len(bucket)
            print(f"    {label:6s}: n={len(bucket):3d}  mean={me:+.1f}%")

    print(f"\n  By duration:")
    for lo, hi, label in [(60,120,'1–2 min'), (120,300,'2–5 min'), (300,600,'5–10 min'), (600,900,'10–15 min')]:
        bucket = [e for e in errors if lo <= e['pr_time_s'] < hi]
        if bucket:
            me = sum(e['err_pct'] for e in bucket) / len(bucket)
            print(f"    {label:10s}: n={len(bucket):3d}  mean={me:+.1f}%")

    # Worst outliers
    worst = sorted(errors, key=lambda e: abs(e['err_pct']), reverse=True)[:8]
    print(f"\n  Largest errors (model vs actual):")
    for e in worst:
        print(f"    seg {e['segment_id']:10d}: {e['model_w']}w vs {e['pr_watts']:.0f}w actual "
              f"@ {_fmt_time(e['pr_time_s'])} grade={e['avg_grade']:.1f}%  err={e['err_pct']:+.1f}%")

    print(f"\n  Suggested WEIGHT_KG to zero mean error: "
          f"~{WEIGHT_KG * (1 - mean_err/100):.0f} kg")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Similar KOM Segment Finder")
    parser.add_argument("--top",        type=int,   default=25,  help="Max candidates to show (default 25)")
    parser.add_argument("--no-open",    action="store_true",     help="Save HTML but don't open browser")
    parser.add_argument("--max-dist",   type=float, default=None, help="Filter: max similarity distance")
    parser.add_argument("--calibrate",  action="store_true",     help="Fetch streams + run physics calibration, then exit")
    args = parser.parse_args()

    if args.calibrate:
        run_calibration()
        return

    print("Loading data...")
    report = _load_latest_report()
    home_lat, home_lng = metrics.load_home_coords()

    # Load tier3 candidates
    tier3_candidates = []
    if os.path.exists(CANDS_PATH):
        with open(CANDS_PATH) as f:
            cands_data = json.load(f)
        tier3_candidates = cands_data.get("candidates", [])
    else:
        print(f"  Warning: {CANDS_PATH} not found — tier3 candidates skipped")

    # Build latlng + distance caches
    print("Building segment cache from activity detail cache...")
    detail_seg = _build_detail_seg_cache()
    explore_latlng = _build_explore_latlng_cache()
    print(f"  Detail cache: {len(detail_seg)} segments with latlng/distance")
    print(f"  Explore cache: {len(explore_latlng)} segments with latlng")

    def _get_latlng(seg_id, seg_dict):
        ll = seg_dict.get("start_latlng")
        if ll and len(ll) == 2:
            return ll
        entry = detail_seg.get(seg_id, {})
        return entry.get("latlng") or explore_latlng.get(seg_id)

    def _get_distance(seg_id, seg_dict):
        d = seg_dict.get("distance_m")
        if d:
            return d
        return (detail_seg.get(seg_id) or {}).get("distance_m")

    # Build David's PR cache early — needed for proven segment elapsed_time lookup
    david_pr_data = _build_david_pr_cache()

    # Identify proven segments — primary: scrape Strava leader pages (authoritative)
    # Fallback: use kom_scout report entries where david_rank <= 10 is already set
    print("\nFetching proven segment list from Strava leader pages...")
    strava_proven_ids = _fetch_proven_segment_ids()
    if strava_proven_ids:
        print(f"  Found {len(strava_proven_ids)} segments from Strava (KOMs + top-10s)")
    else:
        print("  Leader pages unavailable (cookie expired?) — falling back to kom_scout ranks")

    # Build index of all segments in the kom_scout report for quick lookup
    report_by_id = {}
    for s in report.get("tier1", []) + report.get("tier2", []) + report.get("tier3", []) + report.get("landmarks", []):
        report_by_id[s["segment_id"]] = s

    proven = []
    proven_ids = set()

    if strava_proven_ids:
        # Use Strava leader pages as authoritative source
        for sid, strava_rank in strava_proven_ids.items():
            # Get segment metadata from report (grade, elapsed_time) or detail cache
            s = dict(report_by_id.get(sid, {}))
            if not s.get("elapsed_time_s"):
                # Try david_prs via detail cache for the elapsed time
                pr = david_pr_data.get(sid)
                if pr:
                    s["elapsed_time_s"] = pr["pr_time_s"]
                    if not s.get("avg_grade"):
                        # Grade from detail cache segment entry
                        dc = detail_seg.get(sid, {})
                        # avg_grade not in detail_seg (only latlng + distance_m)
            if not s.get("elapsed_time_s"):
                continue  # can't use without a reference time
            if not s.get("segment_id"):
                s["segment_id"] = sid
            if not s.get("segment_name"):
                s["segment_name"] = f"Segment {sid}"
            # Use Strava rank; fall back to report rank if available
            s["david_rank"] = strava_rank or s.get("david_rank")
            s["start_latlng"] = _get_latlng(sid, s)
            s["distance_m"] = _get_distance(sid, s)
            proven.append(s)
            proven_ids.add(sid)
    else:
        # Fallback: kom_scout report with rank already set
        for s in report.get("tier1", []) + report.get("tier2", []):
            rank = s.get("david_rank")
            if rank is not None and rank <= 10 and s.get("elapsed_time_s"):
                ll = _get_latlng(s["segment_id"], s)
                s = dict(s)
                s["start_latlng"] = ll
                proven.append(s)
                proven_ids.add(s["segment_id"])

    if len(proven) < 3:
        print(f"Error: only {len(proven)} proven segments found (need ≥3). "
              "Run kom_scout.py to populate the report first.")
        return

    print(f"\nProven segments ({len(proven)}):")
    for s in proven:
        ll_str = f"latlng={s['start_latlng']}" if s.get("start_latlng") else "no latlng"
        grade_str = f"{s['avg_grade']:.1f}%" if s.get("avg_grade") is not None else "grade=?"
        print(f"  rank={s['david_rank']} {grade_str} "
              f"time={_fmt_time(s['elapsed_time_s'])} {ll_str} — {s['segment_name']}")

    proven_latlngs = [s["start_latlng"] for s in proven if s.get("start_latlng")]

    # Build unified local segment pool (for population stats + candidates)
    def _is_local(sid, ll):
        if not ll or len(ll) < 2:
            return False
        return metrics.haversine_km(home_lat, home_lng, ll[0], ll[1]) <= LOCAL_RADIUS

    # Filter proven to local cycling climbs only
    # Removes endurance routes (Marina loop), non-local segments (Mallorca, Marin)
    proven = [
        s for s in proven
        if _is_local(s["segment_id"], s.get("start_latlng"))
        and (s.get("avg_grade") or 0) >= 3.0
        and (s.get("elapsed_time_s") or 0) <= 900
    ]
    print(f"  After filtering to local climbs: {len(proven)} proven segments")

    # All local segments for computing population stats
    all_local_segs = []

    # Tier1/2: collect all (proven + unproven), local only
    for s in report.get("tier1", []) + report.get("tier2", []):
        ll = _get_latlng(s["segment_id"], s)
        if _is_local(s["segment_id"], ll):
            all_local_segs.append({
                "avg_grade":   s.get("avg_grade"),
                "elapsed_s":   s.get("elapsed_time_s"),
                "total_athletes": s.get("total_athletes"),
            })

    # Tier3: local only
    for c in tier3_candidates:
        ll = _get_latlng(c["segment_id"], c) or explore_latlng.get(c["segment_id"])
        if _is_local(c["segment_id"], ll):
            all_local_segs.append({
                "avg_grade":   c.get("avg_grade"),
                "elapsed_s":   c.get("elapsed_time_estimate_s"),
                "total_athletes": c.get("total_athletes"),
            })

    if not all_local_segs:
        print("Error: no local segments found. Check home coordinates in preferences.md.")
        return

    print(f"\nLocal population for z-score stats: {len(all_local_segs)} segments")
    pop_stats = _compute_pop_stats(all_local_segs)
    print(f"  Grade:    μ={pop_stats['grade_mu']:.1f}% σ={pop_stats['grade_sig']:.2f}")
    print(f"  Duration: μ={math.exp(pop_stats['logt_mu']):.0f}s σ (log)={pop_stats['logt_sig']:.2f}")
    print(f"  Athletes: μ={math.exp(pop_stats['loga_mu']):.0f} σ (log)={pop_stats['loga_sig']:.2f}")

    # Build proven feature vectors
    proven_vecs = []
    for s in proven:
        pv = _make_vec(s.get("avg_grade"), s.get("elapsed_time_s"), s.get("total_athletes"), pop_stats)
        proven_vecs.append((s, pv))

    # Build candidate pool
    candidates_pool = []

    # Pool A: unproven tier1/2 (local)
    skipped_nonlocal = 0
    for s in report.get("tier1", []) + report.get("tier2", []):
        if s["segment_id"] in proven_ids:
            continue
        ll = _get_latlng(s["segment_id"], s)
        if not _is_local(s["segment_id"], ll):
            skipped_nonlocal += 1
            continue
        candidates_pool.append({
            "segment_id":          s["segment_id"],
            "segment_name":        s["segment_name"],
            "avg_grade":           s.get("avg_grade"),
            "elapsed_s":           s.get("elapsed_time_s"),
            "elapsed_is_estimate": False,
            "total_athletes":      s.get("total_athletes"),
            "start_latlng":        ll,
            "distance_m":          _get_distance(s["segment_id"], s),
            "tier":                s.get("tier", 1),
            "david_rank":          s.get("david_rank"),
            "kom_time_s":          s.get("kom_time_s"),
        })

    # Pool B: tier3 candidates
    for c in tier3_candidates:
        ll = _get_latlng(c["segment_id"], c) or explore_latlng.get(c["segment_id"])
        if not _is_local(c["segment_id"], ll):
            skipped_nonlocal += 1
            continue
        candidates_pool.append({
            "segment_id":          c["segment_id"],
            "segment_name":        c["segment_name"],
            "avg_grade":           c.get("avg_grade"),
            "elapsed_s":           c.get("elapsed_time_estimate_s"),
            "elapsed_is_estimate": True,
            "total_athletes":      c.get("total_athletes"),
            "start_latlng":        ll,
            "distance_m":          c.get("distance_m"),
            "tier":                3,
            "david_rank":          None,
            "kom_time_s":          c.get("kom_time_s"),
        })

    print(f"\nCandidates: {len(candidates_pool)} local "
          f"({skipped_nonlocal} non-local filtered out)")

    # Score
    results = _score_candidates(candidates_pool, proven_vecs, pop_stats, proven_latlngs)

    if args.max_dist is not None:
        results = [r for r in results if r["similarity_dist"] <= args.max_dist]

    # Enrich top results with power analysis
    power_curve = report.get("power_curve", {})
    weight_kg   = metrics.load_rider_weight()
    print(f"\nEnriching top {min(args.top, len(results))} results with power analysis...")
    _enrich_power(results[:args.top], power_curve, proven, weight_kg)

    print(f"\nTop {min(args.top, len(results))} similar segments:")
    for r in results[:args.top]:
        geo_str = f"{r['geo_dist_km']:.1f}km away" if r.get("geo_dist_km") is not None else ""
        tier_str = "T3" if r["tier"] == 3 else f"T{r['tier']}"
        print(f"  [{r['similarity_dist']:.2f}] {r['segment_name']} "
              f"({tier_str}, grade={r.get('avg_grade', '?'):.1f}%, "
              f"like: {r['most_like']}) {geo_str}")

    # Render + save
    html = _render_html(results, proven, pop_stats, args.top)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = date.today().isoformat()
    out_path = os.path.join(REPORTS_DIR, f"similar_scout_{today}.html")
    with open(out_path, "w") as f:
        f.write(html)
    print(f"\nSaved: {out_path}")

    if not args.no_open:
        webbrowser.open(f"file://{out_path}")
        print("Opening in browser...")


if __name__ == "__main__":
    main()
