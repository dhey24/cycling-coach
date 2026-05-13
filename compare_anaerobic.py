"""
compare_anaerobic.py — Side-by-side comparison of two anaerobic interval sessions.

Compares this week's anaerobic session vs last week's to assess:
  - Power delivery vs target (1-min and 3-min sets separately)
  - HR response: ramp rate, peak HR, plateau %, recovery kinetics
  - Coaching analysis: fade, overperformance, adaptation signals

Usage:
    python compare_anaerobic.py           # current week (0) vs last week (1)
    python compare_anaerobic.py --w1 2 --w2 1  # override week offsets
"""

import argparse
import json
import os
import sys
import numpy as np
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

import strava_client
import metrics

# ── Duration threshold for 1-min vs 3-min set classification ─────────────────
SHORT_MAX_S = 120  # ≤120s → "1-min" set; >120s → "3-min" set
MIN_PLAUSIBLE_HR = 140  # Below this during a hard interval (>400w) = monitor dropout
HR_SPIKE_THRESHOLD = 210  # Above this = sensor noise spike, exclude from session max


def parse_args():
    p = argparse.ArgumentParser(description="Compare two anaerobic interval sessions")
    p.add_argument("--w1", type=int, default=1, help="Week offset for session A (default: 1 = last week)")
    p.add_argument("--w2", type=int, default=0, help="Week offset for session B (default: 0 = this week)")
    return p.parse_args()


def load_plan():
    path = os.path.join(os.path.dirname(__file__), "data", "plan.json")
    with open(path) as f:
        return json.load(f)


def find_anaerobic_session(plan, activities, ftp_outdoor, ftp_indoor, week_offset):
    """Return the day entry for the anaerobic planned session in the given week."""
    week_sessions = metrics.match_sessions_to_rides(
        plan, activities, ftp_outdoor, ftp_indoor, week_offset=week_offset
    )
    for entry in week_sessions:
        planned = entry.get("planned") or {}
        if planned.get("type") == "anaerobic":
            return entry
    return None


def infer_set_targets(planned):
    """
    Extract 1-min and 3-min power targets from the session plan.
    plan.json stores these in the description (not interval_sets), so we parse
    the description for the pattern '10x1min @ 430w' and '2x3min @ 375w'.
    Falls back to target_watts for both sets if parsing fails.
    """
    desc = planned.get("description", "")
    import re
    short_target = planned.get("target_watts", 0)
    long_target  = planned.get("target_watts", 0)

    # Match patterns like "10x1min @ 430w" and "2x3min @ 375w"
    for m in re.finditer(r'(\d+)x(\d+)min\s*@\s*(\d+)w', desc):
        _count, mins, watts = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mins == 1:
            short_target = watts
        elif mins == 3:
            long_target = watts

    return short_target, long_target


