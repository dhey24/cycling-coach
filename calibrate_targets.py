"""
calibrate_targets.py — Periodic outdoor power target calibration.

Analyses recent outdoor intensity sessions to check whether current plan targets
are well-calibrated against actual performance and derived power anchors.

Outputs:
  - Updates the "Outdoor Power Target Calibration" section in preferences.md
  - Returns a compact summary string suitable for passing to the coach prompt

Can be run standalone or called from main.py before weekly email generation.

Usage:
    python calibrate_targets.py          # analyse last 4 weeks, update prefs
    python calibrate_targets.py --weeks=6
"""

import argparse
import os
import re
import json
import sys
import numpy as np
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

import strava_client
import metrics

PREFS_PATH = os.path.join(os.path.dirname(__file__), "preferences.md")

# Calibration constants
SHORT_MAX_S     = 120   # ≤120s = "1-min" anaerobic effort
MIN_PLAUSIBLE_HR = 140  # HR below this during >350w effort = monitor dropout
MIN_SESSIONS    = 2     # need at least this many sessions of a type to update calibration

# Target ranges for flagging
OVERPERFORMANCE_PCT = 2.0   # mean > target by this % = flag
NEGATIVE_FADE_W     = 10    # second half avg > first half by this many watts = negative fade


# ── Session collection and analysis ──────────────────────────────────────────

def _infer_short_target(planned):
    """Parse 1-min target from session description (e.g. '10x1min @ 430w')."""
    import re
    desc = planned.get("description", "")
    for m in re.finditer(r'\d+x1min\s*@\s*(\d+)w', desc):
        return int(m.group(1))
    return planned.get("target_watts", 0)


def _analyze_outdoor_intensity_sessions(activities, plan, ftp_outdoor, ftp_indoor,
                                         max_hr, weeks):
    """
    Return per-session analysis dicts for all matched outdoor intensity sessions
    in the last `weeks` weeks.

    Each dict: {date, type, name, n_efforts, mean_w, target_w, pct_delta,
                fade_delta, mean_plateau, is_indoor}
    """
    week_offsets = list(range(0, weeks))
    seen_ids     = set()
    results      = []

    for offset in week_offsets:
        week_sessions = metrics.match_sessions_to_rides(
            plan, activities, ftp_outdoor, ftp_indoor, week_offset=offset
        )
        for entry in week_sessions:
            p   = entry.get("planned") or {}
            a   = entry.get("actual")
            typ = p.get("type", "")
            if typ not in ("vo2max", "anaerobic") or a is None:
                continue
            if a.get("trainer", False):          # indoor only — skip
                continue
            aid = a.get("id")
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)

            watts_raw = strava_client.fetch_power_stream(aid)
            hr_raw    = strava_client.fetch_hr_stream(aid)
            if not watts_raw:
                continue

            watts_arr = np.array(watts_raw, dtype=float)
            threshold = ftp_outdoor * 0.88
            efforts   = metrics.detect_intervals(watts_arr, threshold_watts=threshold)
            if not efforts:
                continue

            # For anaerobic: score only short (1-min) efforts
            if typ == "anaerobic":
                scored  = [e for e in efforts if e["duration_s"] <= SHORT_MAX_S]
                target  = _infer_short_target(p)
            else:
                scored  = efforts
                target  = p.get("target_watts", 0)

            if not scored or not target:
                continue

            # HR plateau (filtered for dropout)
            hr_arr = np.array(hr_raw, dtype=float) if hr_raw else None
            plateaus = []
            for e in scored:
                s, end = e["start_s"], e["start_s"] + e["duration_s"]
                if hr_arr is not None and end <= len(hr_arr) and e["duration_s"] >= 10:
                    seg = hr_arr[s:end]
                    pk  = int(np.max(seg))
                    if not (pk < MIN_PLAUSIBLE_HR and e["avg_watts"] > 350):
                        plateaus.append(round(pk / max_hr * 100, 1))

            mean_w  = round(sum(e["avg_watts"] for e in scored) / len(scored))
            pct     = round((mean_w - target) / target * 100, 1)

            fade = None
            if len(scored) >= 4:
                h     = len(scored) // 2
                first = sum(e["avg_watts"] for e in scored[:h]) / h
                last  = sum(e["avg_watts"] for e in scored[h:]) / (len(scored) - h)
                fade  = round(last - first)

            results.append({
                "date":         entry.get("date", "?"),
                "type":         typ,
                "name":         a.get("name", "?"),
                "n_efforts":    len(scored),
                "mean_w":       mean_w,
                "target_w":     target,
                "pct_delta":    pct,
                "fade_delta":   fade,
                "mean_plateau": round(sum(plateaus) / len(plateaus), 1) if plateaus else None,
            })

    results.sort(key=lambda x: x["date"])
    return results


# ── Anchor derivation ─────────────────────────────────────────────────────────

