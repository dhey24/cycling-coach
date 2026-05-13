"""
training_history.py — Multi-year training analysis report.

Reads cached data (DuckDB, activities_all.json, power streams) to produce:
  1. 8 matplotlib charts (docs/analysis/charts/)
  2. Self-contained HTML report (docs/analysis/training_history_report.html)
  3. Markdown summary for coach context (data/training_history_summary.md)

Usage:
    python training_history.py              # Full report (2020+)
    python training_history.py --no-coach   # Skip Claude coaching interpretation
    python training_history.py --from=2022  # Override start year
"""

import json
import os
import sys
import base64
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from matplotlib import rcParams

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import metrics
import db as db_module

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
ACTIVITIES_ALL_CACHE = os.path.join(CACHE_DIR, "activities_all.json")
POWER_HISTORY_CACHE = os.path.join(CACHE_DIR, "power_history_cache.json")
CHART_DIR = os.path.join(BASE_DIR, "docs", "analysis", "charts")
REPORT_PATH = os.path.join(BASE_DIR, "docs", "analysis", "training_history_report.html")
SUMMARY_PATH = os.path.join(BASE_DIR, "data", "training_history_summary.md")

# ── Chart style (matches generate_interval_charts.py) ─────────────────────────
rcParams["font.family"] = "monospace"
rcParams["axes.spines.top"] = False
rcParams["axes.spines.right"] = False
rcParams["axes.grid"] = True
rcParams["grid.alpha"] = 0.25
rcParams["grid.linestyle"] = "--"

BG = "#0f0f0f"
PANEL = "#1a1a1a"
ACCENT1 = "#e84545"   # red
ACCENT2 = "#2fbfde"   # cyan
ACCENT3 = "#f5a623"   # amber
ACCENT4 = "#7ed6a8"   # green
GREY = "#555555"
TEXT = "#dddddd"
MUTED = "#888888"

BABY_DATE = date(2025, 9, 18)


def styled_fig(*args, **kwargs):
    return plt.figure(*args, facecolor=BG, **kwargs)


