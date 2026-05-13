"""
metrics.py — TSS, CTL, ATL, TSB calculations + HR analysis + session matching.

Power source detection:
  - trainer=True  → use FTP_INDOOR (Peloton)
  - trainer=False → use FTP_OUTDOOR (road power meter)
"""

import os
import re
import json
import numpy as np
from datetime import datetime, date, timedelta


# ---------------------------------------------------------------------------
# Zone ranges
# ---------------------------------------------------------------------------

ZONE_PCT = {
    "Z1": (0,    0.60),
    "Z2": (0.60, 0.80),
    "Z3": (0.80, 0.90),
    "Z4": (0.90, 1.10),
    "Z5": (1.10, 1.20),
    "Z6": (1.20, 1.50),
    "Z7": (1.50, None),  # open-ended
}


def compute_zone_ranges(ftp_outdoor, ftp_indoor):
    """
    Compute watt ranges for each zone from the given FTPs.

    Returns {"outdoor": {"Z1": (0, 194), ...}, "indoor": {"Z1": (0, 165), ...}}
    Z7 hi is None (open-ended).
    """
    result = {"outdoor": {}, "indoor": {}}
    for zone, (lo_pct, hi_pct) in ZONE_PCT.items():
        out_lo = round(lo_pct * ftp_outdoor)
        in_lo  = round(lo_pct * ftp_indoor)
        if hi_pct is None:
            result["outdoor"][zone] = (out_lo, None)
            result["indoor"][zone]  = (in_lo,  None)
        else:
            result["outdoor"][zone] = (out_lo, round(hi_pct * ftp_outdoor))
            result["indoor"][zone]  = (in_lo,  round(hi_pct * ftp_indoor))
    return result


# ---------------------------------------------------------------------------
# FTP loading
# ---------------------------------------------------------------------------

def load_home_coords(preferences_path="preferences.md"):
    """Parse HOME_LAT and HOME_LNG from preferences.md."""
    lat, lng = None, None
    try:
        with open(preferences_path) as f:
            for line in f:
                if line.strip().startswith("HOME_LAT:"):
                    lat = float(line.split(":")[1].strip())
                elif line.strip().startswith("HOME_LNG:"):
                    lng = float(line.split(":")[1].strip())
    except FileNotFoundError:
        pass
    return lat, lng


def load_ftps(preferences_path="preferences.md"):
    """
    Parse FTP_OUTDOOR, FTP_INDOOR, FTP_INDOOR_WORKING, and PELOTON_DEVICE_FACTOR
    from preferences.md.

    Returns (ftp_outdoor, ftp_indoor, ftp_indoor_working, peloton_factor).
    ftp_indoor_working is the session-inferred estimate (may be None if not set).
    peloton_factor is the hardware calibration constant (default 1.18).
    """
    ftp_outdoor       = 310   # CP model working estimate
    ftp_indoor        = 275   # Peloton FTP test (may be stale)
    ftp_indoor_working = None  # session-inferred working estimate
    peloton_factor    = 1.18  # hardware device calibration (Peloton reads ~18% low)

    def _key(line):
        """Strip markdown list prefix (- , * , + ) before key matching."""
        s = line.strip()
        if s.startswith(("- ", "* ", "+ ")):
            s = s[2:]
        return s

    def _int_val(s, key):
        """Extract integer after 'KEY:'. Strips trailing 'w' unit if present."""
        after = s[len(key):].strip().split()[0].rstrip("w")
        return int(after)

    def _float_val(s, key):
        """Extract float after 'KEY:'."""
        after = s[len(key):].strip().split()[0].rstrip("w")
        return float(after)

    try:
        with open(preferences_path) as f:
            for line in f:
                s = _key(line)
                if s.startswith("FTP_OUTDOOR:"):
                    try:
                        ftp_outdoor = _int_val(s, "FTP_OUTDOOR:")
                    except (ValueError, IndexError):
                        pass
                elif s.startswith("FTP_INDOOR_WORKING:"):
                    try:
                        ftp_indoor_working = _int_val(s, "FTP_INDOOR_WORKING:")
                    except (ValueError, IndexError):
                        pass
                elif s.startswith("FTP_INDOOR:"):
                    try:
                        ftp_indoor = _int_val(s, "FTP_INDOOR:")
                    except (ValueError, IndexError):
                        pass
                elif s.startswith("PELOTON_DEVICE_FACTOR:"):
                    try:
                        peloton_factor = _float_val(s, "PELOTON_DEVICE_FACTOR:")
                    except (ValueError, IndexError):
                        pass
    except FileNotFoundError:
        pass

    return ftp_outdoor, ftp_indoor, ftp_indoor_working, peloton_factor


def update_ftp(new_ftp, which="outdoor", preferences_path="preferences.md"):
    """
    Update FTP_OUTDOOR or FTP_INDOOR in preferences.md in-place.
    `which` must be 'outdoor' or 'indoor'.
    """
    key = "FTP_OUTDOOR" if which == "outdoor" else "FTP_INDOOR"
    try:
        with open(preferences_path, "r") as f:
            content = f.read()
        content = re.sub(
            rf"({key}:\s*)\d+",
            lambda m: m.group(1) + str(new_ftp),
            content,
        )
        with open(preferences_path, "w") as f:
            f.write(content)
        print(f"preferences.md: {key} updated to {new_ftp}w")
    except Exception as e:
        print(f"update_ftp failed: {e}")
        raise


def load_rider_weight(preferences_path="preferences.md"):
    """Parse RIDER_WEIGHT_KG from preferences.md. Default 77 (David's weight)."""
    weight_kg = 77
    try:
        with open(preferences_path) as f:
            for line in f:
                if line.strip().startswith("RIDER_WEIGHT_KG:"):
                    weight_kg = float(line.split(":")[1].strip())
    except FileNotFoundError:
        pass
    return weight_kg


def load_max_hr(preferences_path="preferences.md"):
    """Parse MAX_HR from preferences.md. Returns None if not set."""
    try:
        with open(preferences_path) as f:
            for line in f:
                stripped = line.strip().lstrip("-").strip()
                if stripped.startswith("MAX_HR:"):
                    return int(stripped.split(":")[1].split()[0])
    except FileNotFoundError:
        pass
    return None


# ---------------------------------------------------------------------------
# TSS per ride
# ---------------------------------------------------------------------------

def tss_for_ride(ride, ftp_outdoor, ftp_indoor):
    """
    Compute TSS for a single Strava activity dict.

    Formula: TSS = (duration_s × NP × IF) / (FTP × 3600) × 100
    where IF = NP / FTP, so TSS = (duration_s × NP²) / (FTP² × 3600) × 100

    Uses weighted_average_watts as proxy for normalized power (NP).
    Falls back to average_watts if NP unavailable.
    """
    is_indoor = ride.get("trainer", False)
    ftp = ftp_indoor if is_indoor else ftp_outdoor

    np_watts = ride.get("weighted_average_watts") or ride.get("average_watts")
    if not np_watts or np_watts <= 0:
        return None

    duration_s = ride.get("moving_time", 0)
    if duration_s <= 0:
        return None

    intensity_factor = np_watts / ftp
    tss = (duration_s * np_watts * intensity_factor) / (ftp * 3600) * 100
    return round(tss, 1)


# ---------------------------------------------------------------------------
# Daily TSS aggregation
# ---------------------------------------------------------------------------

def daily_tss(activities, ftp_outdoor, ftp_indoor, weeks=8):
    """
    Build a dict of {date: total_tss} for the past N weeks.
    If weeks=None, span from the earliest ride date to today (all-time mode).
    Days with no rides get TSS=0.
    """
    today = date.today()

    # Collect ride dates and TSS first so we know the actual span
    ride_tss_map = {}
    for ride in activities:
        if ride.get("type") != "Ride":
            continue
        ride_tss = tss_for_ride(ride, ftp_outdoor, ftp_indoor)
        if ride_tss is None:
            continue
        ride_date_str = ride.get("start_date_local", "")[:10]
        try:
            ride_date = date.fromisoformat(ride_date_str)
        except ValueError:
            continue
        ride_tss_map[ride_date] = ride_tss_map.get(ride_date, 0.0) + ride_tss

    if weeks is None:
        # All-time mode: start from earliest ride date
        start = min(ride_tss_map.keys()) if ride_tss_map else today
    else:
        start = today - timedelta(weeks=weeks)

    # Pre-populate all days with 0
    tss_by_day = {}
    d = start
    while d <= today:
        tss_by_day[d] = 0.0
        d += timedelta(days=1)

    # Fill in actual TSS (only dates within the window)
    for ride_date, tss in ride_tss_map.items():
        if ride_date in tss_by_day:
            tss_by_day[ride_date] += tss

    return tss_by_day


# ---------------------------------------------------------------------------
# PMC: CTL / ATL / TSB
# ---------------------------------------------------------------------------

def compute_pmc(tss_by_day, ctl_days=42, atl_days=7, ctl_seed=0.0, atl_seed=0.0):
    """
    Compute Performance Management Chart metrics.

    CTL (fitness):  42-day exponential weighted average of daily TSS
    ATL (fatigue):   7-day exponential weighted average of daily TSS
    TSB (form):     CTL - ATL (positive = fresh, negative = fatigued)

    ctl_seed / atl_seed: starting values (from DB history) so the window
    doesn't have to recompute from zero each run.

    Returns list of dicts sorted by date:
      [{"date": date, "tss": float, "ctl": float, "atl": float, "tsb": float}, ...]
    """
    sorted_days = sorted(tss_by_day.keys())
    if not sorted_days:
        return []

    ctl = float(ctl_seed)
    atl = float(atl_seed)
    results = []

    for d in sorted_days:
        tss = tss_by_day[d]
        ctl += (tss - ctl) / ctl_days
        atl += (tss - atl) / atl_days
        results.append({
            "date": d,
            "tss":  round(tss, 1),
            "ctl":  round(ctl, 1),
            "atl":  round(atl, 1),
            "tsb":  round(ctl - atl, 1),
        })

    return results


def current_pmc(pmc_series):
    """Return the most recent PMC data point."""
    if not pmc_series:
        return {"ctl": 0, "atl": 0, "tsb": 0, "tss": 0}
    return pmc_series[-1]


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

def weekly_summary(activities, ftp_outdoor, ftp_indoor, week_offset=0):
    """
    Summarize a given week's rides.
    week_offset=0 → current week (Mon–today)
    week_offset=1 → last week
    week_offset=2 → two weeks ago

    Returns dict with: tss, distance_km, elevation_m, duration_min, ride_count,
                       avg_power, rides (list of dicts)
    """
    today = date.today()
    week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=week_offset)
    week_end   = week_start + timedelta(days=6)

    week_rides = []
    for ride in activities:
        if ride.get("type") != "Ride":
            continue
        ride_date_str = ride.get("start_date_local", "")[:10]
        try:
            ride_date = date.fromisoformat(ride_date_str)
        except ValueError:
            continue
        if week_start <= ride_date <= week_end:
            week_rides.append(ride)

    total_tss      = 0.0
    total_dist_m   = 0.0
    total_elev_m   = 0.0
    total_dur_s    = 0
    total_watts    = 0.0
    rides_with_pwr = 0

    ride_summaries = []
    for ride in week_rides:
        t = tss_for_ride(ride, ftp_outdoor, ftp_indoor)
        total_tss    += t or 0
        total_dist_m += ride.get("distance", 0)
        total_elev_m += ride.get("total_elevation_gain", 0)
        total_dur_s  += ride.get("moving_time", 0)
        avg_w = ride.get("average_watts") or 0
        if avg_w > 0:
            total_watts    += avg_w
            rides_with_pwr += 1

        ride_summaries.append({
            "date":     ride.get("start_date_local", "")[:10],
            "name":     ride.get("name", ""),
            "tss":      round(t, 1) if t else None,
            "distance_km": round(ride.get("distance", 0) / 1000, 1),
            "elevation_m": round(ride.get("total_elevation_gain", 0)),
            "duration_min": round(ride.get("moving_time", 0) / 60),
            "avg_watts":    ride.get("average_watts"),
            "trainer":      ride.get("trainer", False),
        })

    return {
        "week_start":    week_start.isoformat(),
        "week_end":      week_end.isoformat(),
        "tss":           round(total_tss, 1),
        "distance_km":   round(total_dist_m / 1000, 1),
        "elevation_m":   round(total_elev_m),
        "duration_min":  round(total_dur_s / 60),
        "ride_count":    len(week_rides),
        "avg_power":     round(total_watts / rides_with_pwr) if rides_with_pwr else None,
        "rides":         sorted(ride_summaries, key=lambda r: r["date"]),
    }