def analyze_session(session_entry, ftp_outdoor, ftp_indoor):
    """
    Detect intervals + compute HR metrics for a matched session entry.
    Returns dict with session metadata and per-interval data.
    """
    planned = session_entry.get("planned") or {}
    actual  = session_entry.get("actual")

    if actual is None:
        return None

    activity_id = actual.get("id")
    is_indoor   = actual.get("trainer", False)
    scale       = ftp_outdoor / ftp_indoor if is_indoor else 1.0
    name        = actual.get("name", "?")
    duration    = actual.get("duration_min", 0)
    act_date    = session_entry.get("date", "?")

    short_target, long_target = infer_set_targets(planned)
    threshold = ftp_outdoor * 0.88

    # Fetch streams
    watts_raw = strava_client.fetch_power_stream(activity_id)
    hr_raw    = strava_client.fetch_hr_stream(activity_id)

    if not watts_raw:
        print(f"  WARNING: no power stream for activity {activity_id}", file=sys.stderr)
        return None

    watts_arr = np.array(watts_raw, dtype=float)
    if is_indoor:
        watts_arr = watts_arr * scale

    # Detect intervals
    efforts = metrics.detect_intervals(watts_arr, threshold_watts=threshold)

    # Assign targets + HR metrics per interval
    stream_len = len(watts_arr)
    hr_arr = np.array(hr_raw, dtype=float) if hr_raw else None
    if hr_arr is not None:
        hr_arr = hr_arr[:stream_len]

    for e in efforts:
        dur = e["duration_s"]
        e["target_watts"] = short_target if dur <= SHORT_MAX_S else long_target
        e["set"] = "short" if dur <= SHORT_MAX_S else "long"

        if hr_arr is not None:
            s = e["start_s"]
            end = s + dur
            if end <= len(hr_arr) and dur >= 10:
                seg_hr = hr_arr[s:end]
                peak_hr = int(np.max(seg_hr))
                # Sanity check: discard HR data for hard intervals where monitor clearly dropped out
                if peak_hr < MIN_PLAUSIBLE_HR and e["avg_watts"] > 400:
                    e["peak_hr"] = e["hr_at_60s"] = e["ramp_rate"] = e["recovery_60s"] = None
                else:
                    e["peak_hr"]   = peak_hr
                    e["hr_at_60s"] = int(seg_hr[min(60, len(seg_hr) - 1)])
                    e["ramp_rate"] = round(float(seg_hr[min(60, len(seg_hr) - 1)] - seg_hr[0]), 1)
                    if end + 60 <= len(hr_arr):
                        e["recovery_60s"] = int(e["peak_hr"] - hr_arr[end + 60])
                    else:
                        e["recovery_60s"] = None
            else:
                e["peak_hr"] = e["hr_at_60s"] = e["ramp_rate"] = e["recovery_60s"] = None
        else:
            e["peak_hr"] = e["hr_at_60s"] = e["ramp_rate"] = e["recovery_60s"] = None

    # Use configured MAX_HR if available; otherwise derive from session (clipped at spike threshold)
    configured_max_hr = metrics.load_max_hr()
    if configured_max_hr:
        session_max_hr = configured_max_hr
    elif hr_arr is not None and len(hr_arr) > 0:
        clipped = hr_arr[hr_arr <= HR_SPIKE_THRESHOLD]
        session_max_hr = int(np.max(clipped)) if len(clipped) > 0 else None
    else:
        session_max_hr = None
    for e in efforts:
        if session_max_hr and e.get("peak_hr"):
            e["hr_plateau_pct"] = round(e["peak_hr"] / session_max_hr * 100, 1)
        else:
            e["hr_plateau_pct"] = None

    return {
        "date":         act_date,
        "name":         name,
        "duration_min": duration,
        "is_indoor":    is_indoor,
        "short_target": short_target,
        "long_target":  long_target,
        "efforts":      efforts,
        "session_max_hr": session_max_hr,
    }


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_dur(s):
    return f"{s // 60}:{s % 60:02d}"


def fmt_pct(avg, target):
    if not target:
        return "  ?"
    p = round((avg - target) / target * 100)
    sign = "+" if p >= 0 else ""
    return f"{sign}{p}%"


def sym(avg, target):
    if not target:
        return "?"
    if avg >= target:
        return "✓"
    elif avg >= target * 0.95:
        return "~"
    return "✗"


def _v(val, fmt="{}", fallback="  —"):
    return fmt.format(val) if val is not None else fallback


# ── Display ───────────────────────────────────────────────────────────────────

SEP = "─" * 100
DSEP = "═" * 100


def print_session_header(label, sess):
    env = "INDOOR" if sess["is_indoor"] else "OUTDOOR"
    print(f"  {label}  {sess['date']}  \"{sess['name']}\"  [{env}]  {sess['duration_min']}min")
    print(f"    1-min target: {sess['short_target']}w   3-min target: {sess['long_target']}w")


