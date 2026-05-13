"""
strava_client.py — Strava OAuth token refresh + activity fetching.
"""

import os
import re
import time
import json
import requests
from datetime import datetime, timedelta

STRAVA_BASE = "https://www.strava.com/api/v3"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data")
ACTIVITIES_CACHE     = os.path.join(CACHE_DIR, "activities.json")
ACTIVITIES_ALL_CACHE = os.path.join(os.path.dirname(__file__), ".cache", "activities_all.json")
RATE_LIMIT_DELAY = 0.5


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def refresh_access_token():
    """Exchange refresh token for a fresh access token. Updates .env in place."""
    client_id     = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip()

    if not all([client_id, client_secret, refresh_token]):
        return

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

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            content = f.read()
        content = re.sub(r"STRAVA_ACCESS_TOKEN=.*",  f"STRAVA_ACCESS_TOKEN={new_access}",  content)
        content = re.sub(r"STRAVA_REFRESH_TOKEN=.*", f"STRAVA_REFRESH_TOKEN={new_refresh}", content)
        with open(env_path, "w") as f:
            f.write(content)

    print("Strava access token refreshed.")


def _headers():
    token = os.environ.get("STRAVA_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("STRAVA_ACCESS_TOKEN not set")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Activity fetching
# ---------------------------------------------------------------------------

def fetch_activities(weeks=8, force_refresh=False):
    """
    Fetch rides from the past N weeks. Cached to data/activities.json.
    Pass force_refresh=True to bypass cache and re-fetch from Strava.
    Returns list of activity dicts.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not force_refresh and os.path.exists(ACTIVITIES_CACHE):
        with open(ACTIVITIES_CACHE) as f:
            activities = json.load(f)
        print(f"Loaded {len(activities)} activities from cache.")
        return activities

    after_ts = int((datetime.now() - timedelta(weeks=weeks)).timestamp())
    activities = []
    page = 1

    print(f"Fetching activities from past {weeks} weeks from Strava...")
    while True:
        resp = requests.get(
            f"{STRAVA_BASE}/athlete/activities",
            headers=_headers(),
            params={"per_page": 200, "page": page, "after": after_ts},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 200:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    with open(ACTIVITIES_CACHE, "w") as f:
        json.dump(activities, f)
    print(f"Fetched and cached {len(activities)} activities.")
    return activities


def fetch_all_activities(force_refresh=False):
    """
    Fetch ALL rides ever from Strava (no time filter). Cached to
    .cache/activities_all.json — safe to call repeatedly, won't re-fetch
    unless force_refresh=True.

    Used by fill_pmc_history.py for the one-time PMC backfill, and by
    main.py to seed the 8-week PMC computation with a historically accurate
    starting CTL.
    """
    os.makedirs(os.path.dirname(ACTIVITIES_ALL_CACHE), exist_ok=True)

    if not force_refresh and os.path.exists(ACTIVITIES_ALL_CACHE):
        with open(ACTIVITIES_ALL_CACHE) as f:
            activities = json.load(f)
        print(f"Loaded {len(activities)} all-time activities from cache.")
        return activities

    activities = []
    page = 1
    print("Fetching all-time activities from Strava (this may take a moment)...")
    while True:
        resp = requests.get(
            f"{STRAVA_BASE}/athlete/activities",
            headers=_headers(),
            params={"per_page": 200, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        print(f"  ...page {page}: {len(batch)} activities (total {len(activities)})")
        if len(batch) < 200:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    with open(ACTIVITIES_ALL_CACHE, "w") as f:
        json.dump(activities, f)
    print(f"Fetched and cached {len(activities)} all-time activities.")
    return activities


STREAM_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def fetch_power_stream(activity_id):
    """Fetch per-second watts stream. Cached to .cache/stream_{id}.json."""
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, f"stream_{activity_id}.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            data = json.load(f)
        return data if data else None  # [] cached means no stream

    resp = requests.get(
        f"{STRAVA_BASE}/activities/{activity_id}/streams",
        headers=_headers(),
        params={"keys": "watts", "key_by_type": "true"},
    )
    if resp.status_code == 404:
        with open(cache_path, "w") as f:
            json.dump([], f)
        return None
    resp.raise_for_status()
    data = resp.json()
    watts_stream = data.get("watts")
    result = watts_stream.get("data") if watts_stream else None
    with open(cache_path, "w") as f:
        json.dump(result if result is not None else [], f)
    time.sleep(RATE_LIMIT_DELAY)
    return result


def fetch_hr_stream(activity_id):
    """Fetch per-second heartrate stream. Cached to .cache/hr_{id}.json."""
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, f"hr_{activity_id}.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            data = json.load(f)
        return data if data else None  # [] cached means no HR monitor

    resp = requests.get(
        f"{STRAVA_BASE}/activities/{activity_id}/streams",
        headers=_headers(),
        params={"keys": "heartrate", "key_by_type": "true"},
    )
    if resp.status_code == 404:
        with open(cache_path, "w") as f:
            json.dump([], f)
        return None
    if resp.status_code == 429:
        print(f"  Rate limited on HR stream {activity_id}, skipping.")
        return None
    resp.raise_for_status()
    data = resp.json()
    hr_stream = data.get("heartrate")
    result = hr_stream.get("data") if hr_stream else None
    with open(cache_path, "w") as f:
        json.dump(result if result is not None else [], f)
    time.sleep(RATE_LIMIT_DELAY)
    return result


def fetch_activity_description(activity_id):
    """Fetch activity description from detail endpoint. Cached to .cache/detail_{id}.json."""
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, f"detail_{activity_id}.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f).get("description") or None

    resp = requests.get(f"{STRAVA_BASE}/activities/{activity_id}", headers=_headers())
    if resp.status_code == 404:
        with open(cache_path, "w") as f:
            json.dump({"description": None, "segment_efforts": [], "laps": []}, f)
        return None
    resp.raise_for_status()
    data = resp.json()
    description = data.get("description") or None
    segment_efforts = data.get("segment_efforts", [])
    laps = data.get("laps", [])
    with open(cache_path, "w") as f:
        json.dump({"description": description, "segment_efforts": segment_efforts, "laps": laps}, f)
    time.sleep(RATE_LIMIT_DELAY)
    return description


def fetch_activity_segments(activity_id):
    """
    Return segment_efforts[] for an activity. Reads from same detail cache as
    fetch_activity_description. Old cached files (without segment_efforts) return [].
    If not cached, triggers a full detail fetch (writing description + segments).
    """
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, f"detail_{activity_id}.json")

    if not os.path.exists(cache_path):
        fetch_activity_description(activity_id)  # fetches + writes cache

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f).get("segment_efforts", [])
    return []


def fetch_activity_laps(activity_id):
    """
    Return laps[] for an activity. Reads from same detail cache as fetch_activity_description.
    Old cached files (without laps key) return [] gracefully.
    If not cached, triggers a full detail fetch (writing description + segments + laps).
    Laps are present for outdoor rides with a GPS device; indoor rides typically return [].
    """
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, f"detail_{activity_id}.json")

    if not os.path.exists(cache_path):
        fetch_activity_description(activity_id)  # fetches + writes cache

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f).get("laps", [])
    return []


def fetch_starred_segments():
    """
    Fetch athlete's starred segments. Cached 7 days to .cache/segments_starred.json.
    Returns list of dicts with id, name, distance, avg_grade, climb_category.
    """
    from datetime import datetime
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, "segments_starred.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        fetched_str = cached.get("fetched")
        if fetched_str:
            age_days = (datetime.now().date() - datetime.fromisoformat(fetched_str).date()).days
            if age_days < 7:
                return cached.get("segments", [])

    resp = requests.get(
        f"{STRAVA_BASE}/segments/starred",
        headers=_headers(),
        params={"per_page": 200},
    )
    resp.raise_for_status()

    segments = []
    for s in resp.json():
        segments.append({
            "id":                   s.get("id"),
            "name":                 s.get("name"),
            "distance":             s.get("distance", 0),
            "avg_grade":            s.get("average_grade", 0),
            "climb_category":       s.get("climb_category", 0),
            "total_elevation_gain": s.get("total_elevation_gain", 0),
            "start_latlng":         s.get("start_latlng"),
        })

    with open(cache_path, "w") as f:
        json.dump({"fetched": datetime.now().date().isoformat(), "segments": segments}, f)
    time.sleep(RATE_LIMIT_DELAY)
    return segments


def _parse_time_str(t):
    """Parse Strava time string '5:17', '1:03:45', or '26s' into seconds."""
    if not t:
        return None
    try:
        if isinstance(t, str) and t.endswith("s") and ":" not in t:
            return int(t[:-1])
        parts = t.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, AttributeError):
        pass
    return None


def fetch_segment_leaderboard(segment_id):
    """
    Fetch segment stats via GET /segments/{id} (detail endpoint).
    The public leaderboard API requires partner access; this gives us
    KOM time, David's PR, and athlete count from the detail endpoint.

    Cached 7 days to .cache/seg_stats_{id}.json.
    Returns {
        "pr_elapsed_time": int (seconds),
        "kom_time_s":      int (seconds),
        "total_athletes":  int,
        "effort_count":    int,
        "athlete_effort_count": int,
    }
    """
    from datetime import datetime
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, f"seg_stats_{segment_id}.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        fetched_str = cached.get("fetched")
        if fetched_str:
            age_days = (datetime.now().date() - datetime.fromisoformat(fetched_str).date()).days
            if age_days < 7:
                return cached.get("data", {})

    resp = requests.get(
        f"{STRAVA_BASE}/segments/{segment_id}",
        headers=_headers(),
    )
    if resp.status_code in (404, 403):
        return {}
    if resp.status_code == 429:
        print(f"  Rate limited on segment {segment_id}, skipping.")
        return {}
    resp.raise_for_status()
    seg = resp.json()

    stats = seg.get("athlete_segment_stats") or {}
    xoms  = seg.get("xoms") or {}

    result = {
        "pr_elapsed_time":      stats.get("pr_elapsed_time"),
        "kom_time_s":           _parse_time_str(xoms.get("kom")),
        "total_athletes":       seg.get("athlete_count", 0),
        "effort_count":         seg.get("effort_count", 0),
        "athlete_effort_count": stats.get("effort_count", 0),
        "david_rank":           None,
    }

    # Fetch David's all-time best rank on this segment via all_efforts
    time.sleep(RATE_LIMIT_DELAY)
    try:
        eff_resp = requests.get(
            f"{STRAVA_BASE}/segments/{segment_id}/all_efforts",
            headers=_headers(),
            params={"per_page": 100},
        )
        if eff_resp.status_code == 200:
            kom_ranks = [e["kom_rank"] for e in eff_resp.json() if e.get("kom_rank")]
            result["david_rank"] = min(kom_ranks) if kom_ranks else None
        time.sleep(RATE_LIMIT_DELAY)
    except Exception:
        pass

    with open(cache_path, "w") as f:
        json.dump({"fetched": datetime.now().date().isoformat(), "data": result}, f)
    return result


def fetch_nearby_segments(lat, lng, radius_km=15):
    """
    Tile home area into overlapping 3km bounding boxes and call Strava Explore API.
    Hard filter: avg_grade >= 5%, not downhill. Cap: 25 API calls. Cache: 30 days per tile.
    Returns list of filtered segment dicts.
    """
    import math
    from datetime import datetime
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)

    deg_lat_per_km = 1 / 111.0
    deg_lng_per_km = 1 / (111.0 * math.cos(math.radians(lat)))
    half_tile_lat = 1.5 * deg_lat_per_km
    half_tile_lng = 1.5 * deg_lng_per_km

    segments_seen = {}
    api_calls = 0

    for i in range(-2, 3):
        for j in range(-2, 3):
            center_lat = lat + i * 3 * deg_lat_per_km
            center_lng = lng + j * 3 * deg_lng_per_km

            tile_key = f"{round(lat, 3)}_{round(lng, 3)}_{i}_{j}"
            cache_path = os.path.join(STREAM_CACHE_DIR, f"explore_{tile_key}.json")

            if os.path.exists(cache_path):
                with open(cache_path) as f:
                    cached = json.load(f)
                fetched_str = cached.get("fetched")
                if fetched_str:
                    age_days = (datetime.now().date() -
                                datetime.fromisoformat(fetched_str).date()).days
                    if age_days < 30:
                        for s in cached.get("segments", []):
                            segments_seen[s["id"]] = s
                        continue

            if api_calls >= 25:
                break

            sw_lat = center_lat - half_tile_lat
            sw_lng = center_lng - half_tile_lng
            ne_lat = center_lat + half_tile_lat
            ne_lng = center_lng + half_tile_lng
            bounds = f"{sw_lat},{sw_lng},{ne_lat},{ne_lng}"

            try:
                resp = requests.get(
                    f"{STRAVA_BASE}/segments/explore",
                    headers=_headers(),
                    params={"bounds": bounds, "activity_type": "riding"},
                )
                if resp.status_code == 429:
                    print("Rate limit on explore API, stopping.")
                    break
                resp.raise_for_status()
                raw_segs = resp.json().get("segments", [])
            except Exception as e:
                print(f"Explore tile {i},{j} failed: {e}")
                raw_segs = []

            api_calls += 1

            tile_segs = []
            for s in raw_segs:
                avg_grade = s.get("avg_grade", 0)
                if avg_grade < 5 or avg_grade <= 0:
                    continue
                seg = {
                    "id":             s.get("id"),
                    "name":           s.get("name"),
                    "avg_grade":      avg_grade,
                    "distance":       s.get("distance", 0),
                    "climb_category": s.get("climb_category", 0),
                    "elev_difference": s.get("elev_difference", 0),
                    "start_latlng":   s.get("start_latlng"),
                }
                tile_segs.append(seg)
                segments_seen[s.get("id")] = seg

            with open(cache_path, "w") as f:
                json.dump({"fetched": datetime.now().date().isoformat(), "segments": tile_segs}, f)
            time.sleep(RATE_LIMIT_DELAY)

    return list(segments_seen.values())


def fetch_segment_top10(segment_id):
    """
    Fetch top 10 leaderboard for a segment via GET /segments/{id}/leaderboard?per_page=10.
    Note: the public leaderboard endpoint may require partner access; returns {} on 403.

    Cached 7 days to .cache/seg_top10_{segment_id}.json.
    Returns {
        "top10_times_s": [int, ...],   # fastest → slowest
        "kom_time_s":    int,
        "tenth_time_s":  int,
        "spread_pct":    float,        # (t10 - t1) / t1 * 100
    }
    """
    from datetime import datetime
    os.makedirs(STREAM_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(STREAM_CACHE_DIR, f"seg_top10_{segment_id}.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        fetched_str = cached.get("fetched")
        if fetched_str:
            age_days = (datetime.now().date() - datetime.fromisoformat(fetched_str).date()).days
            if age_days < 7:
                return cached.get("data", {})

    try:
        resp = requests.get(
            f"{STRAVA_BASE}/segments/{segment_id}/leaderboard",
            headers=_headers(),
            params={"per_page": 10},
        )
        if resp.status_code in (403, 404, 401):
            # Leaderboard requires partner/subscription access — cache empty result
            with open(cache_path, "w") as f:
                json.dump({"fetched": datetime.now().date().isoformat(), "data": {}}, f)
            return {}
        resp.raise_for_status()
        lb = resp.json()
    except Exception:
        return {}

    entries = lb.get("entries", [])
    times = sorted(e.get("elapsed_time", 0) for e in entries if e.get("elapsed_time"))

    result = {}
    if times:
        result["top10_times_s"] = times
        result["kom_time_s"]    = times[0]
        result["tenth_time_s"]  = times[-1]
        if times[0] > 0:
            result["spread_pct"] = round((times[-1] - times[0]) / times[0] * 100, 2)

    with open(cache_path, "w") as f:
        json.dump({"fetched": datetime.now().date().isoformat(), "data": result}, f)
    time.sleep(RATE_LIMIT_DELAY)
    return result


def filter_rides(activities):
    """
    Split activities into indoor (Peloton/trainer) and outdoor rides.
    Returns (indoor_rides, outdoor_rides) — both filtered to Ride type only.
    """
    rides = [a for a in activities if a.get("type") == "Ride"]
    indoor  = [r for r in rides if r.get("trainer", False)]
    outdoor = [r for r in rides if not r.get("trainer", False)]
    return indoor, outdoor