# ---------------------------------------------------------------------------
# Session matching: planned vs actual
# ---------------------------------------------------------------------------

_STOP_WORDS = {"the", "a", "an", "and", "or", "in", "at", "for", "with",
               "min", "warmup", "cooldown", "focus"}

# Keywords that signal what type of effort a ride was
_VO2MAX_WORDS    = {"vo2", "v02"}
_ANAEROBIC_WORDS = {"anaerobic"}
_TEMPO_WORDS     = {"tempo", "sweetspot", "sweet", "threshold"}
_INTERVAL_WORDS  = {"interval", "intervals"}   # generic hard effort
_EASY_WORDS      = {"z2", "z1", "recovery", "shakeout", "flush", "easy", "endurance", "base"}


def _type_compatibility(session_type, ride_name):
    """
    Score 0–1 for how well a ride's name signals match the planned session type.
      1.0 — ride name clearly matches session type
      0.5 — no signal either way (neutral)
      0.0 — ride name clearly signals a different incompatible type

    This prevents a Z2 ride done on the exact planned day from outscoring a VO2max
    ride done one day early just because date proximity dominates.
    """
    words = set(re.sub(r"[^a-z0-9]", " ", ride_name.lower()).split())

    has_vo2max    = bool(words & _VO2MAX_WORDS)
    has_anaerobic = bool(words & _ANAEROBIC_WORDS)
    has_tempo     = bool(words & _TEMPO_WORDS)
    has_interval  = bool(words & _INTERVAL_WORDS)
    has_easy      = bool(words & _EASY_WORDS)

    if session_type in ("vo2max",):
        if has_easy and not has_interval:
            return 0.0   # clearly an easy ride vs hard session
        if has_vo2max or has_interval:
            return 1.0
        if has_anaerobic or has_tempo:
            return 0.3   # different hard type
        return 0.5

    if session_type in ("anaerobic",):
        if has_easy and not has_interval:
            return 0.0
        if has_anaerobic:
            return 1.0
        if has_vo2max or has_interval:
            return 0.4   # similar hard category
        return 0.5

    if session_type in ("threshold",):
        if has_easy and not has_interval:
            return 0.0
        if has_tempo:  # "threshold" keyword already in _TEMPO_WORDS
            return 1.0
        if has_vo2max or has_interval:
            return 0.5
        return 0.5

    if session_type in ("tempo", "sweet_spot"):
        if has_easy and not has_interval:
            return 0.0
        if has_tempo:
            return 1.0
        if has_interval:
            return 0.5
        return 0.5

    if session_type in ("z2", "z1"):
        if (has_vo2max or has_anaerobic) and not has_easy:
            return 0.0   # clearly a hard ride vs easy session
        if has_interval and not has_easy:
            return 0.1
        if has_easy:
            return 1.0
        return 0.5

    return 0.5  # unknown session type — neutral


def _normalize_watts(watts, is_indoor, ftp_outdoor, ftp_indoor):
    """Scale indoor (Peloton) watts to outdoor-equivalent. Outdoor watts pass through unchanged."""
    if not watts or watts <= 0:
        return 0
    if is_indoor and ftp_indoor and ftp_indoor > 0:
        return watts * (ftp_outdoor / ftp_indoor)
    return watts


def _match_score(target_date, session, ride_date, ride, ftp_outdoor=323, ftp_indoor=275):
    """
    Score a (planned session, actual ride) candidate pair.
    Returns 0 if the pair is clearly incompatible, otherwise 0–1.

    Weights:
      40% date proximity      (same day preferred, but type can override a 1-day gap)
      25% type compatibility  (ride name signals vs session type — prevents Z2 stealing VO2max slot)
      20% name similarity     (shared keywords between ride name and session description)
      15% power proximity     (actual watts vs planned target range)

    All watts normalized to outdoor-equivalent before comparison.
    """
    delta = abs((ride_date - target_date).days)
    if delta > 2:
        return 0.0
    date_score = {0: 1.0, 1: 0.6, 2: 0.2}[delta]

    lo, hi = None, None
    r = session.get("target_watts_range")
    if r and len(r) == 2:
        lo, hi = r
    # Use NP when available; normalize indoor → outdoor-equivalent
    is_indoor = ride.get("trainer", False)
    raw = max(ride.get("weighted_average_watts") or 0, ride.get("average_watts") or 0)
    watts = _normalize_watts(raw, is_indoor, ftp_outdoor, ftp_indoor)
    if lo and hi and watts > 0:
        if lo <= watts <= hi * 1.1:
            power_score = 1.0
        elif watts >= lo * 0.85:
            power_score = 0.6
        elif watts >= lo * 0.75:
            power_score = 0.3
        else:
            power_score = 0.1
    else:
        power_score = 0.5  # no power data — neutral

    ride_words = set(re.sub(r"[^a-z0-9]", " ", ride.get("name", "").lower()).split()) - _STOP_WORDS
    session_text = f"{session.get('type', '')} {session.get('zone_label', '')} {session.get('description', '')}"
    session_words = set(re.sub(r"[^a-z0-9]", " ", session_text.lower()).split()) - _STOP_WORDS
    if ride_words and session_words:
        overlap = len(ride_words & session_words)
        name_score = min(overlap / 2, 1.0)
    else:
        name_score = 0.0

    type_score = _type_compatibility(session.get("type", ""), ride.get("name", ""))

    return 0.40 * date_score + 0.25 * type_score + 0.20 * name_score + 0.15 * power_score


def match_sessions_to_rides(plan, activities, ftp_outdoor, ftp_indoor, week_offset=1):
    """
    Match planned sessions to actual rides for a given week using ride-first
    bipartite matching — guarantees each ride appears at most once.

    Returns list of dicts (one per day Mon–Sun):
      {"day": "monday", "date": "2026-03-16", "planned": {...}, "actual": {...} or None}
    """
    today = date.today()
    week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=week_offset)
    week_end   = week_start + timedelta(days=6)

    day_name_to_date = {
        "monday":    week_start,
        "tuesday":   week_start + timedelta(days=1),
        "wednesday": week_start + timedelta(days=2),
        "thursday":  week_start + timedelta(days=3),
        "friday":    week_start + timedelta(days=4),
        "saturday":  week_start + timedelta(days=5),
        "sunday":    week_start + timedelta(days=6),
    }

    # Step 1 — collect rides for the week (exact date range, no fuzzy expansion)
    week_rides = []
    for ride in activities:
        if ride.get("type") != "Ride":
            continue
        try:
            rd = date.fromisoformat(ride.get("start_date_local", "")[:10])
        except ValueError:
            continue
        if week_start <= rd <= week_end:
            week_rides.append((rd, ride))

    # Step 2 — collect planned non-rest sessions
    planned_sessions = {}
    if plan and "weeks" in plan:
        for week in plan["weeks"]:
            try:
                ws = date.fromisoformat(week["week_start"])
            except (ValueError, KeyError):
                continue
            if abs((ws - week_start).days) <= 2:
                for session in week.get("sessions", []):
                    day = session.get("day", "").lower()
                    planned_sessions[day] = session
                break

    non_rest_sessions = {
        day: sess for day, sess in planned_sessions.items()
        if sess.get("type") != "rest"
    }

    # Step 2b — apply manual overrides from data/match_overrides.json
    # Overrides pin a specific activity_id to a planned day, bypassing scoring.
    overrides_path = os.path.join(os.path.dirname(__file__), "data", "match_overrides.json")
    assignments = {}        # day_name → ride
    assigned_ride_ids = set()
    if os.path.exists(overrides_path):
        try:
            with open(overrides_path) as f:
                overrides = json.load(f)
            week_start_str = week_start.isoformat()
            for ov in overrides:
                if ov.get("week_start") != week_start_str:
                    continue
                day = ov.get("day", "").lower()
                if not day:
                    continue
                act_id = ov.get("activity_id")
                if act_id is None:
                    # Explicit force-miss: block auto-matching for this session
                    assignments[day] = {"_force_miss": True}
                else:
                    for ride_date, ride in week_rides:
                        if ride.get("id") == act_id:
                            assignments[day] = ride
                            assigned_ride_ids.add(act_id)
                            break
        except Exception:
            pass

    # Step 3 — score every (planned_day, ride) pair for days not already pinned
    candidates = []
    for day_name, session in non_rest_sessions.items():
        if day_name in assignments:
            continue
        target_date = day_name_to_date[day_name]
        for ride_date, ride in week_rides:
            score = _match_score(target_date, session, ride_date, ride, ftp_outdoor, ftp_indoor)
            if score > 0:
                candidates.append((score, day_name, ride_date, ride))

    # Step 4 — greedy assignment: highest score first, each day and ride used once
    candidates.sort(key=lambda x: x[0], reverse=True)
    for score, day_name, ride_date, ride in candidates:
        if day_name in assignments:
            continue
        rid = ride.get("id")
        if rid in assigned_ride_ids:
            continue
        assignments[day_name] = ride
        assigned_ride_ids.add(rid)

    # Step 5 — unmatched rides → keyed by their actual day
    unplanned_by_day = {}
    for ride_date, ride in week_rides:
        if ride.get("id") in assigned_ride_ids:
            continue
        day_name = [d for d, dt in day_name_to_date.items() if dt == ride_date]
        if day_name:
            unplanned_by_day.setdefault(day_name[0], ride)

    # Step 6 — build 7-row result
    def _make_actual(ride):
        t = tss_for_ride(ride, ftp_outdoor, ftp_indoor)
        is_indoor = ride.get("trainer", False)
        avg_w = ride.get("average_watts")
        np_w  = ride.get("weighted_average_watts") or avg_w
        return {
            "id":                   ride.get("id"),
            "date":                 ride.get("start_date_local", "")[:10],
            "name":                 ride.get("name", ""),
            "duration_min":         round(ride.get("moving_time", 0) / 60),
            "avg_watts":            avg_w,
            "np_watts":             np_w,
            "outdoor_equiv_watts":  round(_normalize_watts(avg_w or 0, is_indoor, ftp_outdoor, ftp_indoor)),
            "outdoor_equiv_np_watts": round(_normalize_watts(np_w or 0, is_indoor, ftp_outdoor, ftp_indoor)),
            "tss":                  round(t, 1) if t else None,
            "trainer":              is_indoor,
        }

    result = []
    for day_name in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
        planned     = planned_sessions.get(day_name)
        target_date = day_name_to_date[day_name]
        actual_ride = assignments.get(day_name)
        if actual_ride and actual_ride.get("_force_miss"):
            actual_ride = None  # explicitly missed — don't fall through to unplanned
        elif actual_ride is None:
            actual_ride = unplanned_by_day.get(day_name)
        actual = _make_actual(actual_ride) if actual_ride else None

        # Determine status
        status = "rest"
        if planned and planned.get("type") != "rest":
            if actual is None:
                status = "missed"
            else:
                lo, hi = None, None
                r = planned.get("target_watts_range")
                if r and len(r) == 2:
                    lo, hi = r
                w = actual.get("outdoor_equiv_watts") or 0
                if lo and hi and w > 0:
                    if lo <= w <= hi * 1.1:
                        status = "hit"
                    elif w >= lo * 0.9:
                        status = "partial"
                    else:
                        status = "miss"
                else:
                    status = "done"
        elif planned is None and actual:
            status = "unplanned"

        result.append({
            "day":     day_name,
            "date":    target_date.isoformat(),
            "planned": planned,
            "actual":  actual,
            "status":  status,
        })

    return result