def print_interval_table(set_label, efforts_a, efforts_b, label_a, label_b):
    print(f"\n{SEP}")
    print(f"  {set_label}")
    print(f"{SEP}")

    hdr = (
        f"  {'#':>2}  {'Dur':>5}  {'AvgW':>5}  {'PeakW':>5}  {'%tgt':>5}  "
        f"{'PkHR':>5}  {'Ramp':>5}  {'Rec60':>5}  {'Sym':>3}"
    )
    print(f"  {'── ' + label_a + ' ':─<46}    {'── ' + label_b + ' ':─<46}")
    print(hdr + "    " + hdr[2:])  # reuse header, strip leading spaces on B

    max_rows = max(len(efforts_a), len(efforts_b))

    for i in range(max_rows):
        ea = efforts_a[i] if i < len(efforts_a) else None
        eb = efforts_b[i] if i < len(efforts_b) else None

        def row(e, idx):
            if e is None:
                return " " * 46
            pct  = fmt_pct(e["avg_watts"], e["target_watts"])
            s    = sym(e["avg_watts"], e["target_watts"])
            ramp = _v(e.get("ramp_rate"), "{:+.0f}", "  —")
            rec  = _v(e.get("recovery_60s"), "{:+d}", "  —")
            phr  = _v(e.get("peak_hr"), "{:d}", "  —")
            return (
                f"  {idx+1:>2}  {fmt_dur(e['duration_s']):>5}  {e['avg_watts']:>5}w"
                f"  {e['peak_watts']:>5}w  {pct:>5}  {phr:>5}  {ramp:>5}  {rec:>5}  {s:>3}"
            )

        print(row(ea, i) + "    " + row(eb, i).strip())

    # Means row
    def mean_row(efforts):
        if not efforts:
            return " " * 46
        avg_w  = round(sum(e["avg_watts"] for e in efforts) / len(efforts))
        tgt    = efforts[0]["target_watts"]
        pct    = fmt_pct(avg_w, tgt)
        phr_vals  = [e["peak_hr"]     for e in efforts if e.get("peak_hr")]
        ramp_vals = [e["ramp_rate"]   for e in efforts if e.get("ramp_rate") is not None]
        rec_vals  = [e["recovery_60s"] for e in efforts if e.get("recovery_60s") is not None]
        phr_m  = _v(round(sum(phr_vals)  / len(phr_vals))  if phr_vals  else None, "{:d}", "—")
        ramp_m = _v(round(sum(ramp_vals) / len(ramp_vals)) if ramp_vals else None, "{:+d}", "—")
        rec_m  = _v(round(sum(rec_vals)  / len(rec_vals))  if rec_vals  else None, "{:+d}", "—")
        s = sym(avg_w, tgt)
        return (
            f"  {'─':>2}  {'─':>5}  {avg_w:>5}w  {'─':>5}   {pct:>5}"
            f"  {phr_m:>5}  {ramp_m:>5}  {rec_m:>5}  {s:>3}"
        )

    print(SEP[:48])
    print(mean_row(efforts_a) + "    " + mean_row(efforts_b).strip())


