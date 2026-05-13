"""
send_travel_email.py — One-off script to send the Summerland travel-adjusted plan email.
Builds and sends the email directly from the current plan.json without Claude re-adjusting it.
"""

import os
import sys
import json
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

import strava_client
import metrics
import db as db_module
import email_builder
import email_sender
import coach

DRY_RUN = "--dry-run" in sys.argv

DAY_ABBR = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
}

def sessions_to_email_plan(sessions):
    """Convert plan.json sessions list to email_builder this_week_plan dict."""
    plan = {}
    for s in sessions:
        day = s.get("day", "")
        abbr = DAY_ABBR.get(day)
        if not abbr:
            continue
        plan[abbr] = {
            "type":              s.get("type", "rest"),
            "duration_min":      s.get("duration_min", 0),
            "zone_label":        s.get("zone_label", ""),
            "target_watts":      s.get("target_watts", 0),
            "target_watts_range": s.get("target_watts_range", [0, 0]),
            "environment":       s.get("environment", "flexible"),
            "optional":          s.get("optional", False),
            "description":       s.get("description", "Rest"),
            "interval_sets":     s.get("interval_sets", []),
        }
    return plan


def main():
    strava_client.refresh_access_token()

    # Load data
    activities   = strava_client.fetch_activities(weeks=2, force_refresh=False)
    ftp_outdoor, ftp_indoor, *_ = metrics.load_ftps()
    zone_ranges  = metrics.compute_zone_ranges(ftp_outdoor, ftp_indoor)
    max_hr       = metrics.load_max_hr()

    # PMC from DB (already computed by today's main.py run)
    tss_by_day   = metrics.daily_tss(activities, ftp_outdoor, ftp_indoor, weeks=8)
    pmc_series   = metrics.compute_pmc(tss_by_day)
    pmc_current  = metrics.current_pmc(pmc_series)

    # Last week summary
    last_week = metrics.weekly_summary(activities, ftp_outdoor, ftp_indoor, week_offset=1)

    # Check-in
    checkin_data = None
    checkin_path = os.path.join(os.path.dirname(__file__), "data", "checkin.json")
    if os.path.exists(checkin_path):
        with open(checkin_path) as f:
            checkin_data = json.load(f)

    # Strava descriptions (last 7 days)
    strava_descriptions = metrics.collect_ride_descriptions(
        activities, strava_client.fetch_activity_description, days=7
    )

    # Load updated plan
    plan = coach._load_plan()
    if not plan:
        print("No plan found.")
        sys.exit(1)

    # Find current week (week 4) and next week (week 5) sessions
    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    current_week = None
    next_week    = None
    for week in plan.get("weeks", []):
        ws = date.fromisoformat(week["week_start"])
        if ws == week_start:
            current_week = week
        elif ws == week_start + timedelta(weeks=1):
            next_week = week

    if not current_week:
        print(f"No current week found for {week_start}.")
        sys.exit(1)

    this_week_plan = sessions_to_email_plan(current_week["sessions"])
    ctl  = round(pmc_current.get("ctl", 0), 1)
    atl  = round(pmc_current.get("atl", 0), 1)
    tsb  = round(pmc_current.get("tsb", 0), 1)

    # Build week 5 summary for coaches note
    next_week_summary = ""
    if next_week:
        nw_sessions = next_week.get("sessions", [])
        hard_days = [s["day"].capitalize() for s in nw_sessions
                     if s.get("type") in ("vo2max", "anaerobic")]
        next_week_summary = (
            f"Week 5 (Apr 20–26) is built around Gibraltar on Monday, "
            f"with structured work returning Friday ({', '.join(hard_days[:2])} are the hard days). "
            f"Wednesday Apr 22 is also a drive day."
        )

    email_content = {
        "fitness_snapshot": {
            "headline": f"CTL {ctl} / ATL {atl} / TSB {tsb:+.0f}",
            "body": (
                f"You're nearly fresh (TSB {tsb:+.0f}) coming off the illness recovery week — "
                f"CTL has held at {ctl}, which is actually good given you lost training days. "
                f"Tuesday's Z2 re-entry spin and the Summerland trip should have you sharp by Friday. "
                f"ATL {atl} is low, meaning there's room to load this week without digging a hole."
            ),
        },
        "last_week_recap": {
            "headline": "Illness recovery — held fitness, TSB reset to nearly fresh",
            "body": (
                f"Last week: {last_week.get('tss', 0):.0f} TSS across "
                f"{last_week.get('rides', 0) if isinstance(last_week.get('rides'), int) else len(last_week.get('rides', []))} rides, "
                f"{last_week.get('distance_km', 0):.0f}km. "
                "Sickness forced a conservative week but TSB is now nearly neutral — "
                "that's actually the ideal state to arrive in Summerland. "
                "The plan was adjusted to make use of the terrain there."
            ),
        },
        "this_week_plan": this_week_plan,
        "block_progress": {
            "headline": f"Week 4 of 5 — Recovery + Summerland",
            "body": (
                "This is the final week of the current block, repurposed as a travel week. "
                "Wednesday is a drive day. Thursday easy arrival spin. "
                "Friday is the first real stimulus: 2×8min max efforts on Old San Marcos "
                "(paced thirds: 340–355 / 360–375 / 375–395w). "
                "Saturday Z2 recovery. Sunday easy ahead of Gibraltar Monday. "
                f"{next_week_summary}"
            ),
        },
        "coaches_note": {
            "headline": "Old San Marcos Friday, Gibraltar Monday — pace the thirds",
            "body": (
                "Two key pacing cues. Old San Marcos 8min: start conservative "
                "(340–355w first third), find ceiling mid-effort (360–375w), then empty it "
                "(375–395w). Second rep will be 10–20w lower — don't chase the first. "
                "Gibraltar Monday: first 17min at 285–300w no matter how good you feel. "
                "The pacing error always happens in the first third. "
                "Fuel aggressively for both: you ran out of water on Gibraltar last year."
            ),
        },
    }

    # Build HTML
    power_curve_data = None
    try:
        rows = db_module._con().execute(
            "SELECT s60, s300, s600, s1200 FROM power_curve_weekly ORDER BY week_end DESC LIMIT 1"
        ).fetchone()
        if rows:
            power_curve_data = {"s60": rows[0], "s300": rows[1], "s600": rows[2], "s1200": rows[3]}
    except Exception:
        pass

    html = email_builder.build_email(
        email_content,
        pmc_current,
        last_week,
        session_comparison=None,
        hr_data=None,
        zone_ranges=zone_ranges,
        ftp_outdoor=ftp_outdoor,
        ftp_indoor=ftp_indoor,
        power_curve_data=power_curve_data,
        strava_descriptions=strava_descriptions,
        checkin_data=checkin_data,
        is_recovery_week=True,
    )

    if DRY_RUN:
        out_path = "/tmp/travel_plan_email.html"
        with open(out_path, "w") as f:
            f.write(html)
        print(f"Dry run — HTML written to {out_path}")
    else:
        email_sender.send_email(html)
        print("Travel plan email sent.")


if __name__ == "__main__":
    main()