# ---------------------------------------------------------------------------
# Power curve: best rolling average at 1/5/10/20-min durations
# ---------------------------------------------------------------------------

_STREAM_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_MAX_UNCACHED_FETCHES = 20

_INTERVAL_TYPES = {"vo2max", "anaerobic", "sweet_spot", "tempo", "threshold"}
_Z2_TYPES = {"z2", "z1"}


def collect_ride_descriptions(activities, fetch_description_fn, days=14, max_fetches=10):
    """
    Fetch descriptions for rides in the past N days.
    Returns list of {"date", "ride_name", "description"} dicts, sorted by date asc.
    Caps uncached API calls at max_fetches.
    """
    today = date.today()
    cutoff = today - timedelta(days=days)

    recent_rides = []
    for a in activities:
        if a.get("type") != "Ride":
            continue
        try:
            rd = date.fromisoformat(a.get("start_date_local", "")[:10])
        except ValueError:
            continue
        if rd >= cutoff:
            recent_rides.append((rd, a))

    cached, uncached = [], []
    for rd, ride in recent_rides:
        cache_file = os.path.join(_STREAM_CACHE_DIR, f"detail_{ride['id']}.json")
        if os.path.exists(cache_file):
            cached.append((rd, ride))
        else:
            uncached.append((rd, ride))

    results = []
    uncached_fetches = 0

    for rd, ride in cached + uncached:
        if not os.path.exists(os.path.join(_STREAM_CACHE_DIR, f"detail_{ride['id']}.json")):
            if uncached_fetches >= max_fetches:
                continue
            uncached_fetches += 1

        desc = fetch_description_fn(ride["id"])
        if not desc:
            continue
        results.append({
            "activity_id":  ride["id"],
            "date":         rd.isoformat(),
            "ride_name":    ride.get("name", ""),
            "description":  desc,
        })

    results.sort(key=lambda x: x["date"])
    return results


def detect_intervals(watts_arr, threshold_watts, min_duration_s=60, gap_tolerance_s=8):
    """
    Find sustained effort blocks in a power stream.

    Detection: state machine on 30s smoothed signal crossing threshold_watts.
    Pass threshold_watts = ftp * 0.88 so all Z4+ efforts are found regardless
    of the session's planned target (avoids missing intervals in multi-set sessions
    where secondary sets have lower targets).

    Boundary refinement: for each rough crossing point, walks to the midpoint of
    the transition ramp in the 5s smooth signal, giving accurate start/end times
    for downstream HR analysis (hr_at_60s, ramp_rate, recovery windows).

    Returns list of {"start_s", "duration_s", "avg_watts", "peak_watts"}.
    """
    if len(watts_arr) < 30:
        return []

    n = len(watts_arr)
    smooth30 = np.convolve(watts_arr, np.ones(30) / 30, mode="same")
    smooth5  = np.convolve(watts_arr, np.ones(5)  / 5,  mode="same")

    # Pass 1: state machine with gap tolerance — find crossings of threshold in 30s smooth.
    # gap_tolerance_s prevents brief mid-interval dips (power variation, corner, traffic)
    # from prematurely closing an interval.
    # NOTE: min_duration_s is applied AFTER boundary refinement (pass 2), not here,
    # because 30s smoothing lags onsets by ~15s — a real 60s effort may look like 45-55s
    # in smooth30 and would be incorrectly discarded if filtered at this stage.
    state = "rest"
    raw_efforts = []
    up_i = 0
    gap_count = 0
    for i in range(1, n):
        if state == "rest":
            if smooth30[i] >= threshold_watts:
                up_i = i
                state = "effort"
                gap_count = 0
        else:  # state == "effort"
            if smooth30[i] < threshold_watts:
                gap_count += 1
                if gap_count > gap_tolerance_s:
                    raw_efforts.append((up_i, i - gap_count))
                    state = "rest"
                    gap_count = 0
            else:
                gap_count = 0
    if state == "effort":
        raw_efforts.append((up_i, n - gap_count))

    # Pass 2: refine each boundary to the midpoint of the transition ramp in smooth5
    efforts = []
    for rough_start, rough_end in raw_efforts:
        # Refine start: midpoint between rest level and effort level
        r_lvl = float(np.mean(smooth5[max(0, rough_start - 15):max(1, rough_start - 2)]))
        e_lvl = float(np.mean(smooth5[rough_start + 2:min(n, rough_start + 20)]))
        mid_up = (r_lvl + e_lvl) / 2
        true_start = rough_start
        for i in range(rough_start - 1, max(0, rough_start - 25), -1):
            if smooth5[i] <= mid_up:
                true_start = i + 1
                break

        # Refine end: midpoint between effort level and rest level
        e_lvl2 = float(np.mean(smooth5[max(0, rough_end - 20):max(1, rough_end - 2)]))
        r_lvl2 = float(np.mean(smooth5[min(n-1, rough_end + 2):min(n, rough_end + 20)]))
        mid_dn = (e_lvl2 + r_lvl2) / 2
        true_end = rough_end
        for i in range(rough_end, min(n, rough_end + 25)):
            if smooth5[i] <= mid_dn:
                true_end = i
                break

        duration = true_end - true_start
        if duration >= min_duration_s:
            segment = watts_arr[true_start:true_end]
            efforts.append({
                "start_s":    true_start,
                "duration_s": duration,
                "avg_watts":  round(float(np.mean(segment))),
                "peak_watts": round(float(np.max(segment))),
            })

    return efforts


def _detect_main_block(watts_arr, lo, session=None):
    """
    Determine the main-block slice of a Z2/Z1 ride, excluding warmup and cooldown.

    Priority:
    1. Dynamic: 60s rolling average >= lo*0.90, sustained for >=120s continuously.
       Warmup spin-ups (15-30s) briefly enter the zone but don't sustain — filtered out.
       Variable cooldowns (including abrupt stops) are handled naturally.
    2. Structured warmup_min/cooldown_min on the session object (if dynamic detection fails,
       e.g. rolling avg never enters zone because indoor FTP scaling mismatch).
    3. Fallback: 5-min heuristic.

    Returns the main-block numpy slice.
    """
    n = len(watts_arr)
    threshold = lo * 0.90
    win = 60
    sustained = 120

    # 60s rolling average
    rolling = np.convolve(watts_arr, np.ones(win) / win, mode='same')
    above = rolling >= threshold  # boolean array

    # Find all contiguous above-threshold runs of >= sustained seconds
    main_start = None
    main_end = None
    i = 0
    while i < n:
        if above[i]:
            run_start = i
            while i < n and above[i]:
                i += 1
            run_end = i  # exclusive
            if (run_end - run_start) >= sustained:
                if main_start is None:
                    main_start = run_start
                main_end = run_end
        else:
            i += 1

    if main_start is not None and main_end is not None and main_end > main_start + 60:
        return watts_arr[main_start:main_end]

    # Fallback 1: structured fields
    if session:
        wm = session.get("warmup_min")
        cm = session.get("cooldown_min")
        if wm is not None and cm is not None:
            ws, cs = int(wm) * 60, int(cm) * 60
            if n > ws + cs + 60:
                return watts_arr[ws:n - cs]

    # Fallback 2: 5-min heuristic
    ws, cs = 300, 300
    if n > ws + cs + 60:
        return watts_arr[ws:n - cs]
    return watts_arr


