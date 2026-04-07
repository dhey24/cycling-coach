"""
power_audit.py — Per-ride interval diagnostics for calibration confidence.

Shows the full derivation chain for each ride in the past N weeks:
  - Which planned session it matched (or MISSED/UNPLANNED)
  - Power source: stream vs summary
  - Indoor scaling: raw → outdoor-equiv
  - For intensity sessions: per-interval breakdown
  - For Z2 sessions: time-in-zone breakdown

Usage:
    python power_audit.py            # last 2 weeks
    python power_audit.py --weeks=1  # current week only
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta

import numpy as np
from dotenv import load_dotenv

load_dotenv()

import strava_client
import metrics

INTERVAL_TYPES = {"vo2max", "anaerobic", "sweet_spot", "tempo"}
Z2_TYPES = {"z2", "z1"}

# Statuses
STATUS_LABELS = {
    "hit":         "HIT",
    "partial":     "PARTIAL",
    "miss":        "MISS",
    "overachieved": "OVERACHIEVED",
    "missed":      "MISSED",
    "unplanned":   "UNPLANNED",
    "rest":        "REST",
    "done":        "DONE",
}

DAY_ABBREVS = {
    "monday":    "MON",
    "tuesday":   "TUE",
    "wednesday": "WED",
    "thursday":  "THU",
    "friday":    "FRI",
    "saturday":  "SAT",
    "sunday":    "SUN",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Power audit: per-ride interval diagnostics")
    parser.add_argument("--weeks", type=int, default=2, help="Number of weeks to audit (default: 2)")
    return parser.parse_args()


def load_plan():
    plan_path = os.path.join(os.path.dirname(__file__), "data", "plan.json")
    if not os.path.exists(plan_path):
        return None
    with open(plan_path) as f:
        return json.load(f)


def get_week_offsets(weeks):
    """Return list of week_offset values. 1 = last week, 2 = two weeks ago, etc."""
    today = date.today()
    offsets = []
    for offset in range(1, weeks + 1):
        week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
        if week_start <= today:
            offsets.append(offset)
    # Also include current week (offset=0) if weeks >= 1 and we're past Monday
    # Plan: audit last N complete+partial weeks. offset=0 is the current week.
    # The plan asks for "last 2 weeks" to mean the 2 most recent (including current).
    offsets = list(range(0, weeks))
    return offsets


def fmt_duration(seconds):
    m = seconds // 60
    s = seconds % 60
    return f"{m}:{s:02d}"


def effort_symbol(avg_w, target_w):
    """✓ if >= target, ~ if >= target*0.95, ✗ otherwise."""
    if avg_w >= target_w:
        return "✓"
    elif avg_w >= target_w * 0.95:
        return "~"
    else:
        return "✗"


def compute_z2_status(time_in_zone_s, total_analysis_s, main_block_avg, target_w, lo, hi):
    """Determine Z2 hit/partial/miss/overachieved."""
    if total_analysis_s <= 0:
        return "miss"
    pct_in_zone = time_in_zone_s / total_analysis_s
    if pct_in_zone >= 0.80 and main_block_avg is not None and main_block_avg > target_w + 10:
        return "overachieved"
    elif pct_in_zone >= 0.65:
        return "hit"
    elif pct_in_zone >= 0.45:
        return "partial"
    else:
        return "miss"


def compute_interval_status(avg_interval_w, lo, hi, target_w):
    """Determine interval hit/partial/miss/overachieved."""
    if hi and avg_interval_w > hi * 1.1:
        return "overachieved"
    elif lo and hi and lo <= avg_interval_w <= hi * 1.1:
        return "hit"
    elif lo and avg_interval_w >= lo * 0.90:
        return "partial"
    else:
        return "miss"


def print_separator(char="─", width=72):
    print(char * width)


def print_day_header(day_name, day_date, planned, actual, base_status):
    day_abbr = DAY_ABBREVS.get(day_name, day_name[:3].upper())
    date_str = day_date  # "2026-03-30"

    if planned is None:
        if actual:
            print(f"\n{day_abbr} {date_str} → UNPLANNED RIDE")
        else:
            print(f"\n{day_abbr} {date_str} → (no plan, no ride)")
        return

    session_type = planned.get("type", "?").upper()
    target_w = planned.get("target_watts", 0)
    duration = planned.get("duration_min", 0)

    if session_type == "REST":
        if actual:
            print(f"\n{day_abbr} {date_str} → REST DAY (unplanned ride recorded)")
        else:
            print(f"\n{day_abbr} {date_str} → REST")
        return

    print(f"\n{day_abbr} {date_str} → {session_type} (target: {target_w}w, {duration}min)")


def print_no_ride(planned):
    if planned and planned.get("type") != "rest":
        print(f"  [MISSED — no matching ride found]  STATUS: MISSED")


def print_ride_header(actual, ftp_outdoor, ftp_indoor):
    """Print ride name, indoor/outdoor, duration, power source."""
    name = actual.get("name", "?")
    is_indoor = actual.get("trainer", False)
    duration = actual.get("duration_min", 0)
    ride_type = "INDOOR, Peloton" if is_indoor else "OUTDOOR"

    scale = ftp_outdoor / ftp_indoor if is_indoor else 1.0
    print(f"  Ride: \"{name}\" [{ride_type}] — {duration}min")

    # Power source
    avg_w = actual.get("avg_watts")
    np_w = actual.get("np_watts")

    # We don't have stream pts here — that info comes from the stream itself
    # Stream pts will be printed in the detailed section if available
    if avg_w:
        if is_indoor:
            equiv = round(avg_w * scale)
            np_equiv = round(np_w * scale) if np_w else None
            avg_str = f"avg={avg_w}w raw → {equiv}w outdoor-equiv"
            np_str = f"  NP={np_w}w raw → {np_equiv}w outdoor-equiv" if np_w and np_w != avg_w else ""
            print(f"  Summary: {avg_str}{np_str}")
        else:
            np_str = f"  NP={np_w}w" if np_w and np_w != avg_w else ""
            print(f"  Summary: avg={avg_w}w{np_str}")
    else:
        print(f"  Summary: no power data in summary")


def print_interval_analysis(actual, planned, ftp_outdoor, ftp_indoor, fetch_stream_fn):
    """Full interval breakdown for vo2max/anaerobic/sweet_spot/tempo sessions."""
    target_w = planned.get("target_watts", 0)
    lo, hi = None, None
    r = planned.get("target_watts_range")
    if r and len(r) == 2:
        lo, hi = r

    is_indoor = actual.get("trainer", False)
    scale = ftp_outdoor / ftp_indoor if is_indoor else 1.0
    threshold = target_w * 0.88

    print(f"  Interval threshold: {target_w}w × 0.88 = {round(threshold)}w")

    # Fetch stream
    activity_id = actual.get("id")
    watts_raw = fetch_stream_fn(activity_id) if activity_id else None

    if not watts_raw:
        # Fall back to summary stats
        w = actual.get("outdoor_equiv_np_watts") or actual.get("outdoor_equiv_watts") or 0
        print(f"  Power source: summary only (no stream)")
        if w > 0:
            status = compute_interval_status(w, lo, hi, target_w)
            pct = round((w - target_w) / target_w * 100) if target_w else 0
            sign = "+" if pct >= 0 else ""
            print(f"  Summary avg (outdoor-equiv): {w}w (target {target_w}w) → {sign}{pct}%  STATUS: {STATUS_LABELS.get(status, status.upper())}")
        else:
            print(f"  No power data available  STATUS: MISS")
        return

    watts_arr = np.array(watts_raw, dtype=float)
    stream_pts = len(watts_arr)

    # Apply indoor scaling
    if is_indoor:
        watts_arr_scaled = watts_arr * scale
        print(f"  Power source: stream ({stream_pts:,} pts) → scaled ×{scale:.3f}")
    else:
        watts_arr_scaled = watts_arr
        print(f"  Power source: stream ({stream_pts:,} pts)")

    # Summary from stream
    stream_avg = round(float(np.mean(watts_arr_scaled)))
    np_arr = np.convolve(watts_arr_scaled, np.ones(30) / 30, mode="same")
    stream_np = round(float(np.mean(np_arr)))
    print(f"  Summary: avg={stream_avg}w  NP≈{stream_np}w")

    # Detect intervals
    efforts = metrics.detect_intervals(watts_arr_scaled, threshold_watts=threshold)

    if not efforts:
        print(f"  Detected efforts: none above threshold ({round(threshold)}w)")
        # Fall back to summary
        w = actual.get("outdoor_equiv_np_watts") or stream_avg
        status = compute_interval_status(w, lo, hi, target_w)
        pct = round((w - target_w) / target_w * 100) if target_w else 0
        sign = "+" if pct >= 0 else ""
        print(f"  (Using stream avg as proxy: {w}w, {sign}{pct}%)  STATUS: {STATUS_LABELS.get(status, status.upper())}")
        return

    print(f"  Detected efforts:")
    effort_avgs = []
    for i, e in enumerate(efforts, 1):
        dur = fmt_duration(e["duration_s"])
        avg = e["avg_watts"]
        peak = e["peak_watts"]
        sym = effort_symbol(avg, target_w)
        effort_avgs.append(avg)
        print(f"    #{i}  {dur}  avg {avg}w  peak {peak}w  {sym} {'above target' if avg >= target_w else 'near target' if avg >= target_w * 0.95 else 'below target'}")

    avg_interval_w = round(sum(effort_avgs) / len(effort_avgs))
    min_w = min(effort_avgs)
    max_w = max(effort_avgs)
    pct = round((avg_interval_w - target_w) / target_w * 100) if target_w else 0
    sign = "+" if pct >= 0 else ""
    status = compute_interval_status(avg_interval_w, lo, hi, target_w)

    range_str = f"range {min_w}–{max_w}w" if len(effort_avgs) > 1 else f"{max_w}w"
    print(f"  Interval avg: {avg_interval_w}w (target {target_w}w, {range_str}) → {sign}{pct}%  STATUS: {STATUS_LABELS.get(status, status.upper())}")


def print_z2_analysis(actual, planned, ftp_outdoor, ftp_indoor, fetch_stream_fn):
    """Z2 time-in-zone breakdown."""
    target_w = planned.get("target_watts", 0)
    lo, hi = None, None
    r = planned.get("target_watts_range")
    if r and len(r) == 2:
        lo, hi = r

    is_indoor = actual.get("trainer", False)
    scale = ftp_outdoor / ftp_indoor if is_indoor else 1.0

    if lo and hi:
        print(f"  Zone floor: {lo}w  Zone ceil: {hi}w")

    # Fetch stream
    activity_id = actual.get("id")
    watts_raw = fetch_stream_fn(activity_id) if activity_id else None

    if not watts_raw:
        # Summary fallback
        w = actual.get("outdoor_equiv_watts") or 0
        if is_indoor:
            raw_avg = actual.get("avg_watts") or 0
            print(f"  Power source: summary only (no stream)")
            print(f"  avg={raw_avg}w raw → {w}w outdoor-equiv")
        else:
            print(f"  Power source: summary only (no stream)")
            print(f"  avg={w}w")

        if lo and hi and w > 0:
            if lo <= w <= hi:
                status = "hit"
            elif w >= lo * 0.90:
                status = "partial"
            else:
                status = "miss"
        else:
            status = "done"
        print(f"  (summary only, no zone breakdown)  STATUS: {STATUS_LABELS.get(status, status.upper())}")
        return

    watts_arr = np.array(watts_raw, dtype=float)
    stream_pts = len(watts_arr)

    if is_indoor:
        watts_arr_scaled = watts_arr * scale
        print(f"  Power source: stream ({stream_pts:,} pts) → scaled ×{scale:.3f}")
        raw_avg = round(float(np.mean(watts_arr)))
        scaled_avg = round(float(np.mean(watts_arr_scaled)))
        print(f"  Summary: avg={raw_avg}w raw → {scaled_avg}w outdoor-equiv")
    else:
        watts_arr_scaled = watts_arr
        stream_avg = round(float(np.mean(watts_arr_scaled)))
        print(f"  Power source: stream ({stream_pts:,} pts)")
        print(f"  Summary: avg={stream_avg}w")

    # Exclude first/last 5min (300s) for main block
    warmup_s = 300
    cooldown_s = 300
    total_s = len(watts_arr_scaled)

    if total_s > warmup_s + cooldown_s + 60:
        main_block = watts_arr_scaled[warmup_s: total_s - cooldown_s]
    else:
        main_block = watts_arr_scaled

    main_block_avg = round(float(np.mean(main_block))) if len(main_block) > 0 else None

    # Time-in-zone bucketing (on main block for status, full stream for display)
    if lo and hi:
        below = int(np.sum(watts_arr_scaled < lo))
        in_zone = int(np.sum((watts_arr_scaled >= lo) & (watts_arr_scaled <= hi)))
        above = int(np.sum(watts_arr_scaled > hi))
        total_analysis = len(watts_arr_scaled)

        below_pct = round(below / total_analysis * 100)
        in_zone_pct = round(in_zone / total_analysis * 100)
        above_pct = round(above / total_analysis * 100)

        in_zone_sym = "✓" if in_zone_pct >= 65 else "~" if in_zone_pct >= 45 else "✗"
        below_sym = "✗" if below_pct > 20 else "~" if below_pct > 10 else " "
        above_sym = "~" if above_pct > 15 else " "

        print(f"  Time in zone:  {in_zone // 60}min ({in_zone_pct}%)  {in_zone_sym}")
        print(f"  Below floor:   {below // 60}min ({below_pct}%)  {below_sym} {'<' + str(lo) + 'w' if below_pct > 5 else ''}")
        print(f"  Above ceiling: {above // 60}min ({above_pct}%)  {above_sym}")

        if main_block_avg is not None:
            print(f"  Main block avg: {main_block_avg}w (excl. first/last 5min)")

        status = compute_z2_status(in_zone, total_analysis, main_block_avg, target_w, lo, hi)
    else:
        # No zone defined — just show avg vs target
        main_avg = main_block_avg or 0
        if target_w and main_avg >= target_w * 0.95:
            status = "hit"
        elif target_w and main_avg >= target_w * 0.85:
            status = "partial"
        else:
            status = "done"
        if main_block_avg:
            print(f"  Main block avg: {main_block_avg}w")

    print(f"  STATUS: {STATUS_LABELS.get(status, status.upper())}")


def print_rest_or_unplanned(day_entry):
    planned = day_entry.get("planned")
    actual = day_entry.get("actual")
    if actual:
        name = actual.get("name", "?")
        duration = actual.get("duration_min", 0)
        avg_w = actual.get("avg_watts") or actual.get("outdoor_equiv_watts")
        w_str = f"  avg {avg_w}w" if avg_w else ""
        print(f"  Ride: \"{name}\" — {duration}min{w_str}  STATUS: UNPLANNED")


def print_week_header(week_offset, ftp_outdoor, ftp_indoor):
    today = date.today()
    week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=week_offset)
    week_end = week_start + timedelta(days=6)
    label = "Current week" if week_offset == 0 else f"{week_offset} week{'s' if week_offset > 1 else ''} ago"
    print_separator("═")
    print(f"  {label}: {week_start} → {week_end}")
    print(f"  FTP outdoor: {ftp_outdoor}w  FTP indoor: {ftp_indoor}w  Scale: ×{round(ftp_outdoor/ftp_indoor, 3)}")
    print_separator("═")


def audit_week(week_offset, activities, plan, ftp_outdoor, ftp_indoor):
    week_sessions = metrics.match_sessions_to_rides(
        plan, activities, ftp_outdoor, ftp_indoor, week_offset=week_offset
    )

    for day_entry in week_sessions:
        day_name = day_entry.get("day", "")
        day_date = day_entry.get("date", "")
        planned = day_entry.get("planned")
        actual = day_entry.get("actual")
        base_status = day_entry.get("status", "")

        session_type = (planned.get("type") if planned else None) or ""

        print_day_header(day_name, day_date, planned, actual, base_status)

        if session_type == "rest":
            print_rest_or_unplanned(day_entry)
            continue

        if planned is None:
            # Unplanned or no plan
            if actual:
                print_rest_or_unplanned(day_entry)
            continue

        if actual is None:
            print_no_ride(planned)
            continue

        # Has a planned non-rest session and a matched ride
        print_ride_header(actual, ftp_outdoor, ftp_indoor)

        if session_type in INTERVAL_TYPES:
            print_interval_analysis(actual, planned, ftp_outdoor, ftp_indoor,
                                    strava_client.fetch_power_stream)
        elif session_type in Z2_TYPES:
            print_z2_analysis(actual, planned, ftp_outdoor, ftp_indoor,
                              strava_client.fetch_power_stream)
        else:
            # Generic: just show summary power vs target
            target_w = planned.get("target_watts", 0)
            lo, hi = None, None
            r = planned.get("target_watts_range")
            if r and len(r) == 2:
                lo, hi = r
            w = actual.get("outdoor_equiv_np_watts") or actual.get("outdoor_equiv_watts") or 0
            if w and target_w:
                pct = round((w - target_w) / target_w * 100)
                sign = "+" if pct >= 0 else ""
                status = compute_interval_status(w, lo, hi, target_w)
                print(f"  Outdoor-equiv power: {w}w (target {target_w}w) → {sign}{pct}%  STATUS: {STATUS_LABELS.get(status, status.upper())}")
            else:
                print(f"  STATUS: {STATUS_LABELS.get(base_status, base_status.upper())}")


def main():
    args = parse_args()
    weeks = max(1, args.weeks)

    strava_client.refresh_access_token()
    activities = strava_client.fetch_activities(weeks=max(weeks + 1, 4))
    ftp_outdoor, ftp_indoor = metrics.load_ftps()
    plan = load_plan()

    if plan is None:
        print("ERROR: data/plan.json not found", file=sys.stderr)
        sys.exit(1)

    scale = round(ftp_outdoor / ftp_indoor, 3)
    print(f"\nPower Audit — {weeks} week{'s' if weeks > 1 else ''}")
    print(f"FTPs: outdoor={ftp_outdoor}w  indoor={ftp_indoor}w  scale={scale}×")
    print(f"Interval threshold: target × 0.88  |  Smoothing: 30s  |  Gap tolerance: 15s  |  Min effort: 60s")

    offsets = list(range(0, weeks))  # 0=current, 1=last, ...
    for offset in offsets:
        print_week_header(offset, ftp_outdoor, ftp_indoor)
        audit_week(offset, activities, plan, ftp_outdoor, ftp_indoor)

    print()


if __name__ == "__main__":
    main()