def print_coaching_analysis(sess_a, sess_b):
    print(f"\n{DSEP}")
    print("  COACHING ANALYSIS")
    print(DSEP)

    short_a = [e for e in sess_a["efforts"] if e["set"] == "short"]
    short_b = [e for e in sess_b["efforts"] if e["set"] == "short"]
    long_a  = [e for e in sess_a["efforts"] if e["set"] == "long"]
    long_b  = [e for e in sess_b["efforts"] if e["set"] == "long"]

    tgt_a_s = sess_a["short_target"]
    tgt_b_s = sess_b["short_target"]

    # ── Power overperformance ──────────────────────────────────────────────────
    print("\n  POWER vs TARGET  (1-min set)")
    if short_a:
        avg_a = round(sum(e["avg_watts"] for e in short_a) / len(short_a))
        above_a = sum(1 for e in short_a if e["avg_watts"] >= tgt_a_s)
        pct_a = fmt_pct(avg_a, tgt_a_s)
        print(f"    Week 1: mean {avg_a}w vs {tgt_a_s}w target = {pct_a}  ({above_a}/{len(short_a)} reps at/above target)")
    if short_b:
        avg_b = round(sum(e["avg_watts"] for e in short_b) / len(short_b))
        above_b = sum(1 for e in short_b if e["avg_watts"] >= tgt_b_s)
        pct_b = fmt_pct(avg_b, tgt_b_s)
        print(f"    Week 2: mean {avg_b}w vs {tgt_b_s}w target = {pct_b}  ({above_b}/{len(short_b)} reps at/above target)")

    # ── Fade check (first half vs second half of common set) ──────────────────
    print("\n  FADE CHECK  (reps 1-4 vs 5-8, 1-min set)")
    for label, short in [("Week 1", short_a), ("Week 2", short_b)]:
        if len(short) >= 4:
            first = [e["avg_watts"] for e in short[:4]]
            mid   = [e["avg_watts"] for e in short[4:8]] if len(short) >= 8 else []
            f_avg = round(sum(first) / len(first))
            if mid:
                m_avg = round(sum(mid) / len(mid))
                delta = m_avg - f_avg
                sign  = "+" if delta >= 0 else ""
                trend = "no fade ↑" if delta >= 0 else "fade ↓"
                print(f"    {label}: reps 1-4 avg {f_avg}w,  reps 5-8 avg {m_avg}w  ({sign}{delta}w  {trend})")
            else:
                print(f"    {label}: reps 1-4 avg {f_avg}w  (fewer than 8 reps)")

    # ── Added-rep test (Week 2 reps 9-10 vs its own mean) ─────────────────────
    if len(short_b) >= 9:
        base_avg = round(sum(e["avg_watts"] for e in short_b[:8]) / 8)
        extra    = [e["avg_watts"] for e in short_b[8:]]
        extra_avg = round(sum(extra) / len(extra))
        delta = extra_avg - base_avg
        sign  = "+" if delta >= 0 else ""
        flag  = "W' not depleted → targets may be conservative" if delta >= -10 else "W' depleting as expected"
        print(f"\n  ADDED-REP TEST  (Week 2 reps 9-10 vs reps 1-8 mean)")
        print(f"    Reps 1-8 mean {base_avg}w   |   Reps 9-10 avg {extra_avg}w   ({sign}{delta}w)  → {flag}")

    # ── HR drift across the set ───────────────────────────────────────────────
    print("\n  HR DRIFT  (peak HR by rep, 1-min set)")
    for label, short in [("Week 1", short_a), ("Week 2", short_b)]:
        phr_vals = [e.get("peak_hr") for e in short if e.get("peak_hr")]
        if len(phr_vals) >= 4:
            first_hr = round(sum(phr_vals[:4]) / 4)
            last_hr  = round(sum(phr_vals[-4:]) / 4)
            drift    = last_hr - first_hr
            sign     = "+" if drift >= 0 else ""
            note     = "expected cardiac drift" if drift >= 3 else "minimal drift — demand may be sub-maximal"
            all_str  = "  ".join(str(h) for h in phr_vals)
            print(f"    {label}: [{all_str}]  first-4 avg {first_hr},  last-4 avg {last_hr}  ({sign}{drift}bpm)  → {note}")
        elif phr_vals:
            print(f"    {label}: {phr_vals}  (too few reps for drift analysis)")
        else:
            print(f"    {label}: no HR data")

    # ── HR plateau % ──────────────────────────────────────────────────────────
    print("\n  HR PLATEAU %  (peak_hr as % of session max HR)")
    for label, short, sess in [("Week 1", short_a, sess_a), ("Week 2", short_b, sess_b)]:
        max_hr = sess.get("session_max_hr")
        phr_vals = [e.get("peak_hr") for e in short if e.get("peak_hr")]
        if phr_vals and max_hr:
            mean_pk  = round(sum(phr_vals) / len(phr_vals))
            plateau  = round(mean_pk / max_hr * 100, 1)
            flag     = "  ← under-loaded (< 88%)" if plateau < 88 else ""
            print(f"    {label}: session max HR {max_hr}bpm,  mean interval peak {mean_pk}bpm  = {plateau}%{flag}")
        else:
            print(f"    {label}: no HR data")

    # ── Recovery kinetics ─────────────────────────────────────────────────────
    print("\n  RECOVERY KINETICS  (HR drop in 60s post-interval, 1-min set)")
    for label, short in [("Week 1", short_a), ("Week 2", short_b)]:
        rec_vals = [e["recovery_60s"] for e in short if e.get("recovery_60s") is not None]
        if rec_vals:
            mean_rec = round(sum(rec_vals) / len(rec_vals))
            print(f"    {label}: mean -{mean_rec}bpm in 60s post-effort  (higher = better aerobic clearance)")
        else:
            print(f"    {label}: no recovery HR data")

    # ── Power:HR efficiency ───────────────────────────────────────────────────
    print("\n  POWER:HR EFFICIENCY  (avg watts / avg peak HR, 1-min set)")
    for label, short in [("Week 1", short_a), ("Week 2", short_b)]:
        paired = [(e["avg_watts"], e["peak_hr"]) for e in short if e.get("peak_hr")]
        if paired:
            eff = round(sum(w / h for w, h in paired) / len(paired), 2)
            print(f"    {label}: {eff} w/bpm")
        else:
            print(f"    {label}: no HR data")

    # ── Overperformance verdict ───────────────────────────────────────────────
    print(f"\n  VERDICT")
    flags = []

    for label, short, tgt, sess in [("Week 1", short_a, tgt_a_s, sess_a), ("Week 2", short_b, tgt_b_s, sess_b)]:
        if not short:
            continue
        avg_w = round(sum(e["avg_watts"] for e in short) / len(short))
        phr_vals = [e["peak_hr"] for e in short if e.get("peak_hr")]
        max_hr = sess.get("session_max_hr")
        mean_plateau = round(sum(phr_vals) / len(phr_vals) / max_hr * 100, 1) if phr_vals and max_hr else None

        over_power = avg_w > tgt * 1.05
        under_hr   = mean_plateau is not None and mean_plateau < 88

        # Fade pattern: compare first half vs second half watts
        negative_fade = False
        if len(short) >= 6:
            half = len(short) // 2
            first_half_avg = sum(e["avg_watts"] for e in short[:half]) / half
            second_half_avg = sum(e["avg_watts"] for e in short[half:]) / (len(short) - half)
            negative_fade = second_half_avg > first_half_avg + 5  # getting stronger through the set

        reasons = []
        if over_power:
            reasons.append(f"power {fmt_pct(avg_w, tgt)} above target")
        if under_hr:
            reasons.append(f"HR plateau {mean_plateau}% < 88%")
        if negative_fade:
            reasons.append("no fade (getting stronger late — W' not taxed)")

        if len(reasons) >= 2:
            flags.append(f"  ⚠  {label}: {avg_w}w mean vs {tgt}w — {'; '.join(reasons)}  → 1-min target conservative, consider +10-15w")
        elif reasons:
            flags.append(f"  ~  {label}: {'; '.join(reasons)} — monitor one more session before revising targets")
        else:
            flags.append(f"  ✓  {label}: performance within expected range")

    for f in flags:
        print(f)

    print()