def interval_achievement(session_comparison, fetch_stream_fn, ftp_outdoor, ftp_indoor=275,
                         fetch_hr_fn=None, fetch_laps_fn=None, max_hr_ever=None):
    """
    Annotates each matched session with power analysis from the stream.

    Interval sessions (vo2max/anaerobic/sweet_spot/tempo):
      Detects efforts using FTP floor, matches each to its planned set by duration,
      optionally enriches with HR metrics if fetch_hr_fn provided.
      Stores interval_stats (for email display) and interval_db_rows (for DB persistence).

    Z2/Z1 sessions:
      Computes time-in-zone breakdown (below/in/above), stores zone_stats.

    Indoor rides are normalized to outdoor-equivalent watts before analysis.
    """
    augmented = []
    for day in session_comparison:
        day = dict(day)
        p = day.get("planned") or {}
        a = day.get("actual")
        session_type = p.get("type", "")

        if (session_type in _INTERVAL_TYPES
                and p.get("target_watts", 0) > 0
                and a is not None
                and a.get("id")):
            watts = fetch_stream_fn(a["id"])
            if watts:
                watts_arr = np.array(watts, dtype=float)
                if a.get("trainer"):
                    watts_arr = watts_arr * (ftp_outdoor / ftp_indoor)

                threshold = ftp_outdoor * 0.88

                # Use Strava lap data as source of truth for outdoor rides.
                # Laps are athlete-defined boundaries — more accurate than power-stream detection.
                # Falls back to detect_intervals for indoor rides or un-cached activities.
                laps = fetch_laps_fn(a["id"]) if fetch_laps_fn else []
                if laps and not a.get("trainer"):
                    efforts = []
                    for lap in laps:
                        avg_w = lap.get("average_watts") or 0
                        moving_t = lap.get("moving_time") or lap.get("elapsed_time") or 0
                        elapsed_t = lap.get("elapsed_time") or moving_t
                        if avg_w >= threshold and moving_t >= 30:
                            efforts.append({
                                "start_s":    None,
                                "duration_s": elapsed_t,
                                "avg_watts":  round(float(avg_w)),
                                "peak_watts": round(float(lap.get("max_watts") or avg_w)),
                            })
                else:
                    efforts = detect_intervals(watts_arr, threshold_watts=threshold)
                if efforts:
                    interval_sets = p.get("interval_sets") or []
                    default_target = p["target_watts"]

                    for e in efforts:
                        if interval_sets:
                            best_set = min(
                                interval_sets,
                                key=lambda s: abs(s["duration_s"] - e["duration_s"])
                            )
                            e["target_watts"] = best_set["target_watts"]
                            e["set_label"] = (
                                f'{best_set["count"]}×{best_set["duration_s"]//60}min'
                                if best_set["duration_s"] >= 60
                                else f'{best_set["count"]}×{best_set["duration_s"]}s'
                            )
                        else:
                            e["target_watts"] = default_target
                            e["set_label"] = None

                    # Enrich with HR metrics if HR stream available.
                    # Lap-sourced efforts have start_s=None — HR indexing is skipped for those.
                    hr_stream = fetch_hr_fn(a["id"]) if fetch_hr_fn else None
                    if hr_stream:
                        stream_len = min(len(watts_arr), len(hr_stream))
                        hr_arr = np.array(hr_stream[:stream_len], dtype=float)
                        for e in efforts:
                            s_idx = e.get("start_s")
                            if s_idx is None:
                                continue  # lap-sourced: no stream offset available
                            e_idx = s_idx + e["duration_s"]
                            if e_idx > stream_len:
                                continue
                            interval_hr = hr_arr[s_idx:e_idx]
                            if len(interval_hr) < 10:
                                continue
                            peak_hr = int(np.max(interval_hr))
                            e["peak_hr"]  = peak_hr
                            e["hr_at_60s"] = int(interval_hr[min(60, len(interval_hr)-1)])
                            e["ramp_rate"] = round(float(
                                interval_hr[min(60, len(interval_hr)-1)] - interval_hr[0]
                            ), 1)
                            rec_start = e_idx
                            e["recovery_60s"]  = (int(peak_hr - hr_arr[rec_start + 60])
                                                   if rec_start + 60 < stream_len else None)
                            e["recovery_120s"] = (int(peak_hr - hr_arr[rec_start + 120])
                                                   if rec_start + 120 < stream_len else None)
                            e["hr_plateau_pct"] = (round(peak_hr / max_hr_ever * 100, 1)
                                                    if max_hr_ever else None)

                    # Build DB rows with all fields
                    act_date = day.get("date") or a.get("start_date_local", "")[:10] or None
                    db_rows = []
                    for i, e in enumerate(efforts, 1):
                        db_rows.append({
                            "activity_id":   a["id"],
                            "activity_date": act_date,
                            "interval_num":  i,
                            "set_label":     e.get("set_label"),
                            "start_s":       e["start_s"],
                            "duration_s":    e["duration_s"],
                            "avg_watts":     e["avg_watts"],
                            "target_watts":  e.get("target_watts"),
                            "peak_watts":    e.get("peak_watts"),
                            "peak_hr":       e.get("peak_hr"),
                            "hr_at_60s":     e.get("hr_at_60s"),
                            "ramp_rate":     e.get("ramp_rate"),
                            "recovery_60s":  e.get("recovery_60s"),
                            "recovery_120s": e.get("recovery_120s"),
                            "hr_plateau_pct": e.get("hr_plateau_pct"),
                        })

                    avg_interval_watts = round(
                        sum(e["avg_watts"] for e in efforts) / len(efforts)
                    )
                    pct_delta = round(
                        (avg_interval_watts - default_target) / default_target * 100
                    )
                    actual = dict(a)
                    actual["interval_stats"] = {
                        "efforts_detected":   len(efforts),
                        "avg_interval_watts": avg_interval_watts,
                        "target_watts":       default_target,
                        "pct_delta":          pct_delta,
                        "efforts":            efforts,
                    }
                    actual["interval_db_rows"] = db_rows
                    day["actual"] = actual

        elif (session_type in _Z2_TYPES
              and a is not None
              and a.get("id")):
            r = p.get("target_watts_range")
            if r and len(r) == 2:
                lo, hi = r
                watts = fetch_stream_fn(a["id"])
                if watts:
                    watts_arr = np.array(watts, dtype=float)
                    if a.get("trainer"):
                        watts_arr = watts_arr * (ftp_outdoor / ftp_indoor)

                    n = len(watts_arr)
                    main_block = _detect_main_block(watts_arr, lo, session=p)
                    mb_len = len(main_block)
                    main_block_avg = round(float(np.mean(main_block))) if mb_len > 0 else None

                    below   = int(np.sum(main_block < lo))
                    in_zone = int(np.sum((main_block >= lo) & (main_block <= hi)))
                    above   = int(np.sum(main_block > hi))

                    actual = dict(a)
                    actual["zone_stats"] = {
                        "zone_lo":      lo,
                        "zone_hi":      hi,
                        "total_s":      mb_len,
                        "in_zone_s":    in_zone,
                        "below_s":      below,
                        "above_s":      above,
                        "pct_in_zone":  round(in_zone / mb_len * 100) if mb_len > 0 else 0,
                        "main_block_avg": main_block_avg,
                    }
                    day["actual"] = actual

        augmented.append(day)
    return augmented


def recompute_status(session_comparison):
    """
    Second-pass status computation after interval_achievement has augmented the data.

    Interval sessions (vo2max, anaerobic, sweet_spot, tempo):
      - If interval_stats present: compare avg_interval_watts vs target_watts_range
      - If no interval_stats: use np_watts (better proxy than avg_watts)
    Non-interval sessions (z2, z1, rest): keep using avg_watts — whole ride should be in zone.

    Thresholds:
      hit:     lo <= watts <= hi * 1.1
      partial: watts >= lo * 0.90
      miss:    below 90% of lo
    """
    result = []
    for day in session_comparison:
        day = dict(day)
        p = day.get("planned") or {}
        a = day.get("actual")
        session_type = p.get("type", "")

        if session_type in _INTERVAL_TYPES and a is not None:
            lo, hi = None, None
            r = p.get("target_watts_range")
            if r and len(r) == 2:
                lo, hi = r

            if lo and hi:
                istats = a.get("interval_stats")
                if istats:
                    # Already in outdoor-equivalent watts (stream was normalized in interval_achievement)
                    w = istats.get("avg_interval_watts", 0)
                else:
                    # Fall back to normalized NP/avg
                    w = a.get("outdoor_equiv_np_watts") or a.get("outdoor_equiv_watts") or 0

                if w > 0:
                    if lo <= w <= hi * 1.1:
                        day["status"] = "hit"
                    elif w >= lo * 0.90:
                        day["status"] = "partial"
                    else:
                        day["status"] = "miss"

        result.append(day)
    return result


def power_curve(activities, fetch_stream_fn, weeks=8,
                durations=None, ftp_outdoor=323, ftp_indoor=275):
    """
    Compute best rolling average power at each duration for outdoor rides.

    Returns dict: {duration_s: {"best": watts_or_None, "best_4wk_ago": watts_or_None}}
    Calls fetch_stream_fn(activity_id) for each ride — cached calls are free,
    new API calls are capped at 20 per run.
    """
    if durations is None:
        durations = [60, 300, 600, 1200]  # 1/5/10/20-min

    today = date.today()
    cutoff    = today - timedelta(weeks=weeks)
    cutoff_4wk = today - timedelta(weeks=4)

    # Collect outdoor rides within the window, sorted newest first
    outdoor_rides = []
    for a in activities:
        if a.get("type") != "Ride":
            continue
        if a.get("trainer", False):
            continue
        try:
            rd = date.fromisoformat(a.get("start_date_local", "")[:10])
        except ValueError:
            continue
        if rd >= cutoff:
            outdoor_rides.append((rd, a))

    outdoor_rides.sort(key=lambda x: x[0], reverse=True)

    best_overall = {d: 0.0 for d in durations}
    best_4wk_ago = {d: 0.0 for d in durations}
    uncached_fetches = 0

    for rd, ride in outdoor_rides:
        if ride.get("moving_time", 0) < min(durations):
            continue

        # Rate limit guard: only cap *new* (uncached) fetches
        cache_file = os.path.join(_STREAM_CACHE_DIR, f"stream_{ride['id']}.json")
        is_cached = os.path.exists(cache_file)
        if not is_cached:
            if uncached_fetches >= _MAX_UNCACHED_FETCHES:
                continue
            uncached_fetches += 1

        watts = fetch_stream_fn(ride["id"])
        if not watts:
            continue

        watts_arr = np.array(watts, dtype=float)

        for d in durations:
            if len(watts_arr) < d:
                continue
            kernel  = np.ones(d) / d
            rolling = np.convolve(watts_arr, kernel, mode="valid")
            best    = float(rolling.max())

            if best > best_overall[d]:
                best_overall[d] = best
            if rd < cutoff_4wk and best > best_4wk_ago[d]:
                best_4wk_ago[d] = best

    result = {}
    for d in durations:
        result[d] = {
            "best":        round(best_overall[d]) if best_overall[d] > 0 else None,
            "best_4wk_ago": round(best_4wk_ago[d]) if best_4wk_ago[d] > 0 else None,
        }
    return result


# ---------------------------------------------------------------------------
# Extended power curve + CP model (clean-window, multi-duration)
# ---------------------------------------------------------------------------

_EXTENDED_DURATIONS = [60, 120, 180, 300, 480, 600, 720, 900, 1200]


def _is_clean_window(watts_slice, ftp,
                     dead_floor_pct=0.25, max_dead_frac=0.05,
                     coherence_pct=0.70, coherence_window_s=15):
    """
    Return True if this power window is clean enough to use as a CP model anchor.

    Layer 1 — hard stops: any second below ftp * dead_floor_pct counts as 'dead'.
    If more than max_dead_frac of the window is dead → reject.

    Layer 2 — coherence: no 15-second sub-window should drop below coherence_pct
    of the overall window mean. Catches 'sprint then collapse' windows.
    """
    dead_floor = ftp * dead_floor_pct
    n = len(watts_slice)

    # Layer 1
    dead_count = int(np.sum(watts_slice < dead_floor))
    if dead_count / n > max_dead_frac:
        return False

    # Layer 2
    window_mean = float(watts_slice.mean())
    if window_mean <= 0:
        return False
    coherence_floor = window_mean * coherence_pct
    step = coherence_window_s
    for start in range(0, n - step + 1, step):
        sub_mean = float(watts_slice[start:start + step].mean())
        if sub_mean < coherence_floor:
            return False

    return True


def _preceding_tss(watts_arr, idx, ftp):
    """
    Estimate TSS accumulated in the ride before the candidate window at index idx.
    Uses NP-based TSS on a 30-second rolling average of preceding watts.
    Returns 0.0 for windows near the start of a ride (< 30s of preceding data).
    """
    preceding = watts_arr[:idx]
    if len(preceding) < 30:
        return 0.0
    rolling = np.convolve(preceding, np.ones(30) / 30, mode="valid")
    np_watts = float(np.mean(rolling ** 4) ** 0.25)
    return (len(preceding) * np_watts ** 2) / (ftp ** 2 * 3600) * 100


def _hr_effort_score(hr_slice, duration_s, max_hr):
    """
    Duration-specific HR quality score for a clean power window.

    Returns float 0.2–1.2, or None if duration is too short for HR to be informative.
    - < 150s: return None (HR lag dominates too much of the window)
    - 150–480s: check fraction of last 40% above 88% max HR
    - > 480s: check fraction of last 40% above 85% max HR (threshold HR range)
    - HR absent: returns None (caller defaults to 0.5)
    """
    if duration_s < 150:
        return None
    if hr_slice is None or len(hr_slice) == 0:
        return None

    last_40_start = int(len(hr_slice) * 0.60)
    last_40 = hr_slice[last_40_start:]
    if len(last_40) == 0:
        return None

    threshold = max_hr * (0.88 if duration_s <= 480 else 0.85)
    frac_above = float(np.sum(last_40 >= threshold) / len(last_40))

    if frac_above >= 0.6:
        return 1.1
    elif frac_above >= 0.3:
        return 0.7
    else:
        return 0.3


def _max_effort_likelihood(watts_arr, hr_arr, idx, duration_s, ftp, max_hr):
    """
    Combine HR effort score and preceding-TSS fatigue penalty into a single 0.0–1.0
    likelihood that this clean window represents a genuine near-max effort.

    HR score (duration-adjusted) × TSS fatigue multiplier, clamped to [0, 1].
    """
    tss = _preceding_tss(watts_arr, idx, ftp)
    if tss < 30:
        tss_mult = 1.00
    elif tss < 60:
        tss_mult = 0.85
    elif tss < 90:
        tss_mult = 0.65
    else:
        tss_mult = 0.45

    hr_slice = hr_arr[idx:idx + duration_s] if hr_arr is not None else None
    hr_score = _hr_effort_score(hr_slice, duration_s, max_hr)
    if hr_score is None:
        hr_score = 0.5  # unknown — don't penalise

    return min(1.0, hr_score * tss_mult)


