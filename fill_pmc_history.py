"""
fill_pmc_history.py — One-time PMC backfill from full Strava history.

Fetches every ride ever from Strava, computes CTL/ATL/TSB from day 1,
and writes the full series to pmc_daily. After this runs, the weekly
main.py uses db.get_pmc_seed() so CTL is continuous rather than
restarting from zero each 8-week window.

Run once:
    python fill_pmc_history.py

Force re-fetch from Strava (skips .cache/activities_all.json):
    python fill_pmc_history.py --force
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import strava_client
import metrics
import db as db_module


def main():
    force = "--force" in sys.argv

    strava_client.refresh_access_token()

    activities = strava_client.fetch_all_activities(force_refresh=force)

    ftp_outdoor, ftp_indoor, ftp_indoor_working, peloton_factor = metrics.load_ftps()

    # Build all-time TSS series (weeks=None → start from first ride date)
    tss_by_day = metrics.daily_tss(activities, ftp_outdoor, ftp_indoor, weeks=None)

    if not tss_by_day:
        print("No rides found — nothing to write.")
        return

    first_date = min(tss_by_day.keys())
    last_date  = max(tss_by_day.keys())
    total_days = len(tss_by_day)
    ride_days  = sum(1 for v in tss_by_day.values() if v > 0)
    print(f"TSS computed: {first_date} → {last_date} ({total_days} days, {ride_days} ride days)")

    # Compute PMC from scratch — no seed needed, this IS the full history
    pmc_series = metrics.compute_pmc(tss_by_day)
    current    = pmc_series[-1]
    print(f"PMC computed: CTL={current['ctl']} / ATL={current['atl']} / TSB={current['tsb']} (today)")

    # Write to DB
    db_module.append_pmc_rows(pmc_series)
    print(f"Written {len(pmc_series)} rows to pmc_daily.")

    # Print CTL trajectory summary (monthly peaks)
    print("\nCTL trajectory (monthly peak):")
    by_month = {}
    for row in pmc_series:
        key = str(row["date"])[:7]  # YYYY-MM
        if row["ctl"] > by_month.get(key, 0):
            by_month[key] = row["ctl"]
    for month, peak_ctl in sorted(by_month.items()):
        bar = "█" * int(peak_ctl / 2)
        print(f"  {month}  {peak_ctl:5.1f}  {bar}")


if __name__ == "__main__":
    main()
