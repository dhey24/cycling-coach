#!/usr/bin/env python3
"""
test_leaderboard_scrape.py — Diagnose Strava leaderboard access strategies.

Tests three approaches to get top-10 leaderboard times for a segment,
printing full diagnostic output so we know which strategy to wire up.

Usage:
    python test_leaderboard_scrape.py
    python test_leaderboard_scrape.py --segment=12345678
"""

import os
import re
import json
import argparse
import requests

from dotenv import load_dotenv
load_dotenv()

# Test against a known segment with a small leaderboard for easy verification
DEFAULT_SEGMENT_ID = 40489131  # Full Valley Climb From Sanchez (David's KOM, 23 athletes)
DEFAULT_SEGMENT_NAME = "Full Valley Climb From Sanchez"


def _parse_leaderboard_html_fragment(html, debug=False):
    """
    Parse Strava's leaderboard HTML partial response (returned by Strategy A).
    Strava returns an HTML fragment (not JSON) for the leaderboard XHR endpoint.

    Tries multiple patterns to extract elapsed_time values in seconds.
    Returns sorted list of ints, or [].
    """
    # Decode HTML entities first (&quot; → ", &amp; → &, etc.)
    html = (html
            .replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&#39;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">"))

    # Pattern 1: data-tracking JSON on each leader <li> containing elapsed_time
    # e.g. data-tracking='{"rank":1,"elapsed_time":138,...}'
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
        if debug:
            print(f"  [Pattern 1] Found {len(times)} times via data-tracking JSON")
        return sorted(set(times))

    # Pattern 2: elapsed_time in any JSON-like blob in the HTML
    matches = re.findall(r'"elapsed_time"\s*:\s*(\d+)', html)
    if matches:
        times = sorted(set(int(m) for m in matches if 30 <= int(m) <= 7200))
        if times:
            if debug:
                print(f"  [Pattern 2] Found {len(times)} times via elapsed_time key")
            return times

    # Pattern 3: <span class="time">M:SS</span> or similar time display
    matches = re.findall(r'<[^>]*class=["\'][^"\']*time[^"\']*["\'][^>]*>(\d+):(\d{2})</[^>]+>', html)
    if matches:
        times = sorted(set(int(m)*60 + int(s) for m, s in matches if 30 <= int(m)*60+int(s) <= 7200))
        if times:
            if debug:
                print(f"  [Pattern 3] Found {len(times)} times via time element")
            return times

    # Pattern 4: raw M:SS patterns near "leader" context
    # Only if we can see leaderboard structure
    if "leader" in html.lower():
        matches = re.findall(r'\b(\d{1,2}):(\d{2})\b', html)
        if matches:
            times = sorted(set(int(m)*60 + int(s) for m, s in matches if 30 <= int(m)*60+int(s) <= 7200))
            if times:
                if debug:
                    print(f"  [Pattern 4] Found {len(times)} M:SS values near leaderboard (noisy)")
                return times

    if debug:
        # Show first 1000 chars of decoded HTML for manual inspection
        print(f"  Decoded HTML snippet:\n  {html[:800]}")

    return []


def _cookie():
    return os.environ.get("STRAVA_WEB_COOKIE", "").strip()


def _access_token():
    return os.environ.get("STRAVA_ACCESS_TOKEN", "").strip()


def _print_result(ok, detail=""):
    mark = "✓ WORKS" if ok else "✗ FAILS"
    print(f"  {mark}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Strategy A: XHR JSON to /segments/{id}/leaderboard with session cookie
# ---------------------------------------------------------------------------

def test_strategy_a(seg_id):
    print("\n[Strategy A] XHR JSON to /segments/{id}/leaderboard with session cookie")
    cookie = _cookie()
    if not cookie:
        print("  ✗ SKIP — STRAVA_WEB_COOKIE not set in .env")
        return None

    url = f"https://www.strava.com/segments/{seg_id}/leaderboard"
    try:
        resp = requests.get(
            url,
            params={"per_page": 10},
            headers={
                "Accept": "application/json, text/javascript, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://www.strava.com/segments/{seg_id}",
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
            },
            cookies={"_strava4_session": cookie},
            timeout=10,
            allow_redirects=False,
        )
    except Exception as e:
        print(f"  Request error: {e}")
        return None

    print(f"  Status: {resp.status_code}")

    if resp.status_code == 302:
        location = resp.headers.get("Location", "")
        print(f"  Redirect → {location}")
        print("  (Cookie likely expired or invalid — redirected to login)")
        _print_result(False, "redirect to login")
        return None

    if resp.status_code != 200:
        print(f"  Body preview: {resp.text[:200]}")
        _print_result(False, f"HTTP {resp.status_code}")
        return None

    content_type = resp.headers.get("Content-Type", "")
    print(f"  Content-Type: {content_type}")
    html = resp.text

    # Check for login redirect rendered as 200
    if "log in" in html[:500].lower() or "sign in" in html[:500].lower():
        print("  Got login page (cookie not accepted)")
        _print_result(False, "cookie rejected — login page returned")
        return None

    # Strategy A returns a Strava Turbo Frame / Rails UJS HTML partial (not JSON),
    # even though Content-Type is text/javascript. Parse it as HTML.
    print(f"  Response size: {len(html):,} bytes")
    print(f"  Body preview: {html[:200]}")

    # Try JSON first in case Strava changes their response format
    try:
        data = resp.json()
        entries = data.get("entries", [])
        times = sorted(e.get("elapsed_time", 0) for e in entries if e.get("elapsed_time"))
        if times:
            print(f"  Times (s): {times}")
            _print_result(True, f"{len(times)} times from JSON")
            return times
    except Exception:
        pass  # Expected — response is HTML, fall through to HTML parsing

    # Parse the HTML fragment for leaderboard times
    times = _parse_leaderboard_html_fragment(html, debug=True)
    if times:
        print(f"  Times (s): {times}")
        _print_result(True, f"{len(times)} times parsed from HTML fragment")
        return times

    _print_result(False, "could not extract times from HTML fragment")
    return None


# ---------------------------------------------------------------------------
# Strategy B: OAuth Bearer token to API leaderboard (expected 403)
# ---------------------------------------------------------------------------

def test_strategy_b(seg_id):
    print("\n[Strategy B] OAuth Bearer token to /api/v3/segments/{id}/leaderboard")
    token = _access_token()
    if not token:
        print("  ✗ SKIP — STRAVA_ACCESS_TOKEN not set in .env")
        return None

    url = f"https://www.strava.com/api/v3/segments/{seg_id}/leaderboard"
    try:
        resp = requests.get(
            url,
            params={"per_page": 10},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except Exception as e:
        print(f"  Request error: {e}")
        return None

    print(f"  Status: {resp.status_code}")
    if resp.status_code == 403:
        try:
            err = resp.json()
            print(f"  Error: {err.get('message', '')} — {err.get('errors', '')}")
        except Exception:
            print(f"  Body: {resp.text[:200]}")
        _print_result(False, "partner access required (expected)")
        return None

    if resp.status_code == 200:
        data = resp.json()
        entries = data.get("entries", [])
        times = sorted(e.get("elapsed_time", 0) for e in entries if e.get("elapsed_time"))
        print(f"  Entries: {len(entries)}, Times: {times}")
        _print_result(True, "OAuth API works (unexpected — partner access not required for this token)")
        return times

    print(f"  Body: {resp.text[:200]}")
    _print_result(False, f"HTTP {resp.status_code}")
    return None


# ---------------------------------------------------------------------------
# Strategy C: Scrape HTML segment page, parse embedded JSON/table
# ---------------------------------------------------------------------------


def test_strategy_c(seg_id):
    print("\n[Strategy C] HTML page scrape with session cookie + regex parse")
    cookie = _cookie()
    if not cookie:
        print("  ✗ SKIP — STRAVA_WEB_COOKIE not set in .env")
        return None

    url = f"https://www.strava.com/segments/{seg_id}"
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
            },
            cookies={"_strava4_session": cookie},
            timeout=15,
            allow_redirects=True,
        )
    except Exception as e:
        print(f"  Request error: {e}")
        return None

    print(f"  Status: {resp.status_code}")
    print(f"  Final URL: {resp.url}")

    if "login" in resp.url or "log in" in resp.text.lower()[:500]:
        print("  Redirected to login — cookie not accepted")
        _print_result(False, "cookie rejected")
        return None

    html = resp.text
    print(f"  Page size: {len(html):,} bytes")

    # Check for leaderboard section
    if "leaderboard" not in html.lower():
        print("  No 'leaderboard' found in page HTML")
        _print_result(False, "leaderboard section absent — possibly rate-limited or wrong page")
        return None

    times = _parse_leaderboard_html_fragment(html, debug=True)
    if times:
        print(f"  Parsed times (s): {times[:10]}")
        _print_result(True, f"{len(times)} times parsed from full page HTML")
        return times
    else:
        _print_result(False, "no times parsed from full page HTML")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Test Strava leaderboard scraping strategies")
    parser.add_argument("--segment", type=int, default=DEFAULT_SEGMENT_ID,
                        help=f"Segment ID to test (default: {DEFAULT_SEGMENT_ID})")
    args = parser.parse_args()

    seg_id = args.segment
    name   = DEFAULT_SEGMENT_NAME if seg_id == DEFAULT_SEGMENT_ID else f"segment {seg_id}"

    print(f"Testing segment {seg_id} ({name})")
    print("=" * 60)

    results = {}
    results["A"] = test_strategy_a(seg_id)
    results["B"] = test_strategy_b(seg_id)
    results["C"] = test_strategy_c(seg_id)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    working = [k for k, v in results.items() if v is not None]
    failing = [k for k, v in results.items() if v is None]

    if working:
        best = working[0]
        print(f"  Primary strategy: {best} — times: {results[best]}")
        if len(working) > 1:
            print(f"  Available fallbacks: {', '.join(working[1:])}")
        print()
        print("  Wire up in _fetch_top10_web():")
        if "A" in working:
            print("  → Strategy A already implemented. No changes needed.")
        elif "C" in working:
            print("  → Add Strategy C HTML fallback to _fetch_top10_web().")
    else:
        print("  All strategies failed.")
        print()
        print("  Possible causes:")
        print("  1. STRAVA_WEB_COOKIE expired — get a fresh one from browser DevTools")
        print("  2. Strava is rate-limiting — wait a few minutes and retry")
        print("  3. Segment is private or requires login for a different reason")

    if failing:
        print(f"\n  Failed strategies: {', '.join(failing)}")


if __name__ == "__main__":
    main()