def power_curve_extended(activities, fetch_stream_fn, ftp_outdoor, weeks=8,
                         fetch_hr_fn=None, max_hr=193):
    """
    Compute best *clean* rolling-average power at each duration for outdoor rides.

    Uses a wider set of durations than power_curve() and applies _is_clean_window()
    before accepting a candidate as the best at that duration. Only clean windows
    (no hard stops, no large mid-window power collapses) are recorded.

    Tracks two candidates per duration:
    - best_display: highest raw watts (used for email/DB display)
    - best_model:   highest watts × likelihood (used as CP model anchor)

    Returns dict: {duration_s: {"watts": int|None, "model_watts": int|None, "likelihood": float|None}}
    Streams are expected to already be cached from the power_curve() call that runs
    first in main.py, so this is computationally free.
    """
    today = date.today()
    cutoff = today - timedelta(weeks=weeks)

    outdoor_rides = []
    for a in activities:
        if a.get("type") != "Ride":
            continue
        if a.get("trainer", False):
            continue
        try:
            rd = date.fromisoformat(a.get("start_date_local", "")[:10])
        except ValueError:
            continue
        if rd >= cutoff:
            outdoor_rides.append((rd, a))

    outdoor_rides.sort(key=lambda x: x[0], reverse=True)

    # Per-duration tracking: display best (raw watts) and model best (watts × likelihood)
    best_display = {d: 0.0 for d in _EXTENDED_DURATIONS}
    best_model_score = {d: 0.0 for d in _EXTENDED_DURATIONS}  # watts × likelihood
    best_model_watts = {d: 0.0 for d in _EXTENDED_DURATIONS}
    best_likelihood = {d: None for d in _EXTENDED_DURATIONS}
    uncached_fetches = 0

    for _rd, ride in outdoor_rides:
        if ride.get("moving_time", 0) < min(_EXTENDED_DURATIONS):
            continue

        cache_file = os.path.join(_STREAM_CACHE_DIR, f"stream_{ride['id']}.json")
        is_cached = os.path.exists(cache_file)
        if not is_cached:
            if uncached_fetches >= _MAX_UNCACHED_FETCHES:
                continue
            uncached_fetches += 1

        watts = fetch_stream_fn(ride["id"])
        if not watts:
            continue

        watts_arr = np.array(watts, dtype=float)

        # Fetch HR stream for this ride (cached; None if unavailable)
        hr_arr = None
        if fetch_hr_fn is not None:
            try:
                hr_raw = fetch_hr_fn(ride["id"])
                if hr_raw:
                    hr_raw_arr = np.array(hr_raw, dtype=float)
                    # Align lengths (Strava streams should match but guard edge cases)
                    min_len = min(len(watts_arr), len(hr_raw_arr))
                    hr_arr = hr_raw_arr[:min_len]
                    watts_arr = watts_arr[:min_len]
            except Exception:
                pass

        for d in _EXTENDED_DURATIONS:
            if len(watts_arr) < d:
                continue
            kernel  = np.ones(d) / d
            rolling = np.convolve(watts_arr, kernel, mode="valid")

            # Scan all clean windows; track best by raw watts and best by watts×likelihood
            sorted_idx = np.argsort(rolling)[::-1]
            for idx in sorted_idx[:20]:  # check top-20 candidates only
                candidate_w = float(rolling[idx])
                window_slice = watts_arr[idx:idx + d]
                if not _is_clean_window(window_slice, ftp_outdoor):
                    continue

                # Display best: highest raw clean watts
                if candidate_w > best_display[d]:
                    best_display[d] = candidate_w

                # Model best: highest watts × likelihood
                likelihood = _max_effort_likelihood(
                    watts_arr, hr_arr, int(idx), d, ftp_outdoor, max_hr
                )
                score = candidate_w * likelihood
                if score > best_model_score[d]:
                    best_model_score[d] = score
                    best_model_watts[d] = candidate_w
                    best_likelihood[d] = likelihood

    result = {}
    for d in _EXTENDED_DURATIONS:
        result[d] = {
            "watts":       round(best_display[d]) if best_display[d] > 0 else None,
            "model_watts": round(best_model_watts[d]) if best_model_watts[d] > 0 else None,
            "likelihood":  round(best_likelihood[d], 3) if best_likelihood[d] is not None else None,
        }
    return result


def fit_cp_model(clean_curve):
    """
    Fit a Critical Power model to clean power data from power_curve_extended().

    Tries the 3-parameter model P(t) = CP + W'/(t+k) first (corrects for curvature
    at short durations). Falls back to 2-parameter P(t) = CP + W'/t if 3-param
    fails to converge or produces a poor fit.

    Requires ≥3 anchor points spanning a duration ratio ≥3×. Returns None if
    insufficient data.

    Returns dict with cp_watts, w_prime_kj, model_type, r_squared, anchor_count,
    max_duration_used_s, anchors_only_short, est_20min, est_60min, confidence,
    and caveat string.
    """
    try:
        from scipy.optimize import curve_fit
        _scipy_available = True
    except ImportError:
        _scipy_available = False

    # Collect valid anchor points — use model_watts if available (weighted candidate),
    # fall back to watts (old flat-dict format from pre-Step-8 callers)
    anchors = []
    anchor_likelihoods = []
    for d, v in clean_curve.items():
        if isinstance(v, dict):
            w = v.get("model_watts") or v.get("watts")
            lk = v.get("likelihood")
        else:
            w = v  # legacy flat-dict format
            lk = None
        if w is not None and w > 0:
            anchors.append((d, w))
            anchor_likelihoods.append(lk)
    anchors = sorted(zip(anchors, anchor_likelihoods), key=lambda x: x[0][0])
    anchor_likelihoods = [x[1] for x in anchors]
    anchors = [x[0] for x in anchors]

    if len(anchors) < 3:
        return None

    t_arr = np.array([a[0] for a in anchors], dtype=float)
    p_arr = np.array([a[1] for a in anchors], dtype=float)

    duration_span = t_arr[-1] / t_arr[0]
    if duration_span < 3.0:
        return None  # anchors too clustered to constrain the hyperbola

    max_dur = int(t_arr[-1])
    anchors_only_short = max_dur < 600  # no anchor ≥ 10 min

    def _r_squared(p_measured, p_predicted):
        ss_res = np.sum((p_measured - p_predicted) ** 2)
        ss_tot = np.sum((p_measured - p_measured.mean()) ** 2)
        return float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Build likelihood weights (default 0.5 for unknown/missing HR)
    w_raw = np.array([lk if lk is not None else 0.5 for lk in anchor_likelihoods])
    w_sqrt = np.sqrt(w_raw)
    avg_anchor_likelihood = float(np.mean(w_raw))

    # --- 2-parameter baseline: P*t = CP*t + W' (weighted linearised) ---
    A = np.column_stack([t_arr, np.ones_like(t_arr)])
    b = p_arr * t_arr
    A_w = A * w_sqrt[:, None]
    b_w = b * w_sqrt
    result_2p, _, _, _ = np.linalg.lstsq(A_w, b_w, rcond=None)
    cp_2p = float(result_2p[0])
    w_prime_2p = float(result_2p[1])

    # Sanity clamp
    cp_2p = max(150.0, min(500.0, cp_2p))
    w_prime_2p = max(5000.0, min(80000.0, w_prime_2p))

    p_pred_2p = cp_2p + w_prime_2p / t_arr
    r2_2p = _r_squared(p_arr, p_pred_2p)

    # --- 3-parameter model: P(t) = CP + W'/(t+k) ---
    cp_final = cp_2p
    w_prime_final = w_prime_2p
    k_final = None
    r2_final = r2_2p
    model_type = "2param"

    if _scipy_available and len(anchors) >= 3:
        def _cp3(t, cp, wp, k):
            return cp + wp / (t + k)

        try:
            p0 = [cp_2p, w_prime_2p, 30.0]
            bounds = ([150, 5000, 0], [500, 80000, 300])
            sigma = 1.0 / w_sqrt  # lower sigma = higher confidence anchor
            popt, _ = curve_fit(_cp3, t_arr, p_arr, p0=p0, bounds=bounds,
                                sigma=sigma, absolute_sigma=False, maxfev=5000)
            cp_3p, w_prime_3p, k_3p = float(popt[0]), float(popt[1]), float(popt[2])
            p_pred_3p = _cp3(t_arr, cp_3p, w_prime_3p, k_3p)
            r2_3p = _r_squared(p_arr, p_pred_3p)

            # Accept 3-param if it's better and k is physically sensible
            if r2_3p >= 0.85 and r2_3p >= r2_2p - 0.01 and 0 <= k_3p <= 300:
                cp_final = cp_3p
                w_prime_final = w_prime_3p
                k_final = k_3p
                r2_final = r2_3p
                model_type = "3param"
        except Exception:
            pass  # curve_fit failed — stay with 2-param

    # Estimated power at 20-min and 60-min from the fitted model
    def _est(t_s):
        if model_type == "3param":
            return round(cp_final + w_prime_final / (t_s + k_final))
        return round(cp_final + w_prime_final / t_s)

    est_20min = _est(1200)
    est_60min = _est(3600)

    # Confidence scoring
    anchor_count = len(anchors)
    if anchor_count >= 5 and duration_span >= 5 and max_dur >= 600 and r2_final >= 0.95:
        confidence = "high"
    elif anchor_count >= 3 and duration_span >= 3 and r2_final >= 0.88:
        confidence = "moderate"
    else:
        confidence = "low"

    # Human-readable caveat
    caveat = None
    if anchors_only_short:
        caveat = (f"All clean anchors are <10min (longest: {max_dur // 60}min) — "
                  f"est_20min and est_60min are pure extrapolation with high uncertainty.")
    elif model_type == "2param":
        caveat = ("3-parameter fit did not converge — using 2-parameter model, which may "
                  "overestimate CP when short-duration anchors dominate.")
    elif confidence == "low":
        caveat = f"Low confidence: only {anchor_count} anchor(s), R²={r2_final:.2f}."

    return {
        "cp_watts":             round(cp_final),
        "w_prime_kj":           round(w_prime_final / 1000, 1),
        "k_constant":           round(k_final, 1) if k_final is not None else None,
        "model_type":           model_type,
        "r_squared":            round(r2_final, 3),
        "anchor_count":         anchor_count,
        "max_duration_used_s":  max_dur,
        "anchors_only_short":   anchors_only_short,
        "est_20min":            est_20min,
        "est_60min":            est_60min,
        "confidence":           confidence,
        "caveat":               caveat,
        "avg_anchor_likelihood": round(avg_anchor_likelihood, 3),
    }


# ---------------------------------------------------------------------------
# HR analysis for fatigue / fitness trends
# ---------------------------------------------------------------------------

