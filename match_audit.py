"""
match_audit.py — Diagnostic tool for session matching accuracy.

Shows planned vs. actual ride pairings for recent weeks, highlights mismatches,
and supports interactive labeling to create manual override entries.

Usage:
    python match_audit.py              # Show last 4 weeks
    python match_audit.py --weeks=8   # Show last 8 weeks
    python match_audit.py --label     # Interactive label mode: write match_overrides.json
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Setup paths
# ---------------------------------------------------------------------------
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
ACTIVITIES_CACHE = os.path.join(BASE_DIR, "data", "activities.json")
PLAN_PATH        = os.path.join(BASE_DIR, "data", "plan.json")
PLANS_DIR        = os.path.join(BASE_DIR, "data", "plans")
OVERRIDES_PATH   = os.path.join(BASE_DIR, "data", "match_overrides.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_activities():
    if not os.path.exists(ACTIVITIES_CACHE):
        print("ERROR: data/activities.json not found. Run main.py first to cache activities.")
        sys.exit(1)
    with open(ACTIVITIES_CACHE) as f:
        return json.load(f)


def _load_plan():
    if not os.path.exists(PLAN_PATH):
        print("ERROR: data/plan.json not found.")
        sys.exit(1)
    with open(PLAN_PATH) as f:
        return json.load(f)


def _build_plan_index():
    """
    Build a mapping of week_start (date) → list of plan dicts that contain that week,
    sourced from both the current plan.json and all archived plans in data/plans/.

    For each week_start, keeps the most recently generated plan (by filename date).
    Also handles the common off-by-one where plan week_start is a Tuesday (generated date)
    instead of Monday — we normalize to Monday.
    """
    all_plans = []

    # Load current plan
    if os.path.exists(PLAN_PATH):
        try:
            with open(PLAN_PATH) as f:
                p = json.load(f)
            p["_source_date"] = p.get("generated_date", "0000-00-00")
            all_plans.append(p)
        except Exception:
            pass

    # Load all archives
    if os.path.isdir(PLANS_DIR):
        import re
        for fname in sorted(os.listdir(PLANS_DIR)):
            m = re.match(r"plan_(\d{4}-\d{2}-\d{2})\.json$", fname)
            if not m:
                continue
            try:
                with open(os.path.join(PLANS_DIR, fname)) as f:
                    p = json.load(f)
                p["_source_date"] = m.group(1)
                all_plans.append(p)
            except Exception:
                continue

    # Build index: monday_week_start → best plan
    index = {}  # date → (source_date_str, plan_dict)
    for p in all_plans:
        src = p.get("_source_date", "")
        for week in p.get("weeks", []):
            try:
                ws = date.fromisoformat(week["week_start"])
            except (ValueError, KeyError):
                continue
            # Normalize to Monday (handles Tuesday-generated week_starts)
            ws_monday = ws - timedelta(days=ws.weekday())
            existing_src = index.get(ws_monday, ("", None))[0]
            if src >= existing_src:
                index[ws_monday] = (src, p)

    return {k: v[1] for k, v in index.items()}  # date → plan_dict


def _load_overrides():
    if not os.path.exists(OVERRIDES_PATH):
        return []
    with open(OVERRIDES_PATH) as f:
        return json.load(f)


def _save_overrides(overrides):
    with open(OVERRIDES_PATH, "w") as f:
        json.dump(overrides, f, indent=2)
    print(f"Saved {len(overrides)} override(s) to {OVERRIDES_PATH}")


def _fmt_duration(minutes):
    if not minutes:
        return "—"
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _fmt_ride(ride):
    """Short one-line summary of a ride."""
    name  = ride.get("name", "Ride")
    dur   = _fmt_duration((ride.get("moving_time") or 0) // 60)
    watts = ride.get("weighted_average_watts") or ride.get("average_watts") or 0
    tss   = ride.get("suffer_score") or "—"
    act_id = ride.get("id", "?")
    trainer = " [indoor]" if ride.get("trainer") else ""
    w_str = f" {int(watts)}w" if watts else ""
    return f'"{name}" {dur}{w_str}{trainer}  (id:{act_id})'


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------

def _normalize_plan_week_starts(plan):
    """Return a copy of plan with all week_start values normalized to Monday."""
    if not plan:
        return plan
    import copy
    p = copy.deepcopy(plan)
    for week in p.get("weeks", []):
        try:
            ws = date.fromisoformat(week["week_start"])
            week["week_start"] = (ws - timedelta(days=ws.weekday())).isoformat()
        except (ValueError, KeyError):
            pass
    return p


def build_week_audit(plan_index, activities, ftp_outdoor, ftp_indoor, week_offset, overrides):
    """
    Return audit dict for one week:
      week_start, sessions: list of day-level dicts

    plan_index: dict of monday_week_start (date) → plan_dict (from _build_plan_index())
    """
    import metrics as m

    today      = date.today()
    week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=week_offset)
    week_end   = week_start + timedelta(days=6)

    # Pick the best plan for this week from the index; normalize week_starts to Monday
    raw_plan = plan_index.get(week_start)
    plan     = _normalize_plan_week_starts(raw_plan)

    day_order = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    # Rides this week
    week_rides = {}
    for ride in activities:
        if ride.get("type") != "Ride":
            continue
        try:
            rd = date.fromisoformat(ride.get("start_date_local", "")[:10])
        except ValueError:
            continue
        if week_start <= rd <= week_end:
            day_name = day_order[rd.weekday()]
            week_rides.setdefault(day_name, []).append(ride)

    # Planned sessions for this week (from normalized plan)
    planned = {}
    if plan and "weeks" in plan:
        for week in plan["weeks"]:
            try:
                ws = date.fromisoformat(week["week_start"])
            except (ValueError, KeyError):
                continue
            if abs((ws - week_start).days) <= 2:
                for sess in week.get("sessions", []):
                    planned[sess["day"].lower()] = sess
                break

    # Pinned overrides for this week
    week_start_str = week_start.isoformat()
    pinned = {}  # day → activity_id
    for ov in overrides:
        if ov.get("week_start") == week_start_str:
            pinned[ov["day"].lower()] = ov.get("activity_id")

    # Run the matcher using the normalized archived plan and this week's offset
    session_comparison = m.match_sessions_to_rides(plan, activities, ftp_outdoor, ftp_indoor, week_offset)

    # Build per-day audit rows
    rows = []
    for sc in session_comparison:
        day = sc.get("day", "").lower()
        p   = sc.get("planned") or {}
        a   = sc.get("actual")
        status = sc.get("status", "")

        day_rides = week_rides.get(day, [])
        is_pinned = day in pinned

        rows.append({
            "day":       day,
            "date":      (week_start + timedelta(days=day_order.index(day))).isoformat(),
            "planned":   p,
            "actual":    a,
            "status":    status,
            "day_rides": day_rides,
            "is_pinned": is_pinned,
            "pinned_id": pinned.get(day),
        })

    return {
        "week_start": week_start_str,
        "rows": rows,
    }


def print_week_audit(audit):
    """Pretty-print one week's audit."""
    ws = audit["week_start"]
    print(f"\n{'='*72}")
    print(f"  Week of {ws}")
    print(f"{'='*72}")

    for row in audit["rows"]:
        day    = row["day"].capitalize()
        p      = row["planned"] or {}
        a      = row["actual"]
        status = row["status"].upper() if row["status"] else "—"
        is_pinned = row["is_pinned"]

        ptype = p.get("type", "rest") if p else "rest"
        pdur  = _fmt_duration(p.get("duration_min")) if p else "—"
        lo, hi = (p.get("target_watts_range") or [0, 0])
        prange = f"[{lo}–{hi}w]" if lo or hi else ""

        pin_flag = " [PINNED OVERRIDE]" if is_pinned else ""

        if ptype == "rest" and not a:
            # Rest day with no ride — skip unless there was an unplanned ride
            if not row["day_rides"]:
                print(f"  {day:<11}  REST")
                continue

        # Show planned session
        if ptype != "rest":
            planned_str = f"PLANNED: {ptype.upper()} {pdur} {prange}"
        else:
            planned_str = "REST (no session planned)"

        # Show matched actual
        if a:
            actual_str = f"→ MATCHED: {_fmt_ride(a)} [{status}]{pin_flag}"
        else:
            actual_str = f"→ NO MATCH [{status}]{pin_flag}"

        print(f"  {day:<11}  {planned_str}")
        print(f"             {actual_str}")

        # Show unmatched rides from that day (candidates for labeling)
        matched_id = (a or {}).get("id")
        others = [r for r in row["day_rides"] if r.get("id") != matched_id]
        if others and not a:
            print(f"             Available rides this day:")
            for r in others:
                print(f"               - {_fmt_ride(r)}")

        # If no match, show nearby rides from ±2 days (already done by matcher, shown for labeling)
        if not a and ptype != "rest":
            print()