INTENSITY_TYPES = {"vo2max", "anaerobic", "sweet_spot", "tempo", "threshold"}


def collect_intensity_sessions(plan, activities, ftp_outdoor, ftp_indoor, week_offsets):
    """Return all matched intensity day entries across the given week offsets."""
    seen_ids = set()
    results = []
    for offset in week_offsets:
        week_sessions = metrics.match_sessions_to_rides(
            plan, activities, ftp_outdoor, ftp_indoor, week_offset=offset
        )
        for entry in week_sessions:
            planned = entry.get("planned") or {}
            actual  = entry.get("actual")
            if planned.get("type") in INTENSITY_TYPES and actual is not None:
                aid = actual.get("id")
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    results.append(entry)
    results.sort(key=lambda e: e.get("date") or "")
    return results


def analyze_generic_session(entry, ftp_outdoor, ftp_indoor, max_hr):
    """
    Detect intervals + HR metrics for any intensity session.
    Returns dict with efforts (all classified as one set) and summary stats.
    """
    planned = entry.get("planned") or {}
    actual  = entry.get("actual")
    if actual is None:
        return None

    activity_id  = actual.get("id")
    is_indoor    = actual.get("trainer", False)
    scale        = ftp_outdoor / ftp_indoor if is_indoor else 1.0
    session_type = planned.get("type", "?")
    target_w     = planned.get("target_watts", 0)

    watts_raw = strava_client.fetch_power_stream(activity_id)
    hr_raw    = strava_client.fetch_hr_stream(activity_id)
    if not watts_raw:
        return None

    watts_arr = np.array(watts_raw, dtype=float)
    if is_indoor:
        watts_arr = watts_arr * scale

    threshold = ftp_outdoor * 0.88
    efforts = metrics.detect_intervals(watts_arr, threshold_watts=threshold)

    stream_len = len(watts_arr)
    hr_arr = np.array(hr_raw, dtype=float)[:stream_len] if hr_raw else None

    for e in efforts:
        e["target_watts"] = target_w
        s, end = e["start_s"], e["start_s"] + e["duration_s"]
        if hr_arr is not None and end <= len(hr_arr) and e["duration_s"] >= 10:
            seg = hr_arr[s:end]
            pk = int(np.max(seg))
            if pk < MIN_PLAUSIBLE_HR and e["avg_watts"] > 350:
                e["peak_hr"] = e["hr_plateau_pct"] = e["recovery_60s"] = None
            else:
                e["peak_hr"] = pk
                e["hr_plateau_pct"] = round(pk / max_hr * 100, 1) if max_hr else None
                e["recovery_60s"] = (int(pk - hr_arr[end + 60])
                                     if end + 60 <= len(hr_arr) else None)
        else:
            e["peak_hr"] = e["hr_plateau_pct"] = e["recovery_60s"] = None

    if not efforts:
        return None

    # For anaerobic sessions: analyse short intervals only (avoids mixing 1-min and 3-min targets)
    if session_type == "anaerobic":
        short_target, _ = infer_set_targets(planned)
        scored_efforts = [e for e in efforts if e["duration_s"] <= SHORT_MAX_S]
        effective_target = short_target
        set_label = "1-min set"
    else:
        scored_efforts = efforts
        effective_target = target_w
        set_label = "all"

    if not scored_efforts:
        scored_efforts = efforts  # fallback if classification fails
        effective_target = target_w
        set_label = "all"

    for e in scored_efforts:
        e["target_watts"] = effective_target

    mean_w = round(sum(e["avg_watts"] for e in scored_efforts) / len(scored_efforts))
    pct    = round((mean_w - effective_target) / effective_target * 100) if effective_target else None

    # Fade: first half vs second half (only meaningful with ≥4 efforts)
    fade_delta = None
    if len(scored_efforts) >= 4:
        half = len(scored_efforts) // 2
        first = sum(e["avg_watts"] for e in scored_efforts[:half]) / half
        last  = sum(e["avg_watts"] for e in scored_efforts[half:]) / (len(scored_efforts) - half)
        fade_delta = round(last - first)

    phr_vals = [e["peak_hr"] for e in scored_efforts if e.get("peak_hr")]
    mean_plateau = round(sum(e["hr_plateau_pct"] for e in scored_efforts
                             if e.get("hr_plateau_pct")) / len(phr_vals), 1) if phr_vals else None

    return {
        "date":         entry.get("date", "?"),
        "type":         session_type,
        "name":         actual.get("name", "?"),
        "is_indoor":    is_indoor,
        "n_efforts":    len(scored_efforts),
        "set_label":    set_label,
        "mean_w":       mean_w,
        "target_w":     effective_target,
        "pct_delta":    pct,
        "fade_delta":   fade_delta,
        "mean_plateau": mean_plateau,
        "efforts":      efforts,
    }