def hr_analysis(activities, weeks=4, ftp_outdoor=310, ftp_indoor=275,
                peloton_factor=1.18):
    """
    Analyze HR patterns from summary data (no streams needed).

    Returns dict with power:HR ratio trend, HR elevation flag, and suffer scores.
    Indoor watts are normalized to outdoor-equivalent using the hardware device factor
    (peloton_factor), NOT the FTP ratio. This separates hardware calibration from
    fitness-derived ratios so the normalization remains stable as fitness changes.
    """
    today = date.today()
    cutoff_4wk = today - timedelta(weeks=weeks)
    cutoff_1wk = today - timedelta(weeks=1)

    def parse_date(r):
        try:
            return date.fromisoformat(r.get("start_date_local", "")[:10])
        except ValueError:
            return None

    rides_4wk = []
    rides_1wk = []
    for ride in activities:
        if ride.get("type") != "Ride":
            continue
        avg_w  = ride.get("average_watts") or 0
        avg_hr = ride.get("average_heartrate") or 0
        if avg_w <= 0 or avg_hr <= 0:
            continue
        rd = parse_date(ride)
        if rd is None:
            continue
        if rd >= cutoff_4wk:
            rides_4wk.append(ride)
        if rd >= cutoff_1wk:
            rides_1wk.append(ride)

    if not rides_4wk:
        return {}

    def phr(ride):
        w  = ride.get("average_watts") or 0
        hr = ride.get("average_heartrate") or 0
        if ride.get("trainer"):
            w = w * peloton_factor  # hardware calibration: Peloton reads ~18% low
        return round(w / hr, 3) if hr > 0 else None

    ratios_4wk = [r for r in (phr(ride) for ride in rides_4wk) if r]
    ratios_1wk = [r for r in (phr(ride) for ride in rides_1wk) if r]

    avg_ratio_4wk = round(sum(ratios_4wk) / len(ratios_4wk), 3) if ratios_4wk else None
    avg_ratio_1wk = round(sum(ratios_1wk) / len(ratios_1wk), 3) if ratios_1wk else None

    trend = "stable"
    if avg_ratio_4wk and avg_ratio_1wk:
        delta = (avg_ratio_1wk - avg_ratio_4wk) / avg_ratio_4wk
        if delta > 0.03:
            trend = "improving"
        elif delta < -0.03:
            trend = "declining"

    avg_hr_4wk = sum(r.get("average_heartrate", 0) for r in rides_4wk) / len(rides_4wk)
    avg_hr_1wk = (sum(r.get("average_heartrate", 0) for r in rides_1wk) / len(rides_1wk)
                  if rides_1wk else 0)
    hr_elevation_flag = bool(avg_hr_4wk > 0 and rides_1wk and avg_hr_1wk > avg_hr_4wk * 1.05)

    suffer_4wk  = [r.get("suffer_score") for r in rides_4wk if r.get("suffer_score")]
    suffer_1wk  = [r.get("suffer_score") for r in rides_1wk if r.get("suffer_score")]

    return {
        "power_hr_ratio_4wk_avg":    avg_ratio_4wk,
        "power_hr_ratio_last_week":  avg_ratio_1wk,
        "ratio_trend":               trend,
        "hr_elevation_flag":         hr_elevation_flag,
        "avg_hr_4wk":                round(avg_hr_4wk, 1) if avg_hr_4wk else None,
        "avg_hr_last_week":          round(avg_hr_1wk, 1) if rides_1wk else None,
        "avg_suffer_score_4wk":      round(sum(suffer_4wk) / len(suffer_4wk), 1) if suffer_4wk else None,
        "suffer_score_last_week":    round(sum(suffer_1wk) / len(suffer_1wk), 1) if suffer_1wk else None,
    }


def hr_drift_cp_inference(hr_at_power_by_week):
    """
    Estimate implied CP change from HR drift at fixed outdoor-equivalent power.

    Rule of thumb: ~1 BPM drop at near-threshold power ≈ ~1.5w CP gain.
    This is a rough heuristic calibrated against David's Apr 2026 data:
      HR at 378w: ~185→172 BPM over 3 weeks → implies +19w; CP model moved +17w ✓

    Returns dict or None if fewer than 2 weekly data points.
    """
    if not hr_at_power_by_week or len(hr_at_power_by_week) < 2:
        return None

    hrs = [r["avg_peak_hr"] for r in hr_at_power_by_week if r.get("avg_peak_hr")]
    if len(hrs) < 2:
        return None

    hr_drop = hrs[0] - hrs[-1]       # positive = HR fell over time = aerobic adaptation
    cp_change_w = round(hr_drop * 1.5, 0)

    try:
        from datetime import date as _date
        d0 = _date.fromisoformat(hr_at_power_by_week[0]["week_end"])
        d1 = _date.fromisoformat(hr_at_power_by_week[-1]["week_end"])
        span_days = (d1 - d0).days
    except Exception:
        span_days = None

    count = sum(r.get("interval_count", 0) for r in hr_at_power_by_week)
    n_weeks = len(hr_at_power_by_week)
    confidence = ("high"   if count >= 6 and n_weeks >= 3
                  else "medium" if count >= 3
                  else "low")

    avg_w = hr_at_power_by_week[0].get("avg_watts") or "?"
    return {
        "cp_change_w":        cp_change_w,
        "hr_drop_bpm":        round(hr_drop, 1),
        "confidence":         confidence,
        "sessions_span_days": span_days,
        "interval_count":     count,
        "note": (
            f"HR at {avg_w}w: {hrs[0]:.0f}→{hrs[-1]:.0f} BPM "
            f"over {n_weeks} weeks ({count} intervals)"
        ),
    }


def estimate_indoor_ftp(activities, fetch_stream_fn, peloton_factor=1.18, weeks=8):
    """
    Estimate current indoor FTP from best 4-min rolling power on Peloton rides.

    Method: best 4-min raw Peloton watts ÷ 1.15 (≈115% FTP for VO2max intervals)
    = estimated raw FTP. Multiply by peloton_factor for outdoor-equivalent.

    Returns dict or None if no indoor activity streams found.
    """
    from datetime import date as _date, timedelta
    cutoff = _date.today() - timedelta(weeks=weeks)
    best_4min_raw = None

    for a in activities:
        if not a.get("trainer", False):
            continue
        if a.get("type") != "Ride":
            continue
        rd_str = (a.get("start_date_local") or "")[:10]
        try:
            if _date.fromisoformat(rd_str) < cutoff:
                continue
        except ValueError:
            continue
        if (a.get("moving_time") or 0) < 300:
            continue
        try:
            stream = fetch_stream_fn(a["id"])
            if not isinstance(stream, list) or len(stream) < 240:
                continue
            arr = np.array(stream, dtype=float)
            arr = np.where(arr < 0, 0, arr)
            kernel = np.ones(240) / 240
            rolling = np.convolve(arr, kernel, mode="valid")
            peak = float(np.max(rolling))
            if best_4min_raw is None or peak > best_4min_raw:
                best_4min_raw = peak
        except Exception:
            continue

    if best_4min_raw is None or best_4min_raw < 100:
        return None

    ftp_raw   = round(best_4min_raw / 1.15)
    ftp_true  = round(ftp_raw * peloton_factor)
    return {
        "ftp_raw_w":        ftp_raw,
        "ftp_true_w":       ftp_true,
        "source_duration_s":  240,
        "source_watts_raw":   round(best_4min_raw),
        "confidence":         "medium",
        "note": (
            f"Best 4-min indoor: {round(best_4min_raw)}w raw "
            f"÷ 1.15 = {ftp_raw}w raw FTP "
            f"(= {ftp_true}w outdoor-equiv via {peloton_factor}× device factor)"
        ),
    }


# ---------------------------------------------------------------------------
# HR stream analysis: EF, decoupling, interval HR response
# ---------------------------------------------------------------------------

def _normalized_power(watts_arr):
    """30-second rolling average raised to 4th power, then mean, then 4th root."""
    n = len(watts_arr)
    if n < 30:
        return float(np.mean(watts_arr))
    kernel = np.ones(30) / 30
    rolling = np.convolve(watts_arr, kernel, mode="valid")
    return float(np.mean(rolling ** 4) ** 0.25)


def compute_ef(power_stream, hr_stream):
    """
    Efficiency Factor = Normalized Power / Avg HR.
    Returns float or None if streams are too short/empty.
    """
    if not power_stream or not hr_stream:
        return None
    min_len = min(len(power_stream), len(hr_stream))
    if min_len < 120:
        return None
    watts = np.array(power_stream[:min_len], dtype=float)
    hr = np.array(hr_stream[:min_len], dtype=float)
    avg_hr = float(np.mean(hr))
    if avg_hr <= 0:
        return None
    np_watts = _normalized_power(watts)
    return round(np_watts / avg_hr, 4)


def aerobic_decoupling(power_stream, hr_stream):
    """
    Aerobic decoupling = (EF_first_half - EF_second_half) / EF_first_half * 100.
    Only meaningful for Z2 rides > 60 min. Returns float (%) or None.
    Positive = cardiac drift (fatigue). Negative = improving within ride.
    """
    if not power_stream or not hr_stream:
        return None
    min_len = min(len(power_stream), len(hr_stream))
    if min_len < 3600:  # need at least 60 min
        return None
    mid = min_len // 2
    watts = np.array(power_stream[:min_len], dtype=float)
    hr = np.array(hr_stream[:min_len], dtype=float)

    def _ef_half(w, h):
        avg_h = float(np.mean(h))
        if avg_h <= 0:
            return None
        return _normalized_power(w) / avg_h

    ef1 = _ef_half(watts[:mid], hr[:mid])
    ef2 = _ef_half(watts[mid:], hr[mid:])
    if not ef1 or not ef2 or ef1 <= 0:
        return None
    return round((ef1 - ef2) / ef1 * 100, 2)


def interval_hr_analysis(power_stream, hr_stream, intervals, max_hr_ever=None):
    """
    For each detected interval (from detect_intervals()), extract HR metrics.

    Returns list of dicts with:
      peak_hr, hr_at_60s, ramp_rate (BPM/min), recovery_60s, recovery_120s,
      hr_plateau_pct (peak_hr / max_hr_ever * 100 if max_hr_ever provided)

    Also returns hr_floor_drift: max(rest_floor) - min(rest_floor) across all
    inter-interval rest windows (>15 BPM = within-session fatigue accumulation).
    """
    if not power_stream or not hr_stream or not intervals:
        return [], None

    stream_len = min(len(power_stream), len(hr_stream))
    hr = np.array(hr_stream[:stream_len], dtype=float)

    results = []
    rest_floors = []

    for i, effort in enumerate(intervals):
        start = effort["start_s"]
        end = start + effort["duration_s"]
        if end > stream_len:
            continue

        interval_hr = hr[start:end]
        if len(interval_hr) < 30:
            continue

        peak_hr = int(np.max(interval_hr))
        hr_at_60s = int(interval_hr[min(60, len(interval_hr) - 1)])
        ramp_rate = round(float(interval_hr[min(60, len(interval_hr) - 1)] - interval_hr[0]) / 1.0, 1)

        # HR recovery: drop in 60s and 120s post-effort
        rec_start = end
        recovery_60s = recovery_120s = None
        if rec_start + 60 < stream_len:
            recovery_60s = int(peak_hr - hr[rec_start + 60])
        if rec_start + 120 < stream_len:
            recovery_120s = int(peak_hr - hr[rec_start + 120])

        hr_plateau_pct = None
        if max_hr_ever and max_hr_ever > 0:
            hr_plateau_pct = round(peak_hr / max_hr_ever * 100, 1)

        results.append({
            "interval_num":  i + 1,
            "avg_watts":     effort["avg_watts"],
            "peak_hr":       peak_hr,
            "hr_at_60s":     hr_at_60s,
            "ramp_rate":     ramp_rate,
            "recovery_60s":  recovery_60s,
            "recovery_120s": recovery_120s,
            "hr_plateau_pct": hr_plateau_pct,
        })

        # Track HR floor in rest window before next interval
        if i + 1 < len(intervals):
            next_start = intervals[i + 1]["start_s"]
            rest_window = hr[end:next_start]
            if len(rest_window) > 10:
                rest_floors.append(float(np.min(rest_window)))

    # HR floor drift across session (rising floor = accumulating fatigue)
    hr_floor_drift = None
    if len(rest_floors) >= 2:
        hr_floor_drift = round(max(rest_floors) - min(rest_floors), 1)

    return results, hr_floor_drift


