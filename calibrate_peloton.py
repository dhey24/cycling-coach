"""
calibrate_peloton.py — One-time script to estimate outdoor FTP via Critical Power model
and compute Peloton power meter calibration factor.

Usage:
    python calibrate_peloton.py

Requires in .env:
    STRAVA_ACCESS_TOKEN=...
    PELOTON_FTP_KNOWN=220   # your tested FTP on Peloton

Method:
    Fetches last 52 weeks of outdoor rides with power data, computes best rolling
    averages at multiple durations, then fits the 2-parameter Critical Power model:
        P(t) = CP + W'/t
    CP (the asymptote) is the estimated outdoor FTP.
"""

import os
import sys
import time
import json
import numpy as np
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from scipy.optimize import curve_fit

load_dotenv()

# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_access_token():
    """Exchange refresh token for a fresh access token. Updates .env in place."""
    client_id     = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip()

    if not all([client_id, client_secret, refresh_token]):
        return  # fall back to existing access token

    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     client_id,
        "client_secret": client_secret,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    data = resp.json()

    new_access  = data["access_token"]
    new_refresh = data["refresh_token"]

    os.environ["STRAVA_ACCESS_TOKEN"]  = new_access
    os.environ["STRAVA_REFRESH_TOKEN"] = new_refresh

    # Persist updated tokens back to .env
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            content = f.read()
        import re
        content = re.sub(r"STRAVA_ACCESS_TOKEN=.*",  f"STRAVA_ACCESS_TOKEN={new_access}",  content)
        content = re.sub(r"STRAVA_REFRESH_TOKEN=.*", f"STRAVA_REFRESH_TOKEN={new_refresh}", content)
        with open(env_path, "w") as f:
            f.write(content)

    print(f"Access token refreshed.")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DURATIONS = [60, 120, 180, 300, 480, 600, 1200, 1800, 3600]  # seconds
DURATION_LABELS = {
    60: "1-min",
    120: "2-min",
    180: "3-min",
    300: "5-min",
    480: "8-min",
    600: "10-min",
    1200: "20-min",
    1800: "30-min",
    3600: "60-min",
}

STRAVA_BASE = "https://www.strava.com/api/v3"