def find_unmatched_sessions(audit):
    """Return list of rows with planned non-rest sessions that have no actual ride."""
    return [
        r for r in audit["rows"]
        if (r["planned"] or {}).get("type", "rest") != "rest"
        and r["actual"] is None
    ]


# ---------------------------------------------------------------------------
# Interactive label mode
# ---------------------------------------------------------------------------

def label_mode(plan_index, activities, ftp_outdoor, ftp_indoor, weeks):
    """
    Interactive mode: show unmatched planned sessions and let user assign activity IDs.
    Writes selections to data/match_overrides.json.
    """
    overrides = _load_overrides()
    existing_keys = {(ov["week_start"], ov["day"]) for ov in overrides}

    # Build ride lookup by ID
    ride_by_id = {r.get("id"): r for r in activities}

    new_overrides = []

    for offset in range(1, weeks + 1):
        audit = build_week_audit(plan_index, activities, ftp_outdoor, ftp_indoor, offset, overrides)
        unmatched = find_unmatched_sessions(audit)
        if not unmatched:
            continue

        ws = audit["week_start"]
        print(f"\n{'='*72}")
        print(f"  Week of {ws} — {len(unmatched)} unmatched planned session(s)")
        print(f"{'='*72}")
        print_week_audit(audit)

        for row in unmatched:
            day  = row["day"]
            key  = (ws, day)
            if key in existing_keys:
                print(f"\n  [{day.capitalize()}] Already has an override — skipping.")
                continue

            p = row["planned"] or {}
            print(f"\n  [{day.capitalize()}] Planned: {p.get('type','?')} {_fmt_duration(p.get('duration_min'))} {p.get('target_watts_range','')}")
            print(f"  Enter activity ID to assign (or 'skip' / 's' / Enter to skip):")
            print(f"  Available rides in this week to choose from:")

            today      = date.today()
            week_start = date.fromisoformat(ws)
            week_end   = week_start + timedelta(days=6)
            nearby = sorted(
                [r for r in activities
                 if r.get("type") == "Ride"
                 and week_start <= date.fromisoformat(r.get("start_date_local","")[:10]) <= week_end],
                key=lambda r: r.get("start_date_local", "")
            )
            for r in nearby:
                print(f"    {r.get('start_date_local','')[:10]}  {_fmt_ride(r)}")

            try:
                val = input("  Activity ID: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                break

            if not val or val.lower() in ("s", "skip"):
                print("  Skipped.")
                continue

            try:
                act_id = int(val)
            except ValueError:
                print("  Invalid ID — skipped.")
                continue

            if act_id not in ride_by_id:
                print(f"  Warning: activity {act_id} not found in cached activities. Saving anyway.")

            note_input = ""
            try:
                note_input = input("  Note (optional, Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                pass

            entry = {
                "week_start":  ws,
                "day":         day,
                "activity_id": act_id,
            }
            if note_input:
                entry["note"] = note_input

            new_overrides.append(entry)
            existing_keys.add(key)
            print(f"  Saved: {day} → activity {act_id}")

    if new_overrides:
        all_overrides = overrides + new_overrides
        _save_overrides(all_overrides)
        print(f"\nAdded {len(new_overrides)} new override(s).")
    else:
        print("\nNo new overrides added.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Audit planned vs actual session matching.")
    parser.add_argument("--weeks", type=int, default=4, help="Number of past weeks to audit (default: 4)")
    parser.add_argument("--label", action="store_true", help="Interactive label mode: assign activity IDs to unmatched sessions")
    args = parser.parse_args()

    import metrics as m

    activities   = _load_activities()
    plan_index   = _build_plan_index()
    ftp_outdoor, ftp_indoor, *_ = m.load_ftps()
    overrides    = _load_overrides()

    if args.label:
        label_mode(plan_index, activities, ftp_outdoor, ftp_indoor, args.weeks)
        return

    # Audit mode
    print(f"\nSession matching audit — last {args.weeks} week(s)")
    print(f"FTP outdoor: {ftp_outdoor}w  FTP indoor: {ftp_indoor}w")
    print(f"Plan weeks indexed: {sorted(str(k) for k in plan_index.keys())}")
    print(f"Overrides file: {OVERRIDES_PATH} ({len(overrides)} override(s) loaded)")

    total_planned = 0
    total_matched = 0
    total_misses  = 0

    for offset in range(1, args.weeks + 1):
        audit = build_week_audit(plan_index, activities, ftp_outdoor, ftp_indoor, offset, overrides)
        print_week_audit(audit)

        for row in audit["rows"]:
            p = row["planned"] or {}
            if p.get("type", "rest") == "rest":
                continue
            total_planned += 1
            if row["actual"]:
                total_matched += 1
            else:
                total_misses += 1

    print(f"\n{'='*72}")
    match_pct = round(total_matched / total_planned * 100) if total_planned else 0
    print(f"  Summary: {total_matched}/{total_planned} planned sessions matched ({match_pct}%)")
    print(f"  Unmatched: {total_misses}")
    if total_misses:
        print(f"  Run with --label to assign activity IDs to unmatched sessions.")
    print()


if __name__ == "__main__":
    main()