def compute_activity_metrics(activity, power_stream, hr_stream,
                              ftp_outdoor=323, ftp_indoor=275,
                              zone_ranges=None, max_hr_ever=None):
    """
    Compute EF, decoupling, and interval HR metrics for a single activity.

    Returns:
      activity_metrics dict (for activity_metrics table)
      interval_hr_list (for interval_hr_stats table, may be empty)
    """
    if not power_stream or not hr_stream:
        return None, []

    is_indoor = activity.get("trainer", False)
    # Normalize indoor watts to outdoor-equivalent
    watts = np.array(power_stream, dtype=float)
    if is_indoor and ftp_outdoor and ftp_indoor:
        watts = watts * (ftp_outdoor / ftp_indoor)

    min_len = min(len(watts), len(hr_stream))
    watts = watts[:min_len]
    hr_arr = np.array(hr_stream[:min_len], dtype=float)

    np_val = round(_normalized_power(watts))
    avg_hr = int(np.mean(hr_arr))
    max_hr = int(np.max(hr_arr))
    avg_watts = int(np.mean(watts))

    # EF
    ef = round(np_val / avg_hr, 4) if avg_hr > 0 else None

    # Decoupling — only for Z2/endurance rides > 60 min
    decoupling = None
    avg_power_pct = avg_watts / ftp_outdoor if ftp_outdoor else 0
    if min_len >= 3600 and 0.55 <= avg_power_pct <= 0.82:
        decoupling = aerobic_decoupling(list(watts), hr_stream[:min_len])

    # Interval HR — for interval-type rides (detect intervals above Z4)
    interval_hr_list = []
    threshold = ftp_outdoor * 0.88  # Z4 bottom (~88% FTP)
    if avg_power_pct >= 0.70 and min_len >= 600:
        intervals = detect_intervals(watts, threshold_watts=threshold)
        if intervals:
            hr_results, _ = interval_hr_analysis(
                list(watts), hr_stream[:min_len], intervals, max_hr_ever=max_hr_ever
            )
            for r in hr_results:
                r["activity_id"] = activity["id"]
            interval_hr_list = hr_results

    try:
        act_date = activity.get("start_date_local", "")[:10]
    except Exception:
        act_date = None

    act_metrics = {
        "activity_id":   activity["id"],
        "activity_date": act_date,
        "activity_type": "indoor" if is_indoor else "outdoor",
        "ef":            ef,
        "decoupling":    decoupling,
        "avg_hr":        avg_hr,
        "max_hr":        max_hr,
        "avg_watts":     avg_watts,
        "np":            np_val,
    }

    return act_metrics, interval_hr_list


# ---------------------------------------------------------------------------
# Segment discovery + tracking
# ---------------------------------------------------------------------------

def _format_db_results(db_rows):
    """Convert query_segment_history() row list to extract_ride_segments() dict format."""
    result = {}
    for row in db_rows:
        seg_id = row["segment_id"]
        result[seg_id] = {
            "segment_id":     seg_id,
            "segment_name":   row["segment_name"],
            "avg_grade":      row["avg_grade"],
            "distance_m":     row["distance_m"],
            "climb_category": 0,
            "efforts":        [],
            "best_effort": {
                "elapsed_time_s": row["best_elapsed_time_s"],
                "avg_watts":      row.get("best_avg_watts"),
                "date":           str(row.get("last_date", "")),
            },
            "effort_count":   row["effort_count"],
            "last_date":      str(row.get("last_date", "")),
            "best_kom_rank":  None,
        }
    return result


def extract_ride_segments(activities, fetch_segments_fn, weeks=8, max_uncached=20):
    """
    Extract segment efforts from recent outdoor rides.
    Hard filter: avg_grade >= 5%, elapsed_time 60–600s, not downhill.

    Returns dict keyed by segment_id with best effort, effort count, recency.
    Uses segment_history DB if populated (backfill); otherwise scans files.
    API calls for uncached rides capped at max_uncached.
    """
    import db as _db

    # DB-first: use backfilled history if available
    db_rows = _db.query_segment_history()
    if db_rows:
        return _format_db_results(db_rows)

    # Fallback: scan recent ride cache files
    today = date.today()
    cutoff = today - timedelta(weeks=weeks)

    outdoor_rides = []
    for a in activities:
        if a.get("type") != "Ride" or a.get("trainer", False):
            continue
        try:
            rd = date.fromisoformat(a.get("start_date_local", "")[:10])
        except ValueError:
            continue
        if rd >= cutoff:
            outdoor_rides.append((rd, a))

    # Process cached rides first to maximise coverage within the API cap
    cached_rides = [(rd, r) for rd, r in outdoor_rides
                    if os.path.exists(os.path.join(_STREAM_CACHE_DIR, f"detail_{r['id']}.json"))]
    uncached_rides = [(rd, r) for rd, r in outdoor_rides
                      if not os.path.exists(os.path.join(_STREAM_CACHE_DIR, f"detail_{r['id']}.json"))]
    # Sort each group by date desc
    cached_rides.sort(key=lambda x: x[0], reverse=True)
    uncached_rides.sort(key=lambda x: x[0], reverse=True)

    segments_by_id = {}
    uncached_fetches = 0

    for rd, ride in cached_rides + uncached_rides:
        is_cached = os.path.exists(os.path.join(_STREAM_CACHE_DIR, f"detail_{ride['id']}.json"))
        if not is_cached:
            if uncached_fetches >= max_uncached:
                continue
            uncached_fetches += 1

        efforts = fetch_segments_fn(ride["id"])
        if not efforts:
            continue

        for effort in efforts:
            seg = effort.get("segment", {})
            seg_id = seg.get("id")
            if not seg_id:
                continue

            avg_grade = seg.get("average_grade", 0)
            elapsed_time = effort.get("elapsed_time", 0)

            # Hard filter
            if avg_grade < 5 or avg_grade <= 0:
                continue
            if not (60 <= elapsed_time <= 600):
                continue

            entry = segments_by_id.get(seg_id, {
                "segment_id":     seg_id,
                "segment_name":   seg.get("name", ""),
                "avg_grade":      avg_grade,
                "distance_m":     seg.get("distance", 0),
                "climb_category": seg.get("climb_category", 0),
                "efforts":        [],
            })
            entry["efforts"].append({
                "date":           rd.isoformat(),
                "elapsed_time_s": elapsed_time,
                "avg_watts":      effort.get("average_watts"),
                "pr_rank":        effort.get("pr_rank"),
                "kom_rank":       effort.get("kom_rank"),
            })
            segments_by_id[seg_id] = entry

    # Summarize each segment
    for entry in segments_by_id.values():
        sorted_efforts = sorted(entry["efforts"], key=lambda e: e.get("elapsed_time_s", 9999))
        entry["best_effort"] = sorted_efforts[0] if sorted_efforts else None
        entry["effort_count"] = len(sorted_efforts)
        entry["last_date"] = max(e["date"] for e in sorted_efforts) if sorted_efforts else None
        kom_ranks = [e["kom_rank"] for e in sorted_efforts if e.get("kom_rank")]
        entry["best_kom_rank"] = min(kom_ranks) if kom_ranks else None

    return segments_by_id


def infer_climb_profile(starred_segments):
    """
    Compute p25/p75 of avg_grade and distance from starred segments.
    Returns {"grade_lo", "grade_hi", "dist_lo", "dist_hi"} or None if < 2 segments.
    """
    if not starred_segments or len(starred_segments) < 2:
        return None

    grades = sorted(s["avg_grade"] for s in starred_segments if s.get("avg_grade", 0) > 0)
    distances = sorted(s["distance"] for s in starred_segments if s.get("distance", 0) > 0)

    if len(grades) < 2:
        return None

    def percentile(data, pct):
        idx = (pct / 100) * (len(data) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(data) - 1)
        return data[lo] + (data[hi] - data[lo]) * (idx - lo)

    return {
        "grade_lo": round(percentile(grades, 25), 1),
        "grade_hi": round(percentile(grades, 75), 1),
        "dist_lo":  round(percentile(distances, 25)),
        "dist_hi":  round(percentile(distances, 75)),
    }


def estimate_achievability(pr_time_s, kom_time_s, total_athletes):
    """
    Score 1–10: how achievable is a top-10 KOM on this segment.

    Based on how far David's PR is from the KOM time:
      1.0x (tied)  → 10   1.2x behind → ~8   1.5x → ~6   2.0x+ → 1–2

    Adjusted ±1 for competition depth (athlete_count).
    Returns 5 (unknown) if times are unavailable.
    """
    if not pr_time_s or not kom_time_s or kom_time_s <= 0:
        return 5

    ratio = pr_time_s / kom_time_s  # 1.0 = tied KOM; 2.0 = twice as slow
    base = max(0, round(10 - (ratio - 1.0) * 12))

    if total_athletes and total_athletes > 5000:
        comp = -1
    elif total_athletes and total_athletes < 200:
        comp = 1
    else:
        comp = 0

    return max(1, min(10, base + comp))


def _interpolate_power(dur_s, power_curve_data):
    """
    Estimate power at dur_s via log-linear interpolation of the power curve.
    Uses curve points at 60/300/600/1200s.  Returns None if no data.
    """
    import math
    if not power_curve_data or not dur_s:
        return None
    points = [(d, (power_curve_data.get(d) or {}).get("best"))
              for d in [60, 300, 600, 1200]]
    points = [(d, p) for d, p in points if p]
    if not points:
        return None
    if len(points) == 1:
        return points[0][1]

    below = [(d, p) for d, p in points if d <= dur_s]
    above = [(d, p) for d, p in points if d > dur_s]

    if below and above:
        d1, p1 = max(below, key=lambda x: x[0])
        d2, p2 = min(above, key=lambda x: x[0])
        t = (math.log(dur_s) - math.log(d1)) / (math.log(d2) - math.log(d1))
        return round(p1 + t * (p2 - p1))
    return (max(below, key=lambda x: x[0])[1] if below
            else min(above, key=lambda x: x[0])[1])