# Rate limit: 100 req/15min. Fetch streams for up to 30 rides safely.
MAX_STREAM_FETCHES = 30
RATE_LIMIT_DELAY = 0.5  # seconds between requests

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def cache_path(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{key}.json")


def load_cache(key):
    path = cache_path(key)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None


def save_cache(key, data):
    with open(cache_path(key), "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Strava API helpers
# ---------------------------------------------------------------------------

def get_headers():
    token = os.environ.get("STRAVA_ACCESS_TOKEN")
    if not token:
        print("ERROR: STRAVA_ACCESS_TOKEN not set in .env")
        sys.exit(1)
    return {"Authorization": f"Bearer {token}"}


def fetch_activities(weeks=52):
    """Fetch all activities from the past N weeks. Cached to disk."""
    cached = load_cache("activities")
    if cached is not None:
        print(f"Loaded {len(cached)} activities from cache.")
        return cached

    after_ts = int((datetime.now() - timedelta(weeks=weeks)).timestamp())
    headers = get_headers()
    activities = []
    page = 1

    print(f"Fetching activities from past {weeks} weeks...")

    while True:
        resp = requests.get(
            f"{STRAVA_BASE}/athlete/activities",
            headers=headers,
            params={"per_page": 200, "page": page, "after": after_ts},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        print(f"  Page {page}: {len(batch)} activities (total so far: {len(activities)})")
        if len(batch) < 200:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    save_cache("activities", activities)
    return activities


def fetch_power_stream(activity_id):
    """Fetch second-by-second power data for an activity. Cached to disk."""
    cache_key = f"stream_{activity_id}"
    cached = load_cache(cache_key)
    if cached is not None:
        return cached  # may be [] to indicate "no stream"

    headers = get_headers()
    resp = requests.get(
        f"{STRAVA_BASE}/activities/{activity_id}/streams",
        headers=headers,
        params={"keys": "watts", "key_by_type": "true"},
    )
    if resp.status_code == 404:
        save_cache(cache_key, [])
        return None
    resp.raise_for_status()
    data = resp.json()
    watts_stream = data.get("watts")
    result = watts_stream.get("data") if watts_stream else None
    save_cache(cache_key, result if result is not None else [])
    return result


# ---------------------------------------------------------------------------
# Critical Power model
# ---------------------------------------------------------------------------

def cp_model(t, CP, Wprime):
    return CP + Wprime / t


def fit_cp_model(durations, powers):
    """Fit CP model and return (CP, W', r_squared)."""
    popt, _ = curve_fit(
        cp_model,
        durations,
        powers,
        p0=[250, 20000],
        bounds=([100, 0], [600, 100000]),
        maxfev=10000,
    )
    cp, w_prime = popt

    # R² calculation
    predicted = cp_model(np.array(durations), cp, w_prime)
    ss_res = np.sum((np.array(powers) - predicted) ** 2)
    ss_tot = np.sum((np.array(powers) - np.mean(powers)) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return cp, w_prime, r_squared


# ---------------------------------------------------------------------------
# Rolling max helper
# ---------------------------------------------------------------------------

def best_rolling_avg(watts_arr, duration):
    """Compute best (max) rolling average power over `duration` seconds."""
    if len(watts_arr) < duration:
        return None
    kernel = np.ones(duration) / duration
    rolling = np.convolve(watts_arr, kernel, mode="valid")
    return float(rolling.max())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Refresh access token using refresh token + client credentials
    refresh_access_token()

    # Load known Peloton FTP
    peloton_ftp_str = os.environ.get("PELOTON_FTP_KNOWN")
    if not peloton_ftp_str:
        peloton_ftp_str = input("Enter your known Peloton FTP (from your last test): ").strip()
    peloton_ftp = float(peloton_ftp_str)

    # 1. Fetch activities
    all_activities = fetch_activities(weeks=52)
    print(f"\nTotal activities fetched: {len(all_activities)}")

    # 2. Filter to outdoor rides with power data (trainer=False, has average_watts, >3 min)
    outdoor_rides = [
        r for r in all_activities
        if r.get("type") == "Ride"
        and not r.get("trainer", False)
        and r.get("average_watts") is not None
        and r.get("moving_time", 0) > 180
    ]
    print(f"Outdoor rides with power data (>3 min): {len(outdoor_rides)}")

    if not outdoor_rides:
        print("\nNo qualifying outdoor rides found. Exiting.")
        sys.exit(1)

    # Sort by moving_time descending to prioritize longer rides for stream fetches
    outdoor_rides.sort(key=lambda r: r["moving_time"], reverse=True)
    rides_to_fetch = outdoor_rides[:MAX_STREAM_FETCHES]

    # 3. Fetch power streams and compute rolling maxima
    best_power = {d: 0.0 for d in DURATIONS}
    rides_with_streams = 0
    skipped = 0

    print(f"\nFetching power streams for up to {MAX_STREAM_FETCHES} rides...")

    for i, ride in enumerate(rides_to_fetch):
        ride_date = ride.get("start_date_local", "")[:10]
        ride_name = ride.get("name", "Unnamed")
        print(f"  [{i+1}/{len(rides_to_fetch)}] {ride_date} — {ride_name} ({ride['moving_time']//60} min)", end="", flush=True)

        watts = fetch_power_stream(ride["id"])
        time.sleep(RATE_LIMIT_DELAY)

        if not watts:
            print(" → no power stream, skipping")
            skipped += 1
            continue

        rides_with_streams += 1
        watts_arr = np.array(watts, dtype=float)

        improvements = []
        for d in DURATIONS:
            best = best_rolling_avg(watts_arr, d)
            if best is not None and best > best_power[d]:
                best_power[d] = best
                improvements.append(DURATION_LABELS[d])

        if improvements:
            print(f" → new PRs at: {', '.join(improvements)}")
        else:
            print(" → no new PRs")

    print(f"\nStreams fetched: {rides_with_streams} | No stream: {skipped}")

    # 4. Filter to durations where we have valid data
    valid_pairs = [(d, p) for d, p in best_power.items() if p > 0]
    if len(valid_pairs) < 3:
        print(f"\nNot enough data points to fit CP model (need ≥3, got {len(valid_pairs)}). Exiting.")
        sys.exit(1)

    valid_pairs.sort()
    durations_arr = np.array([d for d, _ in valid_pairs])
    powers_arr = np.array([p for _, p in valid_pairs])

    # 5. Fit CP model
    try:
        cp, w_prime, r_squared = fit_cp_model(durations_arr, powers_arr)
    except Exception as e:
        print(f"\nCP model fitting failed: {e}")
        sys.exit(1)

    # 6. Calibration factor
    calibration_factor = cp / peloton_ftp
    pct_low = (calibration_factor - 1.0) * 100

    # ---------------------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------------------

    print("\n" + "=" * 50)
    print("=== Peloton Power Calibration Results ===")
    print("=" * 50)

    print(f"\nOutdoor rides analyzed: {rides_with_streams} (streams fetched successfully)")

    print("\nPower Duration Curve (outdoor best efforts):")
    for d in DURATIONS:
        label = DURATION_LABELS[d]
        p = best_power[d]
        if p > 0:
            tag = ""
        else:
            # Extrapolate from model
            p = cp_model(d, cp, w_prime)
            tag = "  (extrapolated)"
        print(f"  {label:<8} {p:>5.0f}w{tag}")

    print(f"\nCritical Power model fit:")
    print(f"  CP (estimated FTP): {cp:.0f}w")
    print(f"  W' (anaerobic capacity): {w_prime/1000:.1f} kJ")
    print(f"  Model R²: {r_squared:.3f}")

    print(f"\nYour known Peloton FTP:    {peloton_ftp:.0f}w")
    print(f"Calibration factor:        {calibration_factor:.2f}x  ← Peloton reads ~{pct_low:.0f}% low")

    print("\nRecommended values for preferences.md:")
    print(f"  FTP_OUTDOOR: {cp:.0f}w    (from CP model)")
    print(f"  FTP_INDOOR:  {peloton_ftp:.0f}w    (raw Peloton test value — use as-is for indoor TSS)")

    if calibration_factor < 1.05:
        print("\n⚠  Factor is very close to 1.0 — Peloton may read accurately, or outdoor data is limited.")
    elif calibration_factor > 1.45:
        print("\n⚠  Factor is unusually high — check that trainer=False filter is working and outdoor rides have real power.")

    print()


if __name__ == "__main__":
    main()