def print_cross_session_evidence(sessions):
    print(f"\n{DSEP}")
    print("  CROSS-SESSION EVIDENCE  (last 2 weeks — intensity sessions)")
    print(DSEP)
    print(f"  {'Date':10}  {'Env':6}  {'Type':9}  {'Name':34}  {'Set':7}  {'Reps':4}  {'MeanW':6}  {'Tgt':5}  {'%Tgt':5}  {'Fade':5}  {'PkHR%':6}  Verdict")
    print(f"  {'-'*10}  {'-'*6}  {'-'*9}  {'-'*34}  {'-'*7}  {'-'*4}  {'-'*6}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*7}")

    for s in sessions:
        env     = "indoor" if s["is_indoor"] else "outdr"
        name    = s["name"][:34]
        pct     = f"{'+' if s['pct_delta'] >= 0 else ''}{s['pct_delta']}%" if s["pct_delta"] is not None else "  —"
        fade    = (f"{'+' if s['fade_delta'] >= 0 else ''}{s['fade_delta']}w"
                   if s["fade_delta"] is not None else "  —")
        plateau = f"{s['mean_plateau']}%" if s["mean_plateau"] is not None else "  —"

        # Verdict per session
        over   = s["pct_delta"] is not None and s["pct_delta"] > 5
        no_fde = s["fade_delta"] is not None and s["fade_delta"] > 5
        lo_hr  = s["mean_plateau"] is not None and s["mean_plateau"] < 88
        flags  = sum([over, no_fde, lo_hr])
        if flags >= 2:
            verdict = "⚠ over"
        elif flags == 1:
            verdict = "~ watch"
        else:
            verdict = "✓ ok"

        set_lbl = s.get("set_label", "all")[:7]
        print(f"  {s['date']:10}  {env:6}  {s['type']:9}  {name:34}  {set_lbl:7}  {s['n_efforts']:4}  "
              f"{s['mean_w']:5}w  {s['target_w']:4}w  {pct:5}  {fade:5}  {plateau:6}  {verdict}")

    # Summary: consistent over-target across sessions?
    all_pcts = [s["pct_delta"] for s in sessions if s["pct_delta"] is not None]
    all_fades = [s["fade_delta"] for s in sessions if s["fade_delta"] is not None]
    if all_pcts:
        mean_pct = round(sum(all_pcts) / len(all_pcts), 1)
        above_tgt = sum(1 for p in all_pcts if p > 0)
        neg_fades = sum(1 for f in all_fades if f > 5)
        print(f"\n  Summary: {above_tgt}/{len(all_pcts)} sessions above target  |  mean delta {'+' if mean_pct >= 0 else ''}{mean_pct}%  |  {neg_fades}/{len(all_fades)} sessions with negative fade")