def compute_power_phenotype(curve_data):
    """
    Classify athlete type from power-duration curve using ratio-based analysis.

    Two key ratios distinguish phenotype:
    - P60/P300 (anaerobic index): how much stronger is 1-min vs 5-min?
      > 1.25 = anaerobic/punch specialist; < 1.12 = aerobically oriented
    - P600/P1200 (threshold flatness): how little does power drop from 10→20min?
      > 0.94 = threshold specialist (very flat); < 0.90 = VO2max oriented (bigger drop)

    Args:
        curve_data: dict like {60: {"best": 400}, 300: {"best": 380}, ...}

    Returns dict:
        primary:                   "anaerobic" | "vo2max" | "threshold" | "all-rounder" | "unknown"
        secondary:                 secondary phenotype string or None
        best_kom_duration_range_s: [min_s, max_s]
        best_kom_label:            human-readable KOM target recommendation
        rationale:                 ratio breakdown string
        cp_estimate:               CP estimated from 5-min and 20-min power (or None)
        w_prime_kj:                W' estimated from CP model (or None)
    """
    p60   = (curve_data.get(60)   or {}).get("best")
    p300  = (curve_data.get(300)  or {}).get("best")
    p600  = (curve_data.get(600)  or {}).get("best")
    p1200 = (curve_data.get(1200) or {}).get("best")

    available = sum(1 for v in [p60, p300, p600, p1200] if v)
    if available < 3:
        return {
            "primary": "unknown",
            "secondary": None,
            "best_kom_duration_range_s": [180, 480],
            "best_kom_label": "VO2max climb (3-8min)",
            "rationale": "Insufficient power data for phenotype classification.",
            "cp_estimate": None,
            "w_prime_kj": None,
        }

    # Ratio 1: anaerobic index — 1-min relative to 5-min
    anaerobic_index = round(p60 / p300, 3) if p60 and p300 else None

    # Ratio 2: threshold flatness — 20-min relative to 10-min (0→1; closer to 1 = flatter = threshold)
    threshold_flatness = round(p1200 / p600, 3) if p600 and p1200 else None

    # CP model from 5-min and 20-min (informational, not used for classification)
    cp_est = None
    w_prime_kj = None
    if p300 and p1200:
        cp_raw = (p300 * 300 - p1200 * 1200) / (300 - 1200)
        cp_est = round(max(50, min(cp_raw, p300 * 0.98)))
        w_prime_kj = round(max(0, (p300 - cp_est) * 300) / 1000, 1)

    rationale_parts = []
    if anaerobic_index:
        rationale_parts.append(f"1-min/5-min ratio: {anaerobic_index:.2f}")
    if threshold_flatness:
        rationale_parts.append(f"20-min/10-min flatness: {threshold_flatness:.2f}")
    if cp_est:
        rationale_parts.append(f"CP≈{cp_est}w")
    if w_prime_kj:
        rationale_parts.append(f"W'≈{w_prime_kj}kJ")

    # Classification: anaerobic index takes priority; threshold flatness breaks ties
    secondary = None

    if anaerobic_index is not None and anaerobic_index > 1.25:
        # Strong 1-min vs 5-min: punch/sprint specialist
        primary = "anaerobic"
        range_s = [60, 180]
        label = "punch/sprint climb (<3min)"
        if threshold_flatness and threshold_flatness > 0.94:
            secondary = "threshold"

    elif anaerobic_index is not None and anaerobic_index > 1.18:
        # Moderate anaerobic capacity: good at short-mid VO2max durations too
        primary = "anaerobic"
        range_s = [60, 300]
        label = "punch or short VO2max climb (1-5min)"
        secondary = "vo2max"

    elif threshold_flatness is not None and threshold_flatness > 0.94:
        # Very flat 10→20min curve: threshold specialist
        primary = "threshold"
        range_s = [480, 1200]
        label = "threshold climb (8-20min)"
        if anaerobic_index and anaerobic_index > 1.15:
            secondary = "anaerobic"

    elif threshold_flatness is not None and threshold_flatness < 0.92:
        # Larger 10→20min drop (20-min is <92% of 10-min): VO2max phenotype
        primary = "vo2max"
        range_s = [180, 480]
        label = "VO2max climb (3-8min)"

    else:
        primary = "all-rounder"
        range_s = [180, 480]
        label = "VO2max climb (3-8min) — balanced, prioritize achievability"

    return {
        "primary": primary,
        "secondary": secondary,
        "best_kom_duration_range_s": range_s,
        "best_kom_label": label,
        "rationale": " | ".join(rationale_parts) if rationale_parts else "Insufficient data.",
        "cp_estimate": cp_est,
        "w_prime_kj": w_prime_kj,
    }


def estimate_climb_time(distance_m, avg_grade_pct, power_curve_data, weight_kg=77):
    """
    Estimate David's time on an unridden climb using simplified climb physics.
    Valid for grade >= 5% where gravity dominates (aero < 15% of power).
    Returns estimated seconds, or None if power curve data is insufficient.

    Model: v = P / (m * g * (grade + Crr))
    Iterates 3x to converge duration → power curve lookup → speed → duration.
    """
    if avg_grade_pct < 5:
        return None
    import math
    grade = avg_grade_pct / 100
    g, Crr = 9.81, 0.004
    duration_guess = 300
    for _ in range(3):
        watts = _interpolate_power(duration_guess, power_curve_data)
        if not watts:
            return None
        speed_ms = watts / (weight_kg * g * (grade + Crr))
        duration_guess = distance_m / speed_ms
    return round(duration_guess)


def compute_power_metric(elapsed_time_s, avg_watts, kom_time_s,
                         avg_grade, distance_m, power_curve_data, weight_kg=77):
    """
    Compute the 3-tier +% power needed to match KOM.

    Tier A (Exact):   has avg_watts + elapsed_time_s from ride history
    Tier B (PR):      has elapsed_time_s but no watts
    Tier C (Modeled): no PR; estimate via climb physics
    Returns (pct_power_increase, power_tier) or (None, None).
    Negative pct means David is faster than KOM.
    """
    if not kom_time_s or kom_time_s <= 0:
        return None, None

    # Tier A: actual ride data with power
    if elapsed_time_s and avg_watts:
        pct = round((elapsed_time_s / kom_time_s - 1) * 100, 1)
        return pct, "A"

    # Tier B: PR time available, no watts
    if elapsed_time_s:
        pct = round((elapsed_time_s / kom_time_s - 1) * 100, 1)
        return pct, "B"

    # Tier C: no time on file — use climb physics model
    if avg_grade and avg_grade >= 5 and distance_m:
        est_time = estimate_climb_time(distance_m, avg_grade, power_curve_data, weight_kg)
        if est_time:
            pct = round((est_time / kom_time_s - 1) * 100, 1)
            return pct, "C"

    return None, None


def haversine_km(lat1, lng1, lat2, lng2):
    """Approximate distance in km between two lat/lng points."""
    import math
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_segment_tracker(starred, ride_segments, fetch_leaderboard_fn, power_curve_data,
                          home_lat=None, home_lng=None, radius_km=80):
    """
    Build Tier 1 + 2 segment tracker.

    Tier 1: starred segments + ride segments with ≥3 efforts (full leaderboard fetch)
    Tier 2: ride segments with 1–2 efforts (use cached leaderboard only)

    Returns list of segment dicts (up to 5 Tier 1 + 1 Tier 2 opportunity).
    Leaderboard fetches capped at 15 per run.
    """
    weight_kg = load_rider_weight()

    # Apply home radius filter to starred segments (ride_segments are local by definition)
    def _is_local(seg):
        if not home_lat or not home_lng:
            return True
        latlng = seg.get("start_latlng")
        if not latlng or len(latlng) < 2:
            return True  # no coords → assume local
        return haversine_km(home_lat, home_lng, latlng[0], latlng[1]) <= radius_km

    starred_ids = {s["id"] for s in (starred or []) if _is_local(s)}

    tier1_ids = set(starred_ids)
    tier2_ids = set()

    for seg_id, entry in (ride_segments or {}).items():
        if entry["effort_count"] >= 3:
            tier1_ids.add(seg_id)
        elif 1 <= entry["effort_count"] <= 2:
            tier2_ids.add(seg_id)

    tier2_ids -= tier1_ids  # don't double-count

    leaderboard_fetches = 0
    MAX_FETCHES = 15
    result = []

    # --- Tier 1 ---
    for seg_id in tier1_ids:
        ride_entry = (ride_segments or {}).get(seg_id, {})
        starred_info = next((s for s in (starred or []) if s["id"] == seg_id), {})
        seg_name = ride_entry.get("segment_name") or starred_info.get("name", f"Segment {seg_id}")
        avg_grade = ride_entry.get("avg_grade") or starred_info.get("avg_grade", 0)
        distance_m = ride_entry.get("distance_m") or starred_info.get("distance", 0)

        stats = {}
        if leaderboard_fetches < MAX_FETCHES:
            stats = fetch_leaderboard_fn(seg_id) or {}
            leaderboard_fetches += 1

        pr_elapsed_time  = stats.get("pr_elapsed_time")
        kom_time_s       = stats.get("kom_time_s")
        total_athletes   = stats.get("total_athletes", 0)
        david_rank       = stats.get("david_rank") or ride_entry.get("best_kom_rank")

        # David's best effort from ride history (may be more granular than segment stats)
        best_effort    = ride_entry.get("best_effort") or {}
        ride_elapsed   = best_effort.get("elapsed_time_s")
        avg_watts      = best_effort.get("avg_watts")

        # Use ride history time if available, else fall back to segment stats PR
        elapsed_time_s = ride_elapsed or pr_elapsed_time

        # Time gap to KOM
        time_gap_s = ((elapsed_time_s - kom_time_s)
                      if elapsed_time_s and kom_time_s else None)

        # Power gap estimate (watts needed to close time gap on a climb)
        power_gap_w = None
        if elapsed_time_s and kom_time_s and elapsed_time_s > 0:
            watts_ref = avg_watts or _interpolate_power(elapsed_time_s, power_curve_data)
            if watts_ref:
                power_gap_w = max(0, round(watts_ref * (elapsed_time_s / max(kom_time_s, 1)) - watts_ref))

        pct_power_increase, power_tier = compute_power_metric(
            elapsed_time_s, avg_watts, kom_time_s,
            avg_grade, distance_m, power_curve_data, weight_kg
        )

        result.append({
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
        })

    # --- Tier 2 (cached leaderboard only, top 1 opportunity) ---
    tier2_candidates = []
    for seg_id in tier2_ids:
        ride_entry = (ride_segments or {}).get(seg_id, {})

        # Only use cached stats — no new API calls for tier 2
        stats = {}
        cache_path = os.path.join(_STREAM_CACHE_DIR, f"seg_stats_{seg_id}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cached = json.load(f)
            stats = cached.get("data", {})

        total_athletes = stats.get("total_athletes", 100)
        best_effort    = ride_entry.get("best_effort") or {}
        elapsed_time_s = best_effort.get("elapsed_time_s")
        avg_watts      = best_effort.get("avg_watts")
        kom_time_s     = stats.get("kom_time_s")
        avg_grade      = ride_entry.get("avg_grade")
        distance_m     = ride_entry.get("distance_m", 0)

        pct_power_increase, power_tier = compute_power_metric(
            elapsed_time_s, avg_watts, kom_time_s,
            avg_grade, distance_m, power_curve_data, weight_kg
        )

        tier2_candidates.append({
            "segment_id":         seg_id,
            "segment_name":       ride_entry.get("segment_name", f"Segment {seg_id}"),
            "avg_grade":          avg_grade,
            "elapsed_time_s":     elapsed_time_s,
            "avg_watts":          avg_watts,
            "kom_time_s":         kom_time_s,
            "time_gap_s":         ((elapsed_time_s - kom_time_s)
                                   if elapsed_time_s and kom_time_s else None),
            "total_athletes":     total_athletes,
            "tier":               2,
            "pct_power_increase": pct_power_increase,
            "power_tier":         power_tier,
            "effort_count":       ride_entry.get("effort_count", 0),
        })

    # Sort tier 1 by pct_power_increase asc (smallest gap = most achievable), cap at 5
    tier1_result = sorted(
        result,
        key=lambda s: s["pct_power_increase"] if s["pct_power_increase"] is not None else 9999
    )[:5]
    tier2_result = sorted(
        tier2_candidates,
        key=lambda s: s["pct_power_increase"] if s["pct_power_increase"] is not None else 9999
    )[:1]

    return tier1_result + tier2_result