def style_ax(ax, title=None, xlabel=None, ylabel=None):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.xaxis.label.set_color(TEXT)
    ax.yaxis.label.set_color(TEXT)
    for spine in ax.spines.values():
        spine.set_edgecolor(GREY)
    if title:
        ax.set_title(title, color=TEXT, fontsize=11, pad=8, fontweight="bold")
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=9)


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1: DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def load_all_data(start_year=2020):
    start_date = f"{start_year}-01-01"

    # PMC from DuckDB
    conn = db_module.get_conn()
    pmc_rows = conn.execute(
        "SELECT date, tss, ctl, atl, tsb FROM pmc_daily WHERE date >= ? ORDER BY date",
        [start_date],
    ).fetchall()
    pmc = [{"date": r[0], "tss": r[1], "ctl": r[2], "atl": r[3], "tsb": r[4]} for r in pmc_rows]

    # Activity metrics from DuckDB
    try:
        am_rows = conn.execute(
            "SELECT activity_id, activity_date, ef, decoupling, avg_hr, avg_watts, np, activity_type "
            "FROM activity_metrics WHERE activity_date >= ? ORDER BY activity_date",
            [start_date],
        ).fetchall()
        activity_metrics = [
            {"activity_id": r[0], "activity_date": r[1], "ef": r[2], "decoupling": r[3],
             "avg_hr": r[4], "avg_watts": r[5], "np": r[6], "activity_type": r[7]}
            for r in am_rows
        ]
    except Exception:
        activity_metrics = []
    conn.close()

    # Activities from cache
    if not os.path.exists(ACTIVITIES_ALL_CACHE):
        print(f"ERROR: {ACTIVITIES_ALL_CACHE} not found. Run fill_pmc_history.py first.")
        sys.exit(1)
    with open(ACTIVITIES_ALL_CACHE) as f:
        all_activities = json.load(f)

    # Filter to rides >= start_year
    activities = [
        a for a in all_activities
        if a.get("type") == "Ride" and a.get("start_date_local", "")[:4] >= str(start_year)
    ]

    ftp_outdoor, ftp_indoor, ftp_indoor_working, peloton_factor = metrics.load_ftps()
    weight_kg = metrics.load_rider_weight()

    print(f"Loaded: {len(pmc)} PMC days, {len(activities)} rides, {len(activity_metrics)} activity_metrics rows")
    return {
        "pmc": pmc,
        "activities": activities,
        "activity_metrics": activity_metrics,
        "ftp_outdoor": ftp_outdoor,
        "ftp_indoor": ftp_indoor,
        "peloton_factor": peloton_factor,
        "weight_kg": weight_kg,
        "start_year": start_year,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2: ANALYSIS FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _ride_date(ride):
    return ride.get("start_date_local", ride.get("start_date", ""))[:10]


def _ym(d):
    """Return (year, month) tuple from a date string or date object."""
    if isinstance(d, str):
        return (int(d[:4]), int(d[5:7]))
    return (d.year, d.month)


def _ym_key(d):
    y, m = _ym(d)
    return f"{y}-{m:02d}"


# ── A. Training Volume ────────────────────────────────────────────────────────

def analyze_training_volume(activities, pmc, ftp_outdoor, ftp_indoor):
    monthly = defaultdict(lambda: {
        "rides": 0, "hours": 0.0, "tss": 0.0, "km": 0.0, "elev": 0.0,
        "indoor": 0, "outdoor": 0, "tss_missing": 0,
    })

    for ride in activities:
        rd = _ride_date(ride)
        ym = _ym_key(rd)
        m = monthly[ym]
        m["rides"] += 1
        m["hours"] += ride.get("moving_time", 0) / 3600
        m["km"] += ride.get("distance", 0) / 1000
        m["elev"] += ride.get("total_elevation_gain", 0)
        if ride.get("trainer", False):
            m["indoor"] += 1
        else:
            m["outdoor"] += 1
        tss = metrics.tss_for_ride(ride, ftp_outdoor, ftp_indoor)
        if tss is not None:
            m["tss"] += tss
        else:
            m["tss_missing"] += 1

    # Yearly aggregates
    yearly = defaultdict(lambda: {"rides": 0, "hours": 0.0, "tss": 0.0, "km": 0.0, "elev": 0.0, "indoor": 0, "outdoor": 0})
    for ym, m in monthly.items():
        y = ym[:4]
        for k in ["rides", "hours", "tss", "km", "elev", "indoor", "outdoor"]:
            yearly[y][k] += m[k]

    # Pre/post baby
    pre_baby = {"rides": 0, "hours": 0.0, "months": 0}
    post_baby = {"rides": 0, "hours": 0.0, "months": 0}
    for ym, m in sorted(monthly.items()):
        y, mo = int(ym[:4]), int(ym[5:7])
        d = date(y, mo, 1)
        bucket = post_baby if d >= date(BABY_DATE.year, BABY_DATE.month, 1) else pre_baby
        bucket["rides"] += m["rides"]
        bucket["hours"] += m["hours"]
        bucket["months"] += 1

    if pre_baby["months"] > 0:
        pre_baby["rides_per_month"] = pre_baby["rides"] / pre_baby["months"]
        pre_baby["hours_per_month"] = pre_baby["hours"] / pre_baby["months"]
    if post_baby["months"] > 0:
        post_baby["rides_per_month"] = post_baby["rides"] / post_baby["months"]
        post_baby["hours_per_month"] = post_baby["hours"] / post_baby["months"]

    return {"monthly": dict(monthly), "yearly": dict(yearly), "pre_baby": pre_baby, "post_baby": post_baby}


# ── B. Fitness Arc ────────────────────────────────────────────────────────────

def analyze_fitness_arc(pmc):
    if not pmc:
        return {}

    dates = [p["date"] for p in pmc]
    ctls = [p["ctl"] for p in pmc]
    atls = [p["atl"] for p in pmc]

    peak_idx = int(np.argmax(ctls))
    peak_ctl = {"date": dates[peak_idx], "value": ctls[peak_idx]}
    current_ctl = {"date": dates[-1], "value": ctls[-1]}

    # Per-year peaks
    yearly_peak = {}
    for p in pmc:
        y = str(p["date"].year) if isinstance(p["date"], date) else p["date"][:4]
        if y not in yearly_peak or p["ctl"] > yearly_peak[y]["value"]:
            yearly_peak[y] = {"date": p["date"], "value": p["ctl"]}

    # Notable buildups (CTL rises >15 sustained over >=3 weeks)
    buildups = []
    i = 0
    while i < len(ctls) - 21:
        if ctls[i + 21] - ctls[i] >= 15:
            start_i = i
            end_i = i + 21
            while end_i < len(ctls) - 7 and ctls[end_i + 7] > ctls[end_i] - 2:
                end_i += 7
            buildups.append({
                "start": dates[start_i], "end": dates[min(end_i, len(dates) - 1)],
                "ctl_start": ctls[start_i],
                "ctl_peak": max(ctls[start_i:min(end_i + 1, len(ctls))]),
                "weeks": (end_i - start_i) / 7,
            })
            i = end_i + 1
        else:
            i += 7

    # Notable breaks (CTL drops >20)
    breaks = []
    i = 0
    while i < len(ctls) - 14:
        if ctls[i] - ctls[i + 14] >= 20:
            start_i = i
            end_i = i + 14
            while end_i < len(ctls) - 7 and ctls[end_i + 7] < ctls[end_i] + 2:
                end_i += 7
            breaks.append({
                "start": dates[start_i], "end": dates[min(end_i, len(dates) - 1)],
                "ctl_start": ctls[start_i],
                "ctl_trough": min(ctls[start_i:min(end_i + 1, len(ctls))]),
                "weeks": (end_i - start_i) / 7,
            })
            i = end_i + 1
        else:
            i += 7

    return {
        "dates": dates, "ctls": ctls, "atls": atls,
        "peak_ctl": peak_ctl, "current_ctl": current_ctl,
        "yearly_peak": yearly_peak,
        "buildups": buildups, "breaks": breaks,
    }


# ── C. Power Progression ─────────────────────────────────────────────────────

def _load_stream(activity_id):
    path = os.path.join(CACHE_DIR, f"stream_{activity_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list) and data and isinstance(data[0], (int, float)):
        return np.array(data, dtype=float)
    if isinstance(data, list):
        for s in data:
            if isinstance(s, dict) and s.get("type") == "watts":
                return np.array(s["data"], dtype=float)
    return None


def analyze_power_progression(activities, weight_kg):
    durations = [60, 300, 1200]
    duration_labels = {60: "1-min", 300: "5-min", 1200: "20-min"}

    # Check cache
    if os.path.exists(POWER_HISTORY_CACHE):
        print("Loading power history from cache...")
        with open(POWER_HISTORY_CACHE) as f:
            cached = json.load(f)
        # Validate cache has the right structure
        if "quarterly_bests" in cached and "all_time_bests" in cached:
            return cached

    # Process streams
    quarterly_bests = defaultdict(lambda: {d: 0 for d in durations})
    monthly_bests = defaultdict(lambda: {d: 0 for d in durations})
    all_time_bests = {d: {"watts": 0, "date": None, "activity_id": None} for d in durations}

    rides_with_streams = 0
    rides_without_streams = 0

    rides_sorted = sorted(activities, key=lambda r: _ride_date(r))
    total = len(rides_sorted)

    for idx, ride in enumerate(rides_sorted):
        if idx % 50 == 0:
            print(f"  Processing streams: {idx}/{total}...")

        rd = _ride_date(ride)
        aid = ride.get("id")
        if not aid:
            continue

        watts = _load_stream(aid)
        if watts is None:
            rides_without_streams += 1
            continue

        rides_with_streams += 1
        y, m = _ym(rd)
        q = f"{y}-Q{(m - 1) // 3 + 1}"
        ym = _ym_key(rd)

        for d in durations:
            if len(watts) < d:
                continue
            kernel = np.ones(d) / d
            rolling = np.convolve(watts, kernel, mode="valid")
            best = float(rolling.max())

            if best > quarterly_bests[q][d]:
                quarterly_bests[q][d] = best
            if best > monthly_bests[ym][d]:
                monthly_bests[ym][d] = best
            if best > all_time_bests[d]["watts"]:
                all_time_bests[d]["watts"] = round(best)
                all_time_bests[d]["date"] = rd
                all_time_bests[d]["activity_id"] = aid
                all_time_bests[d]["wkg"] = round(best / weight_kg, 2)

    print(f"  Streams: {rides_with_streams} processed, {rides_without_streams} missing")

    # Round quarterly bests
    for q in quarterly_bests:
        for d in durations:
            quarterly_bests[q][d] = round(quarterly_bests[q][d])

    # FTP estimates from 20-min bests
    ftp_estimates = []
    for ym in sorted(monthly_bests.keys()):
        p20 = monthly_bests[ym][1200]
        if p20 > 0:
            ftp_estimates.append({"month": ym, "ftp_est": round(p20 * 0.95), "p20": round(p20)})

    result = {
        "quarterly_bests": dict(quarterly_bests),
        "monthly_bests": {k: dict(v) for k, v in monthly_bests.items()},
        "all_time_bests": all_time_bests,
        "ftp_estimates": ftp_estimates,
        "duration_labels": duration_labels,
        "rides_with_streams": rides_with_streams,
        "rides_without_streams": rides_without_streams,
        "weight_kg": weight_kg,
    }

    # Cache results
    os.makedirs(os.path.dirname(POWER_HISTORY_CACHE), exist_ok=True)
    with open(POWER_HISTORY_CACHE, "w") as f:
        json.dump(result, f)
    print(f"  Cached power history to {POWER_HISTORY_CACHE}")

    return result


# ── D. Aerobic Efficiency ─────────────────────────────────────────────────────

def analyze_aerobic_efficiency(activities, activity_metrics, ftp_outdoor, peloton_factor):
    # From activity_metrics table (precise EF)
    db_ef_monthly = defaultdict(list)
    db_decoupling_monthly = defaultdict(list)
    for am in activity_metrics:
        if am["ef"] and am["ef"] > 0:
            d = am["activity_date"]
            ym = _ym_key(d)
            db_ef_monthly[ym].append(am["ef"])
        if am.get("decoupling") is not None:
            d = am["activity_date"]
            ym = _ym_key(d)
            db_decoupling_monthly[ym].append(am["decoupling"])

    # From activity summaries (rough EF proxy for older rides)
    activity_ef_monthly = defaultdict(list)
    activity_ids_in_db = {am["activity_id"] for am in activity_metrics}

    for ride in activities:
        aid = ride.get("id")
        if aid in activity_ids_in_db:
            continue
        hr = ride.get("average_heartrate")
        np_watts = ride.get("weighted_average_watts") or ride.get("average_watts")
        if not hr or hr < 80 or not np_watts or np_watts <= 0:
            continue
        # Indoor correction
        if ride.get("trainer", False):
            np_watts = np_watts * peloton_factor
        ef = np_watts / hr
        rd = _ride_date(ride)
        ym = _ym_key(rd)
        activity_ef_monthly[ym].append(ef)

    # Combine: prefer DB EF, fallback to activity-level
    combined_ef = {}
    all_yms = sorted(set(list(db_ef_monthly.keys()) + list(activity_ef_monthly.keys())))
    for ym in all_yms:
        db_vals = db_ef_monthly.get(ym, [])
        act_vals = activity_ef_monthly.get(ym, [])
        if db_vals:
            combined_ef[ym] = {"avg": np.mean(db_vals), "median": np.median(db_vals),
                               "count": len(db_vals), "source": "db"}
        elif act_vals:
            combined_ef[ym] = {"avg": np.mean(act_vals), "median": np.median(act_vals),
                               "count": len(act_vals), "source": "activity"}

    # Decoupling trend
    decoupling_trend = {}
    for ym in sorted(db_decoupling_monthly.keys()):
        vals = db_decoupling_monthly[ym]
        decoupling_trend[ym] = {"avg": np.mean(vals), "count": len(vals)}

    # All individual EF points for scatter (from both sources)
    ef_scatter = []
    for am in activity_metrics:
        if am["ef"] and am["ef"] > 0:
            ef_scatter.append({"date": am["activity_date"], "ef": am["ef"], "source": "db"})
    for ride in activities:
        aid = ride.get("id")
        if aid in activity_ids_in_db:
            continue
        hr = ride.get("average_heartrate")
        np_watts = ride.get("weighted_average_watts") or ride.get("average_watts")
        if not hr or hr < 80 or not np_watts or np_watts <= 0:
            continue
        if ride.get("trainer", False):
            np_watts = np_watts * peloton_factor
        ef = np_watts / hr
        rd = _ride_date(ride)
        ef_scatter.append({"date": rd, "ef": ef, "source": "activity"})

    return {
        "combined_ef": combined_ef,
        "decoupling_trend": decoupling_trend,
        "ef_scatter": ef_scatter,
        "db_ef_count": sum(len(v) for v in db_ef_monthly.values()),
        "activity_ef_count": sum(len(v) for v in activity_ef_monthly.values()),
    }


# ── E. Seasonal Patterns ─────────────────────────────────────────────────────

def analyze_seasonal_patterns(pmc, monthly_stats):
    # CTL by month-of-year
    month_ctls = defaultdict(list)
    for p in pmc:
        d = p["date"]
        m = d.month if isinstance(d, date) else int(d[5:7])
        month_ctls[m].append(p["ctl"])

    month_avg_ctl = {m: np.mean(vals) for m, vals in month_ctls.items()}
    month_std_ctl = {m: np.std(vals) for m, vals in month_ctls.items()}

    # Volume by month-of-year
    month_volume = defaultdict(lambda: {"hours": [], "rides": []})
    for ym, stats in monthly_stats.items():
        m = int(ym[5:7])
        month_volume[m]["hours"].append(stats["hours"])
        month_volume[m]["rides"].append(stats["rides"])

    month_avg_vol = {}
    for m in range(1, 13):
        h = month_volume[m]["hours"]
        r = month_volume[m]["rides"]
        month_avg_vol[m] = {
            "avg_hours": np.mean(h) if h else 0,
            "avg_rides": np.mean(r) if r else 0,
        }

    # Label seasons
    ctl_values = [month_avg_ctl.get(m, 0) for m in range(1, 13)]
    if ctl_values:
        p80 = np.percentile(ctl_values, 80)
        p20 = np.percentile(ctl_values, 20)
    else:
        p80 = p20 = 0

    season_labels = {}
    for m in range(1, 13):
        c = month_avg_ctl.get(m, 0)
        if c >= p80:
            season_labels[m] = "build"
        elif c <= p20:
            season_labels[m] = "off"
        else:
            season_labels[m] = "maintain"

    peak_months = sorted(month_avg_ctl.keys(), key=lambda m: month_avg_ctl[m], reverse=True)[:3]
    trough_months = sorted(month_avg_ctl.keys(), key=lambda m: month_avg_ctl[m])[:3]

    return {
        "month_avg_ctl": month_avg_ctl,
        "month_std_ctl": month_std_ctl,
        "month_avg_vol": month_avg_vol,
        "season_labels": season_labels,
        "peak_months": peak_months,
        "trough_months": trough_months,
    }


# ── F. Dose-Response ─────────────────────────────────────────────────────────

def analyze_dose_response(pmc, monthly_stats):
    # Weekly CTL deltas and TSS
    weekly_data = []
    i = 0
    while i + 6 < len(pmc):
        week_start = pmc[i]["date"]
        week_end = pmc[min(i + 6, len(pmc) - 1)]["date"]
        ctl_start = pmc[i]["ctl"]
        ctl_end = pmc[min(i + 6, len(pmc) - 1)]["ctl"]
        week_tss = sum(p["tss"] for p in pmc[i:i + 7] if p["tss"])
        delta_ctl = ctl_end - ctl_start

        y = str(week_start.year) if isinstance(week_start, date) else week_start[:4]
        weekly_data.append({
            "week_start": week_start, "tss": week_tss,
            "delta_ctl": delta_ctl, "ctl": ctl_start, "year": y,
        })
        i += 7

    if len(weekly_data) < 10:
        return {"weekly_data": weekly_data, "model": None}

    tss_arr = np.array([w["tss"] for w in weekly_data])
    delta_arr = np.array([w["delta_ctl"] for w in weekly_data])

    # Filter out extreme outliers (>3 std)
    mask = np.abs(delta_arr - np.mean(delta_arr)) < 3 * np.std(delta_arr)
    tss_fit = tss_arr[mask]
    delta_fit = delta_arr[mask]

    # Linear regression
    coeffs = np.polyfit(tss_fit, delta_fit, 1)
    slope, intercept = coeffs[0], coeffs[1]

    # Maintenance TSS (where delta_ctl = 0)
    maintenance_tss = -intercept / slope if slope != 0 else 0

    # Decay and build rates
    low_tss_mask = tss_arr < 100
    high_tss_mask = tss_arr > 300
    decay_rate = float(np.mean(delta_arr[low_tss_mask])) if low_tss_mask.sum() > 3 else None
    build_rate = float(np.mean(delta_arr[high_tss_mask])) if high_tss_mask.sum() > 3 else None

    # Historical TSS/hour ratio
    total_tss = sum(m["tss"] for m in monthly_stats.values())
    total_hours = sum(m["hours"] for m in monthly_stats.values())
    tss_per_hour = total_tss / total_hours if total_hours > 0 else 60

    # Projections at different weekly hours
    projections = {}
    for hours_per_week in [5, 7, 10]:
        weekly_tss = hours_per_week * tss_per_hour
        weekly_gain = slope * weekly_tss + intercept
        projections[hours_per_week] = {
            "weekly_tss": round(weekly_tss),
            "weekly_ctl_gain": round(weekly_gain, 1),
            "ctl_4wk": round(weekly_gain * 4, 1),
            "ctl_8wk": round(weekly_gain * 8, 1),
            "ctl_12wk": round(weekly_gain * 12, 1),
        }

    return {
        "weekly_data": weekly_data,
        "model": {"slope": slope, "intercept": intercept},
        "maintenance_tss": round(maintenance_tss),
        "decay_rate": round(decay_rate, 1) if decay_rate else None,
        "build_rate": round(build_rate, 1) if build_rate else None,
        "tss_per_hour": round(tss_per_hour, 1),
        "projections": projections,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3: CHARTS
# ═══════════════════════════════════════════════════════════════════════════════

def _to_mpl_date(d):
    if isinstance(d, str):
        return datetime.strptime(d[:10], "%Y-%m-%d")
    if isinstance(d, date) and not isinstance(d, datetime):
        return datetime(d.year, d.month, d.day)
    return d


def generate_charts(volume, arc, power, efficiency, seasonal, dose):
    os.makedirs(CHART_DIR, exist_ok=True)
    chart_paths = {}

    # ── Chart 1: Fitness Arc ──────────────────────────────────────────────
    if arc.get("dates"):
        fig = styled_fig(figsize=(16, 5))
        ax = fig.add_subplot(111)
        style_ax(ax, title="Fitness Arc — CTL (Chronic Training Load)", ylabel="CTL")

        dates_mpl = [_to_mpl_date(d) for d in arc["dates"]]
        ax.fill_between(dates_mpl, arc["ctls"], alpha=0.25, color=ACCENT2)
        ax.plot(dates_mpl, arc["ctls"], color=ACCENT2, linewidth=1.5, label="CTL")
        ax.plot(dates_mpl, arc["atls"], color=ACCENT1, linewidth=0.8, alpha=0.4, label="ATL")

        # Baby line
        baby_dt = _to_mpl_date(BABY_DATE)
        ax.axvline(baby_dt, color=ACCENT3, linestyle="--", alpha=0.7, linewidth=1)
        ax.text(baby_dt, max(arc["ctls"]) * 0.95, " baby", color=ACCENT3, fontsize=8, va="top")

        # Peak annotation
        peak = arc["peak_ctl"]
        peak_dt = _to_mpl_date(peak["date"])
        ax.annotate(f'peak: {peak["value"]:.0f}', xy=(peak_dt, peak["value"]),
                     xytext=(15, 10), textcoords="offset points",
                     color=ACCENT4, fontsize=9, fontweight="bold",
                     arrowprops=dict(arrowstyle="->", color=ACCENT4, lw=0.8))

        # Current
        cur = arc["current_ctl"]
        cur_dt = _to_mpl_date(cur["date"])
        ax.annotate(f'now: {cur["value"]:.0f}', xy=(cur_dt, cur["value"]),
                     xytext=(-50, 10), textcoords="offset points",
                     color=TEXT, fontsize=9,
                     arrowprops=dict(arrowstyle="->", color=TEXT, lw=0.8))

        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax.legend(loc="upper left", fontsize=8, facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT)
        fig.tight_layout()
        path = os.path.join(CHART_DIR, "th_01_fitness_arc.png")
        fig.savefig(path, dpi=150, facecolor=BG)
        plt.close(fig)
        chart_paths["fitness_arc"] = path

    # ── Chart 2: Annual Volume ────────────────────────────────────────────
    if volume.get("yearly"):
        years = sorted(volume["yearly"].keys())
        hours = [volume["yearly"][y]["hours"] for y in years]
        rides = [volume["yearly"][y]["rides"] for y in years]

        fig = styled_fig(figsize=(14, 6))
        ax1 = fig.add_subplot(111)
        style_ax(ax1, title="Annual Training Volume", ylabel="Hours")

        x = np.arange(len(years))
        w = 0.35
        colors_h = [ACCENT3 if y == str(BABY_DATE.year) else ACCENT2 for y in years]
        bars1 = ax1.bar(x - w / 2, hours, w, color=colors_h, alpha=0.85, label="Hours")

        ax2 = ax1.twinx()
        ax2.tick_params(colors=TEXT, labelsize=9)
        ax2.yaxis.label.set_color(TEXT)
        colors_r = [ACCENT3 if y == str(BABY_DATE.year) else ACCENT4 for y in years]
        bars2 = ax2.bar(x + w / 2, rides, w, color=colors_r, alpha=0.6, label="Rides")
        ax2.set_ylabel("Ride Count", color=MUTED, fontsize=9)

        # Annotate bars
        for i, (h, r) in enumerate(zip(hours, rides)):
            ax1.text(x[i] - w / 2, h + 2, f"{h:.0f}h", ha="center", color=TEXT, fontsize=8)
            ax2.text(x[i] + w / 2, r + 1, str(r), ha="center", color=TEXT, fontsize=8)

        ax1.set_xticks(x)
        ax1.set_xticklabels(years, color=TEXT)

        # Combined legend
        ax1.legend([bars1, bars2], ["Hours", "Rides"], loc="upper left",
                   fontsize=8, facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT)

        fig.tight_layout()
        path = os.path.join(CHART_DIR, "th_02_annual_volume.png")
        fig.savefig(path, dpi=150, facecolor=BG)
        plt.close(fig)
        chart_paths["annual_volume"] = path

    # ── Chart 3: Monthly Heatmap ──────────────────────────────────────────
    if volume.get("monthly"):
        years_in_data = sorted(set(ym[:4] for ym in volume["monthly"].keys()))
        months = list(range(1, 13))
        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        grid = np.zeros((len(years_in_data), 12))
        hours_grid = np.zeros((len(years_in_data), 12))
        for yi, y in enumerate(years_in_data):
            for mi, m in enumerate(months):
                ym = f"{y}-{m:02d}"
                if ym in volume["monthly"]:
                    grid[yi, mi] = volume["monthly"][ym]["tss"]
                    hours_grid[yi, mi] = volume["monthly"][ym]["hours"]

        fig = styled_fig(figsize=(14, max(4, len(years_in_data) * 0.8 + 2)))
        ax = fig.add_subplot(111)
        style_ax(ax, title="Monthly Training Load (TSS)")

        im = ax.imshow(grid, cmap="YlOrRd", aspect="auto", interpolation="nearest")
        ax.set_xticks(range(12))
        ax.set_xticklabels(month_names, color=TEXT, fontsize=9)
        ax.set_yticks(range(len(years_in_data)))
        ax.set_yticklabels(years_in_data, color=TEXT, fontsize=9)

        # Annotate cells with hours
        for yi in range(len(years_in_data)):
            for mi in range(12):
                h = hours_grid[yi, mi]
                if h > 0:
                    text_color = "white" if grid[yi, mi] > np.max(grid) * 0.6 else TEXT
                    ax.text(mi, yi, f"{h:.0f}h", ha="center", va="center",
                            color=text_color, fontsize=7, fontweight="bold")

        cbar = fig.colorbar(im, ax=ax, shrink=0.8, label="Monthly TSS")
        cbar.ax.yaxis.label.set_color(TEXT)
        cbar.ax.tick_params(colors=TEXT)

        fig.tight_layout()
        path = os.path.join(CHART_DIR, "th_03_monthly_heatmap.png")
        fig.savefig(path, dpi=150, facecolor=BG)
        plt.close(fig)
        chart_paths["monthly_heatmap"] = path

    # ── Chart 4: Power Progression ────────────────────────────────────────
    if power.get("quarterly_bests"):
        fig = styled_fig(figsize=(14, 7))
        ax = fig.add_subplot(111)
        style_ax(ax, title="Power Progression — Quarterly Bests", ylabel="Watts")

        quarters = sorted(power["quarterly_bests"].keys())
        dur_colors = {60: ACCENT1, 300: ACCENT2, 1200: ACCENT3}
        dur_labels = power.get("duration_labels", {60: "1-min", 300: "5-min", 1200: "20-min"})

        for d in [60, 300, 1200]:
            vals = []
            qdates = []
            for q in quarters:
                v = power["quarterly_bests"][q].get(str(d) if isinstance(list(power["quarterly_bests"][q].keys())[0], str) else d, 0)
                if v > 0:
                    vals.append(v)
                    # Parse quarter to approximate date
                    y = int(q[:4])
                    qn = int(q[-1])
                    m = (qn - 1) * 3 + 2  # middle of quarter
                    qdates.append(datetime(y, m, 15))

            if vals:
                ax.plot(qdates, vals, color=dur_colors[d], marker="o", markersize=5,
                        linewidth=1.5, label=dur_labels[d], alpha=0.9)

        # ATH annotations
        for d in [60, 300, 1200]:
            ath = power["all_time_bests"].get(d) or power["all_time_bests"].get(str(d))
            if ath and ath.get("watts") and ath["watts"] > 0:
                ath_dt = _to_mpl_date(ath["date"])
                wkg = ath.get("wkg", ath["watts"] / power.get("weight_kg", 77))
                ax.annotate(f'{dur_labels[d]} ATH: {ath["watts"]}w ({wkg:.1f} W/kg)',
                            xy=(ath_dt, ath["watts"]),
                            xytext=(10, 8), textcoords="offset points",
                            color=dur_colors[d], fontsize=8, fontweight="bold",
                            arrowprops=dict(arrowstyle="->", color=dur_colors[d], lw=0.7))

        # W/kg secondary axis
        wkg_ax = ax.secondary_yaxis("right", functions=(lambda w: w / power.get("weight_kg", 77),
                                                          lambda wkg: wkg * power.get("weight_kg", 77)))
        wkg_ax.set_ylabel("W/kg", color=MUTED, fontsize=9)
        wkg_ax.tick_params(colors=TEXT, labelsize=8)

        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax.legend(loc="upper left", fontsize=9, facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT)
        fig.tight_layout()
        path = os.path.join(CHART_DIR, "th_04_power_progression.png")
        fig.savefig(path, dpi=150, facecolor=BG)
        plt.close(fig)
        chart_paths["power_progression"] = path

    # ── Chart 5: Aerobic Efficiency ───────────────────────────────────────
    if efficiency.get("ef_scatter"):
        fig = styled_fig(figsize=(14, 8))
        gs = fig.add_gridspec(2, 1, height_ratios=[2, 1], hspace=0.3)

        # Top: EF scatter + rolling average
        ax1 = fig.add_subplot(gs[0])
        style_ax(ax1, title="Aerobic Efficiency (EF = NP / Avg HR)", ylabel="EF")

        scatter_dates = []
        scatter_ef = []
        scatter_colors = []
        for pt in efficiency["ef_scatter"]:
            d = pt["date"]
            scatter_dates.append(_to_mpl_date(d))
            scatter_ef.append(pt["ef"])
            scatter_colors.append(ACCENT2 if pt["source"] == "db" else ACCENT3)

        ax1.scatter(scatter_dates, scatter_ef, c=scatter_colors, s=8, alpha=0.3, edgecolors="none")

        # Rolling average (90-day)
        if len(scatter_dates) > 20:
            sorted_pts = sorted(zip(scatter_dates, scatter_ef))
            s_dates, s_ef = zip(*sorted_pts)
            window = min(90, len(s_ef) // 3)
            if window >= 5:
                kernel = np.ones(window) / window
                rolling_ef = np.convolve(s_ef, kernel, mode="valid")
                roll_dates = s_dates[window - 1:]
                ax1.plot(roll_dates, rolling_ef, color=ACCENT4, linewidth=2, label=f"{window}-ride rolling avg")
                ax1.legend(loc="upper left", fontsize=8, facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT)

        ax1.xaxis.set_major_locator(mdates.YearLocator())
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

        # Bottom: Decoupling trend
        ax2 = fig.add_subplot(gs[1])
        style_ax(ax2, title="HR Decoupling (Z2 rides)", ylabel="Decoupling %", xlabel="Date")

        if efficiency.get("decoupling_trend"):
            dec_months = sorted(efficiency["decoupling_trend"].keys())
            dec_dates = [datetime(int(m[:4]), int(m[5:7]), 15) for m in dec_months]
            dec_vals = [efficiency["decoupling_trend"][m]["avg"] for m in dec_months]
            ax2.bar(dec_dates, dec_vals, width=25, color=ACCENT2, alpha=0.7)
            ax2.axhline(5, color=ACCENT4, linestyle="--", alpha=0.5, linewidth=0.8)
            ax2.text(dec_dates[0], 5.5, " <5% = well coupled", color=ACCENT4, fontsize=7)
        else:
            ax2.text(0.5, 0.5, "No decoupling data available", transform=ax2.transAxes,
                     ha="center", color=MUTED, fontsize=10)

        ax2.xaxis.set_major_locator(mdates.YearLocator())
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

        fig.tight_layout()
        path = os.path.join(CHART_DIR, "th_05_aerobic_efficiency.png")
        fig.savefig(path, dpi=150, facecolor=BG)
        plt.close(fig)
        chart_paths["aerobic_efficiency"] = path

    # ── Chart 6: Seasonal Radar ───────────────────────────────────────────
    if seasonal.get("month_avg_ctl"):
        fig = styled_fig(figsize=(8, 8))
        ax = fig.add_subplot(111, polar=True)
        ax.set_facecolor(PANEL)

        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        angles = np.linspace(0, 2 * np.pi, 12, endpoint=False).tolist()
        angles += angles[:1]  # close the loop

        # Normalize each series to 0-1
        ctl_vals = [seasonal["month_avg_ctl"].get(m, 0) for m in range(1, 13)]
        hours_vals = [seasonal["month_avg_vol"].get(m, {}).get("avg_hours", 0) for m in range(1, 13)]
        rides_vals = [seasonal["month_avg_vol"].get(m, {}).get("avg_rides", 0) for m in range(1, 13)]

        def _norm(vals):
            mx = max(vals) if max(vals) > 0 else 1
            return [v / mx for v in vals]

        for vals, color, label in [
            (_norm(ctl_vals), ACCENT2, "CTL"),
            (_norm(hours_vals), ACCENT3, "Hours"),
            (_norm(rides_vals), ACCENT4, "Rides"),
        ]:
            vals_closed = vals + vals[:1]
            ax.plot(angles, vals_closed, color=color, linewidth=1.5, label=label)
            ax.fill(angles, vals_closed, color=color, alpha=0.1)

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(month_names, color=TEXT, fontsize=9)
        ax.tick_params(colors=TEXT, labelsize=7)
        ax.set_yticklabels([])
        ax.spines["polar"].set_color(GREY)
        ax.grid(color=GREY, alpha=0.3)
        ax.legend(loc="upper right", bbox_to_anchor=(1.15, 1.15), fontsize=8,
                  facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT)
        ax.set_title("Seasonal Training Pattern", color=TEXT, fontsize=11, pad=20, fontweight="bold")

        fig.tight_layout()
        path = os.path.join(CHART_DIR, "th_06_seasonal_radar.png")
        fig.savefig(path, dpi=150, facecolor=BG)
        plt.close(fig)
        chart_paths["seasonal_radar"] = path

    # ── Chart 7: Dose-Response ────────────────────────────────────────────
    if dose.get("weekly_data") and dose.get("model"):
        fig = styled_fig(figsize=(10, 8))
        ax = fig.add_subplot(111)
        style_ax(ax, title="Dose-Response: Weekly TSS vs CTL Change",
                 xlabel="Weekly TSS", ylabel="Weekly CTL Change")

        wd = dose["weekly_data"]
        tss_vals = [w["tss"] for w in wd]
        delta_vals = [w["delta_ctl"] for w in wd]
        year_vals = [w["year"] for w in wd]

        # Color by year
        unique_years = sorted(set(year_vals))
        cmap = plt.cm.viridis
        year_colors = {y: cmap(i / max(1, len(unique_years) - 1)) for i, y in enumerate(unique_years)}
        colors = [year_colors[y] for y in year_vals]

        ax.scatter(tss_vals, delta_vals, c=colors, s=12, alpha=0.4, edgecolors="none")

        # Regression line
        model = dose["model"]
        x_line = np.linspace(0, max(tss_vals) * 1.1, 100)
        y_line = model["slope"] * x_line + model["intercept"]
        ax.plot(x_line, y_line, color=ACCENT1, linewidth=2, label="Linear fit")

        # Confidence band (approximate)
        residuals = np.array(delta_vals) - (model["slope"] * np.array(tss_vals) + model["intercept"])
        std_resid = np.std(residuals)
        ax.fill_between(x_line, y_line - std_resid, y_line + std_resid,
                         color=ACCENT1, alpha=0.1)

        # Maintenance TSS line
        maint = dose.get("maintenance_tss", 0)
        if 0 < maint < max(tss_vals):
            ax.axvline(maint, color=ACCENT3, linestyle="--", alpha=0.7)
            ax.text(maint + 10, max(delta_vals) * 0.8, f"Maintenance: {maint} TSS/wk",
                    color=ACCENT3, fontsize=8, rotation=90, va="top")

        ax.axhline(0, color=GREY, linewidth=0.5)

        # Year legend
        handles = [mpatches.Patch(color=year_colors[y], label=y) for y in unique_years]
        ax.legend(handles=handles, loc="upper left", fontsize=7,
                  facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT, ncol=2)

        fig.tight_layout()
        path = os.path.join(CHART_DIR, "th_07_dose_response.png")
        fig.savefig(path, dpi=150, facecolor=BG)
        plt.close(fig)
        chart_paths["dose_response"] = path

    # ── Chart 8: Current Snapshot Dashboard ───────────────────────────────
    fig = styled_fig(figsize=(14, 10))
    gs = fig.add_gridspec(2, 2, hspace=0.35, wspace=0.3)

    # Panel A: CTL vs Peak
    ax_a = fig.add_subplot(gs[0, 0])
    style_ax(ax_a, title="CTL: Current vs Peak")
    if arc.get("peak_ctl") and arc.get("current_ctl"):
        peak_v = arc["peak_ctl"]["value"]
        cur_v = arc["current_ctl"]["value"]
        bars = ax_a.barh(["Current", "Peak"], [cur_v, peak_v],
                          color=[ACCENT2, ACCENT4], alpha=0.8, height=0.5)
        ax_a.text(cur_v + 1, 0, f"{cur_v:.0f}", va="center", color=TEXT, fontsize=10, fontweight="bold")
        ax_a.text(peak_v + 1, 1, f"{peak_v:.0f}", va="center", color=TEXT, fontsize=10, fontweight="bold")
        pct = cur_v / peak_v * 100 if peak_v > 0 else 0
        ax_a.set_xlabel(f"{pct:.0f}% of all-time peak", color=MUTED, fontsize=9)

    # Panel B: Year volume pace
    ax_b = fig.add_subplot(gs[0, 1])
    style_ax(ax_b, title=f"{date.today().year} Volume Pace vs Best Year")
    if volume.get("yearly"):
        current_year = str(date.today().year)
        day_of_year = date.today().timetuple().tm_yday
        yearly = volume["yearly"]
        if current_year in yearly:
            cur_hours = yearly[current_year]["hours"]
            cur_rides = yearly[current_year]["rides"]
            # Find best year by hours (excluding current partial year)
            best_year = max((y for y in yearly if y != current_year), key=lambda y: yearly[y]["hours"], default=None)
            if best_year:
                best_hours = yearly[best_year]["hours"]
                projected = cur_hours / day_of_year * 365
                ax_b.barh(
                    [f"{current_year} (projected)", f"{current_year} (YTD)", f"Best ({best_year})"],
                    [projected, cur_hours, best_hours],
                    color=[ACCENT2 + "66", ACCENT2, ACCENT4], height=0.5,
                )
                ax_b.text(projected + 2, 0, f"{projected:.0f}h", va="center", color=TEXT, fontsize=9)
                ax_b.text(cur_hours + 2, 1, f"{cur_hours:.0f}h", va="center", color=TEXT, fontsize=9)
                ax_b.text(best_hours + 2, 2, f"{best_hours:.0f}h", va="center", color=TEXT, fontsize=9)

    # Panel C: Power bests vs ATH
    ax_c = fig.add_subplot(gs[1, 0])
    style_ax(ax_c, title="Recent Power vs All-Time Bests")
    if power.get("all_time_bests") and power.get("quarterly_bests"):
        dur_labels_map = power.get("duration_labels", {60: "1-min", 300: "5-min", 1200: "20-min"})
        quarters_sorted = sorted(power["quarterly_bests"].keys())
        recent_q = quarters_sorted[-1] if quarters_sorted else None

        dur_list = [60, 300, 1200]
        ath_vals = []
        recent_vals = []
        labels = []
        for d in dur_list:
            dk = d if d in power["all_time_bests"] else str(d)
            ath = power["all_time_bests"].get(dk, {})
            ath_w = ath.get("watts", 0) if isinstance(ath, dict) else 0
            recent_w = 0
            if recent_q:
                rq = power["quarterly_bests"].get(recent_q, {})
                recent_w = rq.get(d, rq.get(str(d), 0))
            ath_vals.append(ath_w)
            recent_vals.append(recent_w)
            labels.append(dur_labels_map.get(d, f"{d}s"))

        x = np.arange(len(labels))
        w = 0.35
        ax_c.bar(x - w / 2, recent_vals, w, color=ACCENT2, label="Recent Quarter", alpha=0.8)
        ax_c.bar(x + w / 2, ath_vals, w, color=ACCENT4, label="All-Time", alpha=0.8)
        ax_c.set_xticks(x)
        ax_c.set_xticklabels(labels, color=TEXT)
        ax_c.set_ylabel("Watts", color=MUTED, fontsize=9)

        for i in range(len(labels)):
            if recent_vals[i] > 0:
                ax_c.text(x[i] - w / 2, recent_vals[i] + 5, str(recent_vals[i]),
                          ha="center", color=TEXT, fontsize=8)
            if ath_vals[i] > 0:
                ax_c.text(x[i] + w / 2, ath_vals[i] + 5, str(ath_vals[i]),
                          ha="center", color=TEXT, fontsize=8)

        ax_c.legend(fontsize=8, facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT)

    # Panel D: CTL Projections
    ax_d = fig.add_subplot(gs[1, 1])
    style_ax(ax_d, title="12-Week CTL Projections", xlabel="Weeks", ylabel="CTL")
    if dose.get("projections") and arc.get("current_ctl"):
        current_ctl = arc["current_ctl"]["value"]
        weeks = list(range(13))
        proj_colors = {5: ACCENT1, 7: ACCENT2, 10: ACCENT4}
        for hrs, proj in sorted(dose["projections"].items()):
            weekly_gain = proj["weekly_ctl_gain"]
            ctl_proj = [current_ctl + weekly_gain * w for w in weeks]
            ax_d.plot(weeks, ctl_proj, color=proj_colors.get(hrs, GREY),
                      linewidth=2, label=f"{hrs}h/wk (+{weekly_gain}/wk)")

        ax_d.axhline(current_ctl, color=GREY, linestyle=":", alpha=0.5)
        ax_d.text(0.5, current_ctl + 0.5, f"Current: {current_ctl:.0f}", color=MUTED, fontsize=8)
        ax_d.legend(fontsize=8, facecolor=PANEL, edgecolor=GREY, labelcolor=TEXT)

    fig.tight_layout()
    path = os.path.join(CHART_DIR, "th_08_current_snapshot.png")
    fig.savefig(path, dpi=150, facecolor=BG)
    plt.close(fig)
    chart_paths["current_snapshot"] = path

    return chart_paths


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4: MARKDOWN SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

def generate_summary_md(volume, arc, power, efficiency, seasonal, dose, start_year):
    month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    lines = []
    lines.append("# Training History Summary")
    lines.append(f"\n*Generated {date.today().isoformat()} — data from {start_year} to present*\n")

    # Key stats
    total_rides = sum(y["rides"] for y in volume.get("yearly", {}).values())
    total_hours = sum(y["hours"] for y in volume.get("yearly", {}).values())
    lines.append("## Key Stats\n")
    lines.append(f"- **Total rides (since {start_year}):** {total_rides}")
    lines.append(f"- **Total hours:** {total_hours:.0f}")

    if arc.get("peak_ctl"):
        lines.append(f"- **Peak CTL:** {arc['peak_ctl']['value']:.0f} ({arc['peak_ctl']['date']})")
    if arc.get("current_ctl"):
        lines.append(f"- **Current CTL:** {arc['current_ctl']['value']:.0f}")
        if arc.get("peak_ctl") and arc["peak_ctl"]["value"] > 0:
            pct = arc["current_ctl"]["value"] / arc["peak_ctl"]["value"] * 100
            lines.append(f"- **Current vs peak:** {pct:.0f}%")

    if power.get("all_time_bests"):
        lines.append("\n## All-Time Power Bests (from streams)\n")
        for d in [60, 300, 1200]:
            dk = d if d in power["all_time_bests"] else str(d)
            ath = power["all_time_bests"].get(dk, {})
            if ath and ath.get("watts"):
                label = {60: "1-min", 300: "5-min", 1200: "20-min"}.get(d, f"{d}s")
                lines.append(f"- **{label}:** {ath['watts']}w ({ath.get('wkg', 0):.1f} W/kg) — {ath.get('date', '?')}")

    # Volume pre/post baby
    pre = volume.get("pre_baby", {})
    post = volume.get("post_baby", {})
    if pre.get("months") and post.get("months"):
        lines.append("\n## Pre/Post Baby Volume\n")
        lines.append(f"- **Pre-baby (avg/month):** {pre.get('rides_per_month', 0):.1f} rides, {pre.get('hours_per_month', 0):.1f} hours")
        lines.append(f"- **Post-baby (avg/month):** {post.get('rides_per_month', 0):.1f} rides, {post.get('hours_per_month', 0):.1f} hours")
        if pre.get("hours_per_month", 0) > 0:
            pct_change = (post.get("hours_per_month", 0) / pre["hours_per_month"] - 1) * 100
            lines.append(f"- **Volume change:** {pct_change:+.0f}%")

    # Seasonal patterns
    if seasonal.get("peak_months"):
        lines.append("\n## Seasonal Patterns\n")
        peaks = [month_names[m] for m in seasonal["peak_months"]]
        troughs = [month_names[m] for m in seasonal.get("trough_months", [])]
        lines.append(f"- **Peak fitness months:** {', '.join(peaks)}")
        lines.append(f"- **Off-season months:** {', '.join(troughs)}")

    # Dose-response
    if dose.get("model"):
        lines.append("\n## Dose-Response Model\n")
        lines.append(f"- **Maintenance TSS:** ~{dose.get('maintenance_tss', '?')} TSS/week")
        if dose.get("decay_rate") is not None:
            lines.append(f"- **Decay rate (rest weeks):** {dose['decay_rate']} CTL/week")
        if dose.get("build_rate") is not None:
            lines.append(f"- **Build rate (hard weeks):** +{dose['build_rate']} CTL/week")
        lines.append(f"- **Avg TSS per hour:** {dose.get('tss_per_hour', '?')}")
        if dose.get("projections"):
            lines.append("\n### CTL Projections (from current fitness)\n")
            lines.append("| Hours/week | Weekly TSS | 4-wk gain | 8-wk gain | 12-wk gain |")
            lines.append("|------------|-----------|-----------|-----------|------------|")
            for hrs in sorted(dose["projections"].keys()):
                p = dose["projections"][hrs]
                lines.append(f"| {hrs}h | {p['weekly_tss']} | {p['ctl_4wk']:+.0f} | {p['ctl_8wk']:+.0f} | {p['ctl_12wk']:+.0f} |")

    # EF summary
    if efficiency.get("combined_ef"):
        lines.append("\n## Aerobic Efficiency\n")
        efs = efficiency["combined_ef"]
        recent_months = sorted(efs.keys())[-6:]
        if recent_months:
            recent_avg = np.mean([efs[m]["avg"] for m in recent_months])
            lines.append(f"- **Recent 6-month avg EF:** {recent_avg:.2f}")
        earliest_months = sorted(efs.keys())[:6]
        if earliest_months:
            early_avg = np.mean([efs[m]["avg"] for m in earliest_months])
            lines.append(f"- **Earliest 6-month avg EF:** {early_avg:.2f}")
        lines.append(f"- **DB EF data points:** {efficiency.get('db_ef_count', 0)}")
        lines.append(f"- **Activity-level EF proxy points:** {efficiency.get('activity_ef_count', 0)}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 5: CLAUDE COACHING INTERPRETATION
# ═══════════════════════════════════════════════════════════════════════════════

def get_coaching_interpretation(summary_md):
    import anthropic

    system_prompt = """You are an expert cycling coach with deep experience in power-based training,
periodization, and athlete development. You are reviewing a data scientist athlete's multi-year
training history analysis. The athlete is 77kg, rides in San Francisco/Marin, has a new baby
(September 2025), and has limited training time (~5-7 hours/week available).

Provide a thorough coaching interpretation structured as:

1. **Aerobic Base Assessment** — What the CTL history, EF trends, and volume patterns tell you
   about their aerobic development trajectory.

2. **Power Profile Analysis** — Interpret the power curve shape. Are they punchy, diesel, or
   balanced? Where are the biggest opportunities?

3. **Training Efficiency** — Given the dose-response data, how efficiently does their body
   respond to training? What does the maintenance TSS tell you?

4. **Seasonal & Lifestyle Insights** — How has the baby affected training? What do the seasonal
   patterns suggest for block scheduling?

5. **Where to Focus (Limited Time)** — With 5-7h/week, what delivers the most ROI? Be specific
   about session types, frequencies, and durations.

6. **SMART Goals (Next 3-6 Months)** — Provide 3-4 specific, measurable, achievable, relevant,
   time-bound goals based on the data. Reference specific power targets, CTL levels, etc.

7. **What to Deprioritize** — What should they stop doing or reduce given time constraints?

Be data-driven. Reference the specific numbers from the analysis. Be encouraging but honest.
Format as clean HTML paragraphs and lists (no markdown, use <h3>, <p>, <ul>, <li> tags)."""

    user_prompt = f"""Here is the complete training history analysis for this athlete:

{summary_md}

Please provide your coaching interpretation based on this data."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        print(f"Warning: Claude coaching interpretation failed: {e}")
        return "<p><em>Coaching interpretation unavailable (API error).</em></p>"


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 6: HTML REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def _embed_chart(path):
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f'<img src="data:image/png;base64,{b64}" style="width:100%; max-width:1200px; border-radius:8px; margin:16px 0;">'


def generate_html_report(volume, arc, power, efficiency, seasonal, dose,
                          chart_paths, summary_md, coaching_html, start_year):
    month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Executive summary stats
    total_rides = sum(y["rides"] for y in volume.get("yearly", {}).values())
    total_hours = sum(y["hours"] for y in volume.get("yearly", {}).values())
    peak_ctl = arc.get("peak_ctl", {})
    current_ctl = arc.get("current_ctl", {})
    pct_of_peak = (current_ctl.get("value", 0) / peak_ctl.get("value", 1) * 100) if peak_ctl.get("value") else 0

    # Pre/post baby
    pre = volume.get("pre_baby", {})
    post = volume.get("post_baby", {})
    baby_impact = ""
    if pre.get("hours_per_month") and post.get("hours_per_month"):
        pct_change = (post["hours_per_month"] / pre["hours_per_month"] - 1) * 100
        baby_impact = f"Post-baby volume: {pct_change:+.0f}% ({post['hours_per_month']:.0f} vs {pre['hours_per_month']:.0f} hrs/month)"

    # Power bests
    power_bests_html = ""
    if power.get("all_time_bests"):
        for d in [60, 300, 1200]:
            dk = d if d in power["all_time_bests"] else str(d)
            ath = power["all_time_bests"].get(dk, {})
            if ath and ath.get("watts"):
                label = {60: "1-min", 300: "5-min", 1200: "20-min"}.get(d)
                power_bests_html += f'<li><strong>{label}:</strong> {ath["watts"]}w ({ath.get("wkg", 0):.1f} W/kg)</li>'

    # Seasonal peaks/troughs
    season_html = ""
    if seasonal.get("peak_months"):
        peaks = ", ".join(month_names[m] for m in seasonal["peak_months"])
        troughs = ", ".join(month_names[m] for m in seasonal.get("trough_months", []))
        season_html = f"<p>Peak fitness months: <strong>{peaks}</strong>. Off-season: <strong>{troughs}</strong>.</p>"

    # Dose-response projections table
    proj_html = ""
    if dose.get("projections"):
        proj_html = """<table style="width:100%; border-collapse:collapse; margin:12px 0;">
        <tr style="border-bottom:1px solid #333;">
            <th style="text-align:left; padding:6px; color:#888;">Hours/wk</th>
            <th style="text-align:right; padding:6px; color:#888;">Weekly TSS</th>
            <th style="text-align:right; padding:6px; color:#888;">4-wk CTL</th>
            <th style="text-align:right; padding:6px; color:#888;">8-wk CTL</th>
            <th style="text-align:right; padding:6px; color:#888;">12-wk CTL</th>
        </tr>"""
        for hrs in sorted(dose["projections"].keys()):
            p = dose["projections"][hrs]
            proj_html += f"""<tr style="border-bottom:1px solid #222;">
                <td style="padding:6px; color:{TEXT};">{hrs}h</td>
                <td style="text-align:right; padding:6px; color:{TEXT};">{p['weekly_tss']}</td>
                <td style="text-align:right; padding:6px; color:{ACCENT4};">{p['ctl_4wk']:+.0f}</td>
                <td style="text-align:right; padding:6px; color:{ACCENT4};">{p['ctl_8wk']:+.0f}</td>
                <td style="text-align:right; padding:6px; color:{ACCENT4};">{p['ctl_12wk']:+.0f}</td>
            </tr>"""
        proj_html += "</table>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Training History Analysis</title>
<style>
body {{
    background: {BG};
    color: {TEXT};
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    max-width: 1200px;
    margin: 0 auto;
    padding: 20px 40px;
    line-height: 1.6;
}}
h1 {{ color: {ACCENT2}; font-size: 1.8em; border-bottom: 2px solid {GREY}; padding-bottom: 12px; }}
h2 {{ color: {ACCENT3}; font-size: 1.3em; margin-top: 40px; border-bottom: 1px solid #333; padding-bottom: 8px; }}
h3 {{ color: {ACCENT4}; font-size: 1.1em; margin-top: 24px; }}
p {{ color: {TEXT}; margin: 8px 0; }}
ul {{ padding-left: 24px; }}
li {{ margin: 4px 0; }}
strong {{ color: {ACCENT4}; }}
.stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin: 20px 0;
}}
.stat-card {{
    background: {PANEL};
    border: 1px solid {GREY};
    border-radius: 8px;
    padding: 16px;
    text-align: center;
}}
.stat-value {{ font-size: 2em; color: {ACCENT2}; font-weight: bold; }}
.stat-label {{ font-size: 0.85em; color: {MUTED}; margin-top: 4px; }}
.section {{ margin: 32px 0; }}
.coach-section {{
    background: #1a2a1a;
    border: 1px solid {ACCENT4};
    border-radius: 8px;
    padding: 24px;
    margin: 32px 0;
}}
.coach-section h2 {{ color: {ACCENT4}; border-bottom-color: {ACCENT4}; margin-top: 0; }}
.coach-section h3 {{ color: {ACCENT3}; }}
table {{ color: {TEXT}; }}
</style>
</head>
<body>

<h1>Training History Analysis</h1>
<p style="color:{MUTED};">Generated {date.today().isoformat()} &mdash; Data from {start_year} to present</p>

<h2>Executive Summary</h2>

<div class="stat-grid">
    <div class="stat-card">
        <div class="stat-value">{total_rides}</div>
        <div class="stat-label">Total Rides</div>
    </div>
    <div class="stat-card">
        <div class="stat-value">{total_hours:.0f}h</div>
        <div class="stat-label">Total Hours</div>
    </div>
    <div class="stat-card">
        <div class="stat-value">{peak_ctl.get('value', 0):.0f}</div>
        <div class="stat-label">Peak CTL</div>
    </div>
    <div class="stat-card">
        <div class="stat-value">{current_ctl.get('value', 0):.0f}</div>
        <div class="stat-label">Current CTL ({pct_of_peak:.0f}% of peak)</div>
    </div>
</div>

{f'<p>{baby_impact}</p>' if baby_impact else ''}

{f'<p><strong>All-time power bests:</strong></p><ul>{power_bests_html}</ul>' if power_bests_html else ''}

<div class="section">
<h2>Fitness Arc</h2>
<p>Your CTL (Chronic Training Load) over {date.today().year - start_year} years. CTL is a rolling 42-day
exponentially weighted average of daily TSS &mdash; it represents your accumulated fitness.</p>
{_embed_chart(chart_paths.get('fitness_arc', ''))}
</div>

<div class="section">
<h2>Training Volume</h2>
<p>Annual hours and ride count, with {BABY_DATE.year} (baby year) highlighted.</p>
{_embed_chart(chart_paths.get('annual_volume', ''))}
<p>Monthly training load heatmap &mdash; darker cells = higher TSS. Cell values show hours.</p>
{_embed_chart(chart_paths.get('monthly_heatmap', ''))}
</div>

<div class="section">
<h2>Power Progression</h2>
<p>Quarterly best efforts at 1-min, 5-min, and 20-min durations, computed from {power.get('rides_with_streams', '?')} cached power streams.
{power.get('rides_without_streams', '?')} rides had no stream data available.</p>
{_embed_chart(chart_paths.get('power_progression', ''))}
</div>

<div class="section">
<h2>Aerobic Efficiency</h2>
<p>Efficiency Factor (EF = Normalized Power / Avg HR) tracks aerobic fitness independently of power.
A rising EF means you produce more watts per heartbeat. Data sources: {efficiency.get('db_ef_count', 0)} precise DB records,
{efficiency.get('activity_ef_count', 0)} activity-level proxy calculations.</p>
{_embed_chart(chart_paths.get('aerobic_efficiency', ''))}
</div>

<div class="section">
<h2>Seasonal Patterns</h2>
{season_html}
<p>Radar chart shows the &ldquo;shape&rdquo; of your typical training year (averaged across all years). Use this for block scheduling.</p>
{_embed_chart(chart_paths.get('seasonal_radar', ''))}
</div>

<div class="section">
<h2>Dose-Response</h2>
<p>How does your body respond to training load? Each dot is one week &mdash; X axis is weekly TSS, Y axis is how much your CTL changed that week.</p>
{_embed_chart(chart_paths.get('dose_response', ''))}
{f'<p><strong>Maintenance TSS:</strong> ~{dose.get("maintenance_tss", "?")} TSS/week (below this, fitness decays).</p>' if dose.get('maintenance_tss') else ''}
{f'<p><strong>Avg TSS per hour:</strong> {dose.get("tss_per_hour", "?")} (your historical ride intensity).</p>' if dose.get('tss_per_hour') else ''}
{proj_html}
</div>

<div class="section">
<h2>Current Snapshot</h2>
<p>Where you stand today: CTL vs peak, year volume pace, recent power vs all-time, and 12-week projections.</p>
{_embed_chart(chart_paths.get('current_snapshot', ''))}
</div>

<div class="coach-section">
<h2>Coach's Analysis</h2>
{coaching_html}
</div>

<p style="color:{MUTED}; font-size:0.8em; margin-top:40px; border-top:1px solid #333; padding-top:12px;">
Generated by training_history.py &mdash; {date.today().isoformat()}
</p>

</body>
</html>"""

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write(html)
    print(f"HTML report saved to {REPORT_PATH}")
    return REPORT_PATH


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    # Parse args
    skip_coach = "--no-coach" in sys.argv
    start_year = 2020
    for arg in sys.argv[1:]:
        if arg.startswith("--from="):
            start_year = int(arg.split("=")[1])

    print(f"=== Training History Analysis (from {start_year}) ===\n")

    # 1. Load data
    print("Loading data...")
    data = load_all_data(start_year)

    # 2. Analyze
    print("\nAnalyzing training volume...")
    volume = analyze_training_volume(data["activities"], data["pmc"],
                                      data["ftp_outdoor"], data["ftp_indoor"])

    print("Analyzing fitness arc...")
    arc = analyze_fitness_arc(data["pmc"])

    print("Analyzing power progression (processing streams)...")
    power = analyze_power_progression(data["activities"], data["weight_kg"])

    print("Analyzing aerobic efficiency...")
    efficiency = analyze_aerobic_efficiency(data["activities"], data["activity_metrics"],
                                             data["ftp_outdoor"], data["peloton_factor"])

    print("Analyzing seasonal patterns...")
    seasonal = analyze_seasonal_patterns(data["pmc"], volume["monthly"])

    print("Analyzing dose-response...")
    dose = analyze_dose_response(data["pmc"], volume["monthly"])

    # 3. Generate charts
    print("\nGenerating charts...")
    chart_paths = generate_charts(volume, arc, power, efficiency, seasonal, dose)
    print(f"Generated {len(chart_paths)} charts")

    # 4. Markdown summary
    print("\nGenerating markdown summary...")
    summary_md = generate_summary_md(volume, arc, power, efficiency, seasonal, dose, start_year)
    with open(SUMMARY_PATH, "w") as f:
        f.write(summary_md)
    print(f"Summary saved to {SUMMARY_PATH}")

    # 5. Coaching interpretation
    coaching_html = ""
    if not skip_coach:
        print("\nGetting coaching interpretation from Claude...")
        coaching_html = get_coaching_interpretation(summary_md)
        # Append to summary
        with open(SUMMARY_PATH, "a") as f:
            f.write("\n\n## Coach's Interpretation\n\n")
            f.write(coaching_html)
        print("Coaching interpretation received")
    else:
        coaching_html = "<p><em>Skipped (--no-coach flag).</em></p>"

    # 6. HTML report
    print("\nGenerating HTML report...")
    report_path = generate_html_report(volume, arc, power, efficiency, seasonal, dose,
                                        chart_paths, summary_md, coaching_html, start_year)

    print(f"\n=== Done! ===")
    print(f"  Report: {report_path}")
    print(f"  Summary: {SUMMARY_PATH}")
    print(f"  Charts: {CHART_DIR}/th_*.png")


if __name__ == "__main__":
    main()