def _best_60s_from_db():
    """Query best 60s outdoor power from power_curve_weekly, most recent week."""
    try:
        import duckdb
        db_path = os.path.join(os.path.dirname(__file__), "data", "cycling_coach.duckdb")
        con = duckdb.connect(db_path, read_only=True)
        row = con.execute(
            "SELECT s60 FROM power_curve_weekly ORDER BY week_end DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _derive_anchors(sessions_by_type, ftp_outdoor):
    """
    From power curve data + FTP, derive calibrated target ranges.

    For 1-min anaerobic: 87% of best 60s outdoor power (from DB power curve).
    For 4-min VO2max: 117% FTP.

    Returns dict:
      "anaerobic_1min": {"anchor_w": int, "range": (lo, hi), "basis": str}
      "vo2max_4min":    {"anchor_w": int, "range": (lo, hi), "basis": str}
    """
    anchors = {}

    anaerobic = sessions_by_type.get("anaerobic", [])
    if anaerobic:
        best_60s = _best_60s_from_db()
        if best_60s:
            anchor = round(best_60s * 0.87)
            basis  = f"87% of {best_60s}w best 60s (power curve DB)"
        else:
            # Fallback: best observed session mean × 1.07 as proxy for peak
            best_observed = max(s["mean_w"] for s in anaerobic)
            proxy_peak    = round(best_observed * 1.07)
            anchor        = round(proxy_peak * 0.87)
            basis         = f"87% of ~{proxy_peak}w estimated 60s peak (best session mean {best_observed}w × 1.07 — no DB)"
        anchors["anaerobic_1min"] = {
            "anchor_w": anchor,
            "range":    (round(anchor * 0.97), round(anchor * 1.06)),
            "basis":    basis,
        }

    vo2max = sessions_by_type.get("vo2max", [])
    if vo2max:
        anchor = round(ftp_outdoor * 1.17)
        anchors["vo2max_4min"] = {
            "anchor_w": anchor,
            "range":    (round(ftp_outdoor * 1.15), round(ftp_outdoor * 1.20)),
            "basis":    f"117% of {ftp_outdoor}w FTP (115-120% VO2max zone)",
        }

    return anchors


# ── Calibration verdict ───────────────────────────────────────────────────────

def _session_verdict(sessions):
    """
    Return (is_over, is_negative_fade, mean_pct, mean_fade) for a list of sessions.
    """
    if not sessions:
        return False, False, None, None
    pcts   = [s["pct_delta"] for s in sessions if s["pct_delta"] is not None]
    fades  = [s["fade_delta"] for s in sessions if s["fade_delta"] is not None]
    mean_pct  = round(sum(pcts)  / len(pcts),  1) if pcts  else None
    mean_fade = round(sum(fades) / len(fades), 1) if fades else None
    is_over  = mean_pct  is not None and mean_pct  > OVERPERFORMANCE_PCT
    is_neg   = mean_fade is not None and mean_fade > NEGATIVE_FADE_W
    return is_over, is_neg, mean_pct, mean_fade


# ── preferences.md update ─────────────────────────────────────────────────────

SECTION_START = "## Outdoor Power Target Calibration"
SECTION_END   = "\n## "   # next H2 marks end (or EOF)


def _replace_calibration_section(new_content):
    """Replace the calibration section in preferences.md in place."""
    with open(PREFS_PATH) as f:
        text = f.read()

    start_idx = text.find(SECTION_START)
    if start_idx == -1:
        # Append if missing
        with open(PREFS_PATH, "a") as f:
            f.write(f"\n{new_content}\n")
        return

    # Find the end: next H2 heading after the section start
    end_search = text.find("\n## ", start_idx + len(SECTION_START))
    if end_search == -1:
        # Section runs to EOF
        text = text[:start_idx] + new_content
    else:
        text = text[:start_idx] + new_content + "\n" + text[end_search + 1:]

    with open(PREFS_PATH, "w") as f:
        f.write(text)


def _build_calibration_section(sessions_by_type, anchors, today_str):
    lines = [f"## Outdoor Power Target Calibration (updated {today_str})"]

    for typ, label in [("anaerobic", "1-min anaerobic repeats"),
                       ("vo2max",    "4-min VO2max repeats")]:
        sessions = sessions_by_type.get(typ, [])
        if not sessions:
            continue
        is_over, is_neg, mean_pct, mean_fade = _session_verdict(sessions)
        anchor_info = anchors.get(
            "anaerobic_1min" if typ == "anaerobic" else "vo2max_4min"
        )
        sign = "+" if mean_pct and mean_pct >= 0 else ""

        lines.append(f"\n### {label.capitalize()}")
        lines.append(f"Sessions analysed ({len(sessions)}): " +
                     ", ".join(f"{s['date']} ({sign}{s['pct_delta']}%)" for s in sessions))
        lines.append(f"Mean delta vs target: {sign}{mean_pct}% | "
                     f"Negative fade in {sum(1 for s in sessions if (s['fade_delta'] or 0) > NEGATIVE_FADE_W)}"
                     f"/{len(sessions)} sessions")

        if anchor_info:
            lo, hi = anchor_info["range"]
            lines.append(f"Calibrated target: {anchor_info['anchor_w']}w  "
                         f"(range {lo}-{hi}w)  |  basis: {anchor_info['basis']}")

        if is_over and is_neg:
            lines.append("Status: CONSERVATIVE — consistent overperformance + no fade. "
                         "Use calibrated target above for upcoming sessions.")
        elif is_over:
            lines.append("Status: SLIGHTLY CONSERVATIVE — above target but fade pattern unclear. "
                         "Monitor one more session before revising.")
        elif is_neg:
            lines.append("Status: SUSPICIOUS — negative fade without power overperformance. "
                         "Check if efforts are paced too evenly.")
        else:
            lines.append("Status: WELL CALIBRATED — targets are appropriately challenging.")

    lines.append(
        "\nNOTE: Best 20-min outdoor power is not a reliable anchor — "
        "insufficient sustained maximal efforts at that duration. "
        "Use FTP_OUTDOOR=323w (CP model) for zone math only."
    )
    lines.append(
        "Coach instruction: use the calibrated targets above for outdoor interval sessions, "
        "not raw FTP zone percentages. If negative fade reappears after bumping targets, "
        "step up again rather than waiting for RPE signals."
    )

    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def run_calibration(activities, plan, ftp_outdoor, ftp_indoor, max_hr=None,
                    weeks=4, update_prefs=True):
    """
    Analyse recent outdoor intensity sessions and optionally update preferences.md.

    Returns a compact summary string (suitable for injecting into the coach prompt)
    and a list of session analysis dicts.
    """
    if max_hr is None:
        max_hr = metrics.load_max_hr() or 185

    sessions = _analyze_outdoor_intensity_sessions(
        activities, plan, ftp_outdoor, ftp_indoor, max_hr, weeks
    )

    sessions_by_type = {"anaerobic": [], "vo2max": []}
    for s in sessions:
        if s["type"] in sessions_by_type:
            sessions_by_type[s["type"]].append(s)

    anchors = _derive_anchors(sessions_by_type, ftp_outdoor)

    # Build summary for coach prompt
    summary_lines = ["OUTDOOR TARGET CALIBRATION SUMMARY"]
    for typ, label in [("anaerobic", "1-min anaerobic"), ("vo2max", "4-min VO2max")]:
        typ_sessions = sessions_by_type.get(typ, [])
        if len(typ_sessions) < MIN_SESSIONS:
            summary_lines.append(f"  {label}: insufficient data ({len(typ_sessions)} sessions)")
            continue
        is_over, is_neg, mean_pct, mean_fade = _session_verdict(typ_sessions)
        anchor = anchors.get("anaerobic_1min" if typ == "anaerobic" else "vo2max_4min")
        sign   = "+" if mean_pct and mean_pct >= 0 else ""
        status = ("CONSERVATIVE" if (is_over and is_neg)
                  else "slightly conservative" if (is_over or is_neg)
                  else "well calibrated")
        anchor_str = f", calibrated target {anchor['anchor_w']}w" if anchor else ""
        summary_lines.append(
            f"  {label}: mean {sign}{mean_pct}% vs target, "
            f"negative fade in {sum(1 for s in typ_sessions if (s['fade_delta'] or 0) > NEGATIVE_FADE_W)}"
            f"/{len(typ_sessions)} sessions — {status}{anchor_str}"
        )

    summary = "\n".join(summary_lines)
    print(summary)

    # Update preferences.md if enough data
    if update_prefs:
        has_data = any(len(v) >= MIN_SESSIONS for v in sessions_by_type.values())
        if has_data:
            section = _build_calibration_section(
                sessions_by_type, anchors, date.today().isoformat()
            )
            _replace_calibration_section(section)
            print(f"preferences.md calibration section updated.")
        else:
            print("Insufficient sessions for calibration update — preferences.md unchanged.")

    return summary, sessions


# ── Standalone entry point ────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Calibrate outdoor power targets")
    p.add_argument("--weeks",       type=int,  default=4,    help="Weeks of data to analyse (default: 4)")
    p.add_argument("--no-update",   action="store_true",     help="Don't update preferences.md")
    p.add_argument("--no-refresh",  action="store_true",     help="Use cached activities (skip Strava fetch)")
    args = p.parse_args()

    if not args.no_refresh:
        strava_client.refresh_access_token()
    activities = strava_client.fetch_activities(
        weeks=max(args.weeks + 1, 4),
        force_refresh=not args.no_refresh,
    )

    ftp_outdoor, ftp_indoor = metrics.load_ftps()
    max_hr  = metrics.load_max_hr() or 185
    plan    = json.load(open(os.path.join(os.path.dirname(__file__), "data", "plan.json")))

    print(f"\nCalibration run — last {args.weeks} weeks | "
          f"FTP {ftp_outdoor}w | max HR {max_hr}bpm\n")

    run_calibration(
        activities, plan, ftp_outdoor, ftp_indoor,
        max_hr=max_hr, weeks=args.weeks,
        update_prefs=not args.no_update,
    )


if __name__ == "__main__":
    main()