def main():
    args = parse_args()

    print("\nRefreshing Strava token + fetching activities...")
    strava_client.refresh_access_token()
    activities = strava_client.fetch_activities(weeks=4, force_refresh=True)
    ftp_outdoor, ftp_indoor, *_ = metrics.load_ftps()
    plan = load_plan()

    print(f"FTPs: outdoor={ftp_outdoor}w  indoor={ftp_indoor}w")

    entry_a = find_anaerobic_session(plan, activities, ftp_outdoor, ftp_indoor, week_offset=args.w1)
    entry_b = find_anaerobic_session(plan, activities, ftp_outdoor, ftp_indoor, week_offset=args.w2)

    if entry_a is None:
        print(f"ERROR: no anaerobic session found for week_offset={args.w1}", file=sys.stderr)
        sys.exit(1)
    if entry_b is None:
        print(f"ERROR: no anaerobic session found for week_offset={args.w2}", file=sys.stderr)
        sys.exit(1)

    label_a = f"Week -{args.w1}"
    label_b = f"Week -{args.w2}" if args.w2 > 0 else "This week"

    if entry_a.get("actual") is None:
        print(f"ERROR: no ride matched to anaerobic session for {label_a} ({entry_a.get('date')})", file=sys.stderr)
        sys.exit(1)
    if entry_b.get("actual") is None:
        print(f"ERROR: no ride matched to anaerobic session for {label_b} ({entry_b.get('date')})", file=sys.stderr)
        sys.exit(1)

    print(f"\nAnalyzing streams...")
    sess_a = analyze_session(entry_a, ftp_outdoor, ftp_indoor)
    sess_b = analyze_session(entry_b, ftp_outdoor, ftp_indoor)

    if sess_a is None or sess_b is None:
        print("ERROR: could not analyze one or both sessions (missing power stream?)", file=sys.stderr)
        sys.exit(1)

    # ── Print report ──────────────────────────────────────────────────────────
    print(f"\n{DSEP}")
    print("  ANAEROBIC SESSION COMPARISON")
    print(DSEP)
    print_session_header(label_a, sess_a)
    print()
    print_session_header(label_b, sess_b)

    short_a = [e for e in sess_a["efforts"] if e["set"] == "short"]
    short_b = [e for e in sess_b["efforts"] if e["set"] == "short"]
    long_a  = [e for e in sess_a["efforts"] if e["set"] == "long"]
    long_b  = [e for e in sess_b["efforts"] if e["set"] == "long"]

    lbl_a_short = f"{label_a}  ({len(short_a)} reps, target {sess_a['short_target']}w)"
    lbl_b_short = f"{label_b}  ({len(short_b)} reps, target {sess_b['short_target']}w)"
    print_interval_table("1-MINUTE INTERVALS", short_a, short_b, lbl_a_short, lbl_b_short)

    lbl_a_long = f"{label_a}  ({len(long_a)} reps, target {sess_a['long_target']}w)"
    lbl_b_long = f"{label_b}  ({len(long_b)} reps, target {sess_b['long_target']}w)"
    print_interval_table("3-MINUTE INTERVALS", long_a, long_b, lbl_a_long, lbl_b_long)

    print_coaching_analysis(sess_a, sess_b)

    # ── Cross-session evidence (all intensity sessions, last 2 weeks) ─────────
    max_hr = metrics.load_max_hr() or 193
    all_intensity = collect_intensity_sessions(plan, activities, ftp_outdoor, ftp_indoor,
                                               week_offsets=[0, 1])
    analyzed = [analyze_generic_session(e, ftp_outdoor, ftp_indoor, max_hr)
                for e in all_intensity]
    analyzed = [s for s in analyzed if s is not None]
    if analyzed:
        print_cross_session_evidence(analyzed)


if __name__ == "__main__":
    main()
