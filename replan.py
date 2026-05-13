"""
replan.py — Mid-week adaptive plan adjustment.

Run this manually after a session that went wrong (missed intervals, illness,
bad day, etc.). Reads current-week completed sessions (power actuals + Strava
descriptions) and adjusts the remaining days conservatively.

Usage:
    python replan.py            # adjust remaining days based on actuals
    python replan.py --dry-run  # print the adjusted sessions, don't save

Dampening rules (enforced in the Claude prompt):
  - Max 15% reduction in remaining-week TSS
  - No hard sessions within 48h of each other
  - Prefer ±5-10% watts adjustments over session-type swaps
  - Do not shift any session by more than 1 day
"""

import os
import sys
import json
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

import strava_client
import metrics
import coach
import db as db_module


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _current_week_sessions(plan):
    """Return the sessions list for the current week from plan.json, or []."""
    if not plan or "weeks" not in plan:
        return []
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    for week in plan["weeks"]:
        try:
            ws = date.fromisoformat(week["week_start"])
            if ws == week_start:
                return week.get("sessions", [])
        except Exception:
            continue
    return []


def _split_sessions(sessions):
    """
    Split current-week sessions into completed (Mon–yesterday) and remaining (today–Sun).
    Returns (completed_days_ordered, remaining_sessions_ordered).
    completed_days_ordered matches match_sessions_to_rides output format.
    """
    today = date.today()
    today_day = _DAYS[today.weekday()]

    completed_days = [_DAYS[i] for i in range(today.weekday())]
    remaining_days = [_DAYS[i] for i in range(today.weekday(), 7)]

    completed = [s for s in sessions if s.get("day") in completed_days]
    remaining = [s for s in sessions if s.get("day") in remaining_days]

    # Sort by day order
    completed.sort(key=lambda s: _DAYS.index(s.get("day", "monday")))
    remaining.sort(key=lambda s: _DAYS.index(s.get("day", "monday")))

    return completed, remaining


def _merge_replan_into_plan(plan, adjusted_sessions):
    """
    Overwrite plan.json sessions for days that appear in adjusted_sessions.
    Only touches the current week. Returns the modified plan dict.
    """
    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    adjusted_by_day = {s["day"]: s for s in adjusted_sessions}
    if not adjusted_by_day:
        return plan

    for week in plan.get("weeks", []):
        try:
            ws = date.fromisoformat(week["week_start"])
        except Exception:
            continue
        if ws != week_start:
            continue
        new_sessions = []
        for session in week.get("sessions", []):
            day = session.get("day")
            if day in adjusted_by_day:
                new_sessions.append(adjusted_by_day[day])
            else:
                new_sessions.append(session)
        week["sessions"] = new_sessions
        break

    return plan


def run(dry_run=False):
    # 1. Bootstrap
    strava_client.refresh_access_token()
    activities = strava_client.fetch_activities(weeks=2, force_refresh=False)
    ftp_outdoor, ftp_indoor, *_ = metrics.load_ftps()

    # 2. Load current plan
    plan = coach._load_plan()
    if not plan:
        print("No plan found (data/plan.json). Run main.py --init first.")
        sys.exit(1)

    # 3. Split sessions into completed / remaining
    sessions = _current_week_sessions(plan)
    if not sessions:
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        print(f"No sessions found for current week ({week_start.isoformat()}). "
              "The plan may not cover this week yet.")
        sys.exit(1)

    completed_plan_sessions, remaining_sessions = _split_sessions(sessions)

    if not remaining_sessions:
        print("No remaining sessions this week — nothing to replan.")
        sys.exit(0)

    today_day = _DAYS[date.today().weekday()]
    print(f"Today: {date.today().isoformat()} ({today_day.capitalize()})")
    print(f"Completed days: {[s['day'] for s in completed_plan_sessions] or ['none yet']}")
    print(f"Remaining days: {[s['day'] for s in remaining_sessions]}")

    # 4. Match completed planned sessions to actual rides (for power actuals)
    completed_comparison = []
    if completed_plan_sessions:
        # Build a stub plan with only this week to feed into match_sessions_to_rides
        stub_plan = {**plan, "weeks": [{
            "week_start": (date.today() - timedelta(days=date.today().weekday())).isoformat(),
            "sessions": completed_plan_sessions,
        }]}
        completed_comparison = metrics.match_sessions_to_rides(
            stub_plan, activities, ftp_outdoor, ftp_indoor, week_offset=0
        )
        # Filter to only actually-completed days
        completed_day_names = {s["day"] for s in completed_plan_sessions}
        completed_comparison = [d for d in completed_comparison if d.get("day") in completed_day_names]

        # Compute interval achievement for completed days
        completed_comparison = metrics.interval_achievement(
            completed_comparison, strava_client.fetch_power_stream,
            ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
            fetch_hr_fn=strava_client.fetch_hr_stream,
            fetch_laps_fn=strava_client.fetch_activity_laps,
        )
        completed_comparison = metrics.recompute_status(completed_comparison)

    # 5. Fetch ride descriptions for completed days
    descriptions = metrics.collect_ride_descriptions(
        activities, strava_client.fetch_activity_description, days=date.today().weekday() + 1
    )

    # 6. Current PMC
    tss_by_day = metrics.daily_tss(activities, ftp_outdoor, ftp_indoor, weeks=2)
    pmc_series = metrics.compute_pmc(tss_by_day)
    pmc_current = metrics.current_pmc(pmc_series)
    print(f"PMC: CTL={pmc_current['ctl']} ATL={pmc_current['atl']} TSB={pmc_current['tsb']}")

    # 7. Call Claude replan
    result = coach.generate_replan(
        pmc_current=pmc_current,
        completed_sessions=completed_comparison,
        remaining_sessions=remaining_sessions,
        descriptions=descriptions,
    )

    reason = result.get("replan_reason", "No reason provided.")
    adjusted = result.get("adjusted_sessions", [])

    print(f"\nReplan reason: {reason}")

    if not adjusted:
        print("No changes recommended — plan unchanged.")
        return

    # 8. Show diff
    original_by_day = {s["day"]: s for s in remaining_sessions}
    print("\nChanges:")
    for s in adjusted:
        day = s.get("day", "?")
        orig = original_by_day.get(day, {})
        orig_type = orig.get("type", "?")
        new_type = s.get("type", "?")
        orig_w = orig.get("target_watts", "?")
        new_w = s.get("target_watts", "?")
        if orig_type != new_type:
            print(f"  {day.capitalize()}: {orig_type} → {new_type} ({orig_w}w → {new_w}w)")
        elif orig_w != new_w:
            delta = (new_w - orig_w) if isinstance(new_w, (int, float)) and isinstance(orig_w, (int, float)) else "?"
            sign = "+" if isinstance(delta, (int, float)) and delta >= 0 else ""
            print(f"  {day.capitalize()}: {new_type} watts {orig_w}w → {new_w}w ({sign}{delta}w)")
        else:
            # Check interval set changes
            orig_sets = orig.get("interval_sets", [])
            new_sets = s.get("interval_sets", [])
            if orig_sets != new_sets:
                print(f"  {day.capitalize()}: {new_type} — interval sets adjusted")
            else:
                print(f"  {day.capitalize()}: {new_type} — minor adjustment")

    if dry_run:
        print("\n[dry-run] Plan NOT saved.")
        print(json.dumps(adjusted, indent=2))
        return

    # 9. Merge into plan.json and save
    updated_plan = _merge_replan_into_plan(plan, adjusted)
    coach._save_plan(updated_plan)
    print(f"\nPlan updated: data/plan.json ({len(adjusted)} day(s) modified)")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
