"""
email_builder.py — Cycling-themed HTML email template.
"""

import re
from datetime import date, datetime


TEAL   = "#0d9488"
GREEN  = "#16a34a"
SLATE  = "#1e293b"
LIGHT  = "#f8fafc"
BORDER = "#e2e8f0"
RED    = "#dc2626"
AMBER  = "#d97706"
PURPLE = "#7c3aed"


def _card(title, headline, body, accent=TEAL):
    # Render bullet points as line breaks
    body_html = body.replace("\n•", "<br>•").replace("\n", "<br>")
    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:{accent};padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">{title}</span>
      </div>
      <div style="padding:16px 20px;">
        <p style="margin:0 0 8px;font-size:16px;font-weight:700;color:{SLATE};">{headline}</p>
        <p style="margin:0;font-size:14px;color:#475569;line-height:1.6;">{body_html}</p>
      </div>
    </div>"""


def _stat_pill(label, value, color=SLATE):
    return f"""
      <div style="display:inline-block;margin:4px 6px;text-align:center;">
        <div style="font-size:20px;font-weight:800;color:{color};">{value}</div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;
                    letter-spacing:0.06em;">{label}</div>
      </div>"""


def _tsb_color(tsb):
    if tsb > 5:   return GREEN
    if tsb < -20: return RED
    return AMBER


def _fmt_zone_watts(zone_range):
    """Format a zone range tuple as a watts string."""
    if not zone_range:
        return ""
    lo, hi = zone_range
    if lo == 0:
        return f"<{hi}w"
    if hi is None:
        return f">{lo}w"
    return f"{lo}–{hi}w"


def _fmt_desc(desc):
    """Convert bullet-format description to HTML with line breaks."""
    if not desc:
        return "Rest"
    return desc.replace("\n•", "<br>•").replace("\n", "<br>")


def _add_watt_translations(desc, environment, ftp_outdoor, ftp_indoor):
    """
    Scan description text for watt values and append the indoor/outdoor equivalent.

    outdoor/flexible sessions: 355w  → 355w (🏠302w)        [outdoor → add indoor]
                               194–259w → 194–259w (🏠165–220w)
    indoor sessions:           302w  → 302w (🌿355w)         [indoor → add outdoor]

    Single combined regex pass prevents double-processing of range boundaries.
    Only matches 2+ digit numbers to avoid false positives.
    """
    if not desc:
        return desc
    if environment == "indoor":
        ratio = ftp_outdoor / ftp_indoor
        icon  = "🌿"
    else:
        ratio = ftp_indoor / ftp_outdoor
        icon  = "🏠"

    def replacer(m):
        if m.group(1) is not None:
            # Range: NNN-NNNw or NNN–NNNw
            lo, hi = int(m.group(1)), int(m.group(2))
            return f"{lo}–{hi}w ({icon}{round(lo * ratio)}–{round(hi * ratio)}w)"
        else:
            # Single: NNNw or NNNw+
            w    = int(m.group(3))
            plus = m.group(4) or ""
            return f"{w}w{plus} ({icon}{round(w * ratio)}w{plus})"

    # Range alternative tried first; single alternative is fallback — one pass, no double-processing
    return re.sub(r'(\d{2,})[–\-](\d{2,})w|(\d{2,})w(\+)?', replacer, desc)


_ZONE_NAMES = {
    "Z1": "Recovery", "Z2": "Endurance", "Z3": "Tempo",
    "Z4": "Threshold", "Z5": "VO2max", "Z6": "Anaerobic", "Z7": "Neuromusc",
}

_ZONE_COLORS = {
    "Z1": "#94a3b8", "Z2": "#22c55e", "Z3": "#84cc16",
    "Z4": "#f59e0b", "Z5": "#ef4444", "Z6": "#8b5cf6", "Z7": "#ec4899",
}


def _zones_reference_table(zone_ranges, ftp_outdoor, ftp_indoor):
    """Compact zones reference table for email header."""
    outdoor_zones = (zone_ranges or {}).get("outdoor", {})
    indoor_zones  = (zone_ranges or {}).get("indoor", {})

    row_html = ""
    for zone in ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7"]:
        name    = _ZONE_NAMES[zone]
        color   = _ZONE_COLORS.get(zone, TEAL)
        out_str = _fmt_zone_watts(outdoor_zones.get(zone)) if outdoor_zones.get(zone) else "—"
        in_str  = _fmt_zone_watts(indoor_zones.get(zone))  if indoor_zones.get(zone)  else "—"
        row_html += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:4px 8px;">
            <span style="display:inline-block;padding:1px 6px;border-radius:3px;
                         background:{color}20;color:{color};font-size:10px;
                         font-weight:700;">{zone}</span>
          </td>
          <td style="padding:4px 8px;font-size:10px;color:#64748b;">{name}</td>
          <td style="padding:4px 8px;font-size:10px;color:{SLATE};font-weight:600;">{out_str}</td>
          <td style="padding:4px 8px;font-size:10px;color:#7c3aed;font-weight:600;">{in_str}</td>
        </tr>"""

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:#334155;padding:8px 20px;display:flex;justify-content:space-between;
                  align-items:center;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Power Zones Reference</span>
        <span style="color:rgba(255,255,255,0.4);font-size:10px;">
          Outdoor FTP {ftp_outdoor}w &nbsp;|&nbsp; <span style="color:#a78bfa;">Indoor FTP {ftp_indoor}w</span>
        </span>
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f8fafc;">
            <th style="padding:4px 8px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Zone</th>
            <th style="padding:4px 8px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Name</th>
            <th style="padding:4px 8px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Outdoor</th>
            <th style="padding:4px 8px;text-align:left;font-size:9px;color:#a78bfa;text-transform:uppercase;">Indoor</th>
          </tr>
        </thead>
        <tbody>{row_html}</tbody>
      </table>
    </div>"""


def _feedback_indicator(strava_descriptions, checkin_data):
    """Returns a green banner if any feedback was received; empty string otherwise."""
    parts = []
    if strava_descriptions:
        n = len(strava_descriptions)
        parts.append(f"{n} ride description{'s' if n != 1 else ''}")
    if checkin_data:
        parts.append("check-in submitted")
    if not parts:
        return ""
    text = "  ·  ".join(parts)
    return (f'<div style="padding:5px 10px;background:#dcfce7;border-bottom:1px solid #bbf7d0;">'
            f'<span style="font-size:10px;color:#16a34a;font-weight:600;">✓ Feedback received: {text}</span>'
            f'</div>')


def _rides_data_section(last_week_summary, data_range_start, data_range_end,
                        ftp_outdoor=323, ftp_indoor=275,
                        strava_descriptions=None, checkin_data=None):
    """Show date range of analyzed data and last week's rides."""
    rides = last_week_summary.get("rides", [])

    ride_rows = ""
    for r in rides:
        env_icon = "🏠" if r.get("trainer") else "🌿"
        if r.get("avg_watts"):
            if r.get("trainer"):
                outdoor_eq = round(r["avg_watts"] * ftp_outdoor / ftp_indoor)
                watts_str = (f"🏠 {r['avg_watts']}w "
                             f'<span style="color:#94a3b8;font-size:10px;">(→{outdoor_eq}w)</span>')
            else:
                watts_str = f"{r['avg_watts']}w"
        else:
            watts_str = "—"
        tss_str   = str(r["tss"]) if r.get("tss") else "—"
        try:
            d = datetime.fromisoformat(r["date"])
            date_fmt = d.strftime("%a %-d %b")
        except Exception:
            date_fmt = r.get("date", "")

        ride_rows += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:5px 10px;font-size:11px;color:#64748b;white-space:nowrap;">{date_fmt}</td>
          <td style="padding:5px 10px;font-size:11px;color:{SLATE};">{env_icon} {r.get('name','')}</td>
          <td style="padding:5px 10px;font-size:11px;color:{SLATE};text-align:right;">{r.get('duration_min',0)}min</td>
          <td style="padding:5px 10px;font-size:11px;font-weight:600;color:{TEAL};text-align:right;">{watts_str}</td>
          <td style="padding:5px 10px;font-size:11px;color:#64748b;text-align:right;">{tss_str} TSS</td>
        </tr>"""

    if not rides:
        ride_rows = f"""
        <tr><td colspan="5" style="padding:10px;font-size:12px;color:#94a3b8;text-align:center;">
          No rides logged last week
        </td></tr>"""

    week_start = last_week_summary.get("week_start", "")
    week_end   = last_week_summary.get("week_end", "")
    try:
        ws = datetime.fromisoformat(week_start).strftime("%b %-d")
        we = datetime.fromisoformat(week_end).strftime("%b %-d")
        week_range_str = f"{ws} – {we}"
    except Exception:
        week_range_str = f"{week_start} – {week_end}"

    try:
        rs = datetime.fromisoformat(data_range_start).strftime("%b %-d")
        re_str = datetime.fromisoformat(data_range_end).strftime("%b %-d, %Y")
        history_str = f"Based on 8 weeks of data: {rs} – {re_str}"
    except Exception:
        history_str = f"Data: {data_range_start} – {data_range_end}"

    feedback_html = _feedback_indicator(strava_descriptions, checkin_data)

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:#475569;padding:8px 20px;display:flex;justify-content:space-between;align-items:center;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Last Week Rides</span>
        <span style="color:rgba(255,255,255,0.4);font-size:10px;">{week_range_str}</span>
      </div>
      {feedback_html}<div style="padding:6px 10px;background:#f8fafc;border-bottom:1px solid {BORDER};">
        <span style="font-size:10px;color:#94a3b8;">📊 {history_str}</span>
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <tbody>{ride_rows}</tbody>
      </table>
    </div>"""


def _week_table(this_week_plan, zone_ranges=None, ftp_outdoor=323, ftp_indoor=275):
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    day_names = {
        "mon": "MON", "tue": "TUE", "wed": "WED", "thu": "THU",
        "fri": "FRI", "sat": "SAT", "sun": "SUN"
    }
    rows = ""
    for day in days:
        session  = this_week_plan.get(day, {})
        stype    = session.get("type", "rest")
        dur      = session.get("duration_min", "—")
        zone     = session.get("zone_label", "")
        lo, hi   = (session.get("target_watts_range") or [None, None])[:2]
        env      = session.get("environment", "flexible")
        optional = session.get("optional", False)
        desc     = session.get("description", "Rest")

        bg = "#f8fafc" if stype == "rest" else "#ffffff"
        type_color = {
            "rest":       "#94a3b8",
            "z2":         "#22c55e",
            "tempo":      "#84cc16",
            "sweet_spot": "#f97316",
            "vo2max":     "#ef4444",
            "anaerobic":  "#8b5cf6",
            "race":       "#0ea5e9",
        }.get(stype, TEAL)

        zone_colors = {
            "Z1": "#94a3b8", "Z2": "#22c55e", "Z3": "#84cc16",
            "Z4": "#f59e0b", "Z5": "#ef4444", "Z6": "#8b5cf6", "Z7": "#ec4899",
        }
        zone_color = zone_colors.get(zone, type_color)

        # Target cell: zone label + dual watts (outdoor + indoor)
        if stype == "rest":
            target_cell = "—"
        elif zone and zone_ranges:
            out_range = zone_ranges.get("outdoor", {}).get(zone)
            in_range  = zone_ranges.get("indoor",  {}).get(zone)
            if out_range and in_range:
                out_str = _fmt_zone_watts(out_range)
                in_str  = _fmt_zone_watts(in_range)
                if env == "outdoor":
                    out_style = f"color:{TEAL};font-weight:700;"
                    in_style  = "color:#94a3b8;"
                elif env == "indoor":
                    out_style = "color:#94a3b8;"
                    in_style  = f"color:{PURPLE};font-weight:700;"
                else:  # flexible
                    out_style = f"color:{TEAL};"
                    in_style  = f"color:{PURPLE};"
                target_cell = (
                    f'<span style="font-weight:700;color:{zone_color};">{zone}</span><br>'
                    f'<span style="{out_style}font-size:11px;">🌿 {out_str}</span><br>'
                    f'<span style="{in_style}font-size:11px;">🏠 {in_str}</span>'
                )
            elif zone:
                target_cell = f'<span style="font-weight:700;color:{zone_color};">{zone}</span>'
            else:
                target_cell = "—"
        elif zone and lo and hi:
            indoor_lo = round(lo * ftp_indoor / ftp_outdoor)
            indoor_hi = round(hi * ftp_indoor / ftp_outdoor)
            target_cell = (
                f'<span style="font-weight:700;color:{zone_color};">{zone}</span><br>'
                f'<span style="color:{TEAL};font-size:11px;">🌿 {lo}–{hi}w</span><br>'
                f'<span style="color:{PURPLE};font-size:11px;">🏠 {indoor_lo}–{indoor_hi}w</span>'
            )
        elif lo and hi:
            indoor_lo = round(lo * ftp_indoor / ftp_outdoor)
            indoor_hi = round(hi * ftp_indoor / ftp_outdoor)
            target_cell = (
                f'<span style="color:{TEAL};font-size:11px;">🌿 {lo}–{hi}w</span><br>'
                f'<span style="color:{PURPLE};font-size:11px;">🏠 {indoor_lo}–{indoor_hi}w</span>'
            )
        elif zone:
            target_cell = f'<span style="font-weight:700;color:{zone_color};">{zone}</span>'
        else:
            target_cell = "—"

        # Environment indicator
        env_badge = ""
        if stype != "rest":
            if env == "indoor":
                env_badge = ' <span style="font-size:9px;color:#a78bfa;">🏠 indoor</span>'
            elif env == "outdoor":
                env_badge = ' <span style="font-size:9px;color:#22c55e;">🌿 outdoor</span>'

        # Optional badge
        optional_badge = ""
        if optional:
            optional_badge = (' <span style="font-size:9px;padding:1px 4px;border-radius:3px;'
                              'background:#fef3c7;color:#d97706;font-weight:700;">OPTIONAL</span>')

        # Format description as bullets, with inline watt translations
        if stype != "rest":
            desc_translated = _add_watt_translations(desc, env, ftp_outdoor, ftp_indoor)
            desc_html = _fmt_desc(desc_translated)
        else:
            desc_html = ""

        day_label = day_names.get(day, day.upper())
        rows += f"""
        <tr class="week-row" style="background:{bg};border-bottom:1px solid {BORDER};">
          <td class="week-col-day" style="padding:8px 12px;font-size:12px;font-weight:700;color:#94a3b8;
                     text-transform:uppercase;width:40px;vertical-align:top;">{day_label}</td>
          <td class="week-col-type" style="padding:8px 12px;vertical-align:top;white-space:nowrap;">
            <span style="display:inline-block;padding:2px 8px;border-radius:4px;
                         background:{type_color}20;color:{type_color};
                         font-size:11px;font-weight:700;text-transform:uppercase;">{stype}</span>
            {optional_badge}
          </td>
          <td class="week-col-dur" style="padding:8px 12px;font-size:13px;color:{SLATE};width:50px;vertical-align:top;">
            {dur if stype != 'rest' else '—'} {'min' if stype != 'rest' else ''}
          </td>
          <td class="week-col-tgt" style="padding:8px 12px;font-size:12px;width:100px;vertical-align:top;">
            {target_cell}{env_badge}
          </td>
          <td class="week-col-desc" style="padding:8px 12px;font-size:12px;color:#475569;line-height:1.5;">{desc_html}</td>
        </tr>"""

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:{SLATE};padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">This Week's Plan</span>
      </div>
      <table class="week-table" style="width:100%;border-collapse:collapse;">
        <thead class="week-thead">
          <tr style="background:#f1f5f9;">
            <th style="padding:6px 12px;text-align:left;font-size:10px;color:#94a3b8;
                       text-transform:uppercase;">Day</th>
            <th style="padding:6px 12px;text-align:left;font-size:10px;color:#94a3b8;
                       text-transform:uppercase;">Type</th>
            <th style="padding:6px 12px;text-align:left;font-size:10px;color:#94a3b8;
                       text-transform:uppercase;">Duration</th>
            <th style="padding:6px 12px;text-align:left;font-size:10px;color:#94a3b8;
                       text-transform:uppercase;">Target</th>
            <th style="padding:6px 12px;text-align:left;font-size:10px;color:#94a3b8;
                       text-transform:uppercase;">Workout</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _session_comparison_table(session_comparison, zone_ranges=None, ftp_outdoor=323, ftp_indoor=275):
    """Tabular planned vs actual comparison card."""
    if not session_comparison:
        return ""

    status_cfg = {
        "hit":          (GREEN,     "✓ Hit"),
        "overachieved": (PURPLE,    "↑ Over"),
        "partial":      (AMBER,     "~ Partial"),
        "miss":         (RED,       "✗ Miss"),
        "missed":       (RED,       "✗ No ride"),
        "done":         (TEAL,      "✓ Done"),
        "rest":         ("#94a3b8", "—"),
        "unplanned":    ("#64748b", "Unplanned"),
    }

    rows = ""
    for day in session_comparison:
        day_name = day["day"][:3].upper()
        p        = day.get("planned") or {}
        a        = day.get("actual")
        status   = day.get("status", "rest")
        color, label = status_cfg.get(status, ("#94a3b8", status))

        # Planned cell
        if not p or p.get("type") == "rest":
            planned_cell = '<span style="color:#94a3b8;">REST</span>'
        else:
            zone  = p.get("zone_label", "")
            r     = p.get("target_watts_range") or []
            ptype = p.get("type", "").upper()
            dur   = p.get("duration_min", "")
            opt   = " <em style='color:#d97706;font-size:10px;'>(opt)</em>" if p.get("optional") else ""
            zone_str = f"{zone} " if zone else ""
            # Dual watts display
            if zone_ranges and zone:
                out_range = zone_ranges.get("outdoor", {}).get(zone)
                in_range  = zone_ranges.get("indoor", {}).get(zone)
                if out_range and in_range:
                    watts_str = f"🌿{_fmt_zone_watts(out_range)} / 🏠{_fmt_zone_watts(in_range)}"
                elif len(r) == 2:
                    watts_str = f"{r[0]}–{r[1]}w"
                else:
                    watts_str = ""
            elif len(r) == 2:
                indoor_lo = round(r[0] * ftp_indoor / ftp_outdoor)
                indoor_hi = round(r[1] * ftp_indoor / ftp_outdoor)
                watts_str = f"🌿{r[0]}–{r[1]}w / 🏠{indoor_lo}–{indoor_hi}w"
            else:
                watts_str = ""
            planned_cell = f"{ptype} {dur}min<br><small>{zone_str}{watts_str}</small>{opt}"

        # Actual cell
        if a:
            w_str  = f"{a.get('avg_watts','?')}w" if a.get("avg_watts") else "—"
            tss_s  = f"TSS {a.get('tss','?')}" if a.get("tss") else ""
            avg_hr = a.get("average_heartrate")
            max_hr = a.get("max_heartrate")
            hr_str = (f' · <span style="color:#64748b;">♥ {avg_hr:.0f}/{max_hr:.0f}</span>'
                      if avg_hr and max_hr else "")
            detail_html = ""

            istats = a.get("interval_stats")
            if istats:
                efforts = istats.get("efforts", [])
                target_w = istats.get("target_watts", 0)
                delta = istats["pct_delta"]
                delta_color = GREEN if delta >= 0 else RED
                delta_str = f"+{delta}%" if delta >= 0 else f"{delta}%"
                if efforts:
                    pills = ""
                    prev_label = None
                    for i, e in enumerate(efforts, 1):
                        avg = e["avg_watts"]
                        dur_s_total = e["duration_s"]
                        dur_m, dur_s = dur_s_total // 60, dur_s_total % 60
                        e_target = e.get("target_watts") or target_w
                        sym = "✓" if avg >= e_target else ("~" if avg >= e_target * 0.95 else "✗")
                        sym_col = GREEN if sym == "✓" else (AMBER if sym == "~" else RED)
                        # Insert set divider when set changes
                        cur_label = e.get("set_label")
                        if cur_label and cur_label != prev_label and prev_label is not None:
                            pills += (
                                f'<span style="display:inline-block;margin:1px 4px;'
                                f'font-size:9px;color:#94a3b8;">│</span>'
                            )
                        prev_label = cur_label
                        pills += (
                            f'<span style="display:inline-block;margin:1px 2px;padding:1px 5px;'
                            f'border-radius:3px;background:#f1f5f9;font-size:10px;color:{SLATE};">'
                            f'#{i}&nbsp;{dur_m}:{dur_s:02d}&nbsp;'
                            f'<strong>{avg}w</strong>'
                            f'<span style="color:#94a3b8;font-size:9px;">/{e_target}w</span>&nbsp;'
                            f'<span style="color:{sym_col};">{sym}</span>'
                            f'</span>'
                        )
                    detail_html = (
                        f'<div style="margin:3px 0;line-height:1.8;">{pills}</div>'
                        f'<small style="color:#64748b;">avg {istats["avg_interval_watts"]}w · '
                        f'<span style="color:{delta_color};font-weight:700;">'
                        f'{delta_str} vs {target_w}w target</span></small>'
                    )
                else:
                    detail_html = (
                        f'<br><small style="color:#64748b;">'
                        f'{istats["efforts_detected"]} efforts · avg {istats["avg_interval_watts"]}w · '
                        f'<span style="color:{delta_color};font-weight:700;">{delta_str} vs target</span>'
                        f'</small>'
                    )

            zstats = a.get("zone_stats")
            if zstats:
                total = max(zstats.get("total_s", 1), 1)
                pct_in    = zstats.get("pct_in_zone", 0)
                pct_below = round(zstats.get("below_s", 0) / total * 100)
                pct_above = round(zstats.get("above_s", 0) / total * 100)
                main_avg  = zstats.get("main_block_avg")
                in_col    = GREEN if pct_in >= 65 else (AMBER if pct_in >= 45 else RED)
                below_col = RED if pct_below > 20 else (AMBER if pct_below > 10 else "#64748b")
                above_col = AMBER if pct_above > 15 else "#64748b"
                main_str  = f' · block {main_avg}w' if main_avg else ""
                detail_html = (
                    f'<br><small style="color:#64748b;">'
                    f'<span style="color:{in_col};font-weight:700;">{pct_in}% in zone</span> · '
                    f'<span style="color:{below_col};">{pct_below}% below</span> · '
                    f'<span style="color:{above_col};">{pct_above}% above</span>'
                    f'{main_str}</small>'
                )

            actual_cell = (
                f"{a.get('name','Ride')}<br>"
                f"<small>{a.get('duration_min',0)}min · {w_str} · {tss_s}{hr_str}</small>"
                f"{detail_html}"
            )
        elif status in ("rest",):
            actual_cell = '<span style="color:#94a3b8;">—</span>'
        else:
            actual_cell = '<span style="color:#dc2626;">No ride</span>'

        bg = "#fff7f7" if status in ("missed", "miss") else (
             "#f0fdf4" if status in ("hit", "done") else "#ffffff")

        rows += f"""
        <tr class="cmp-row" style="background:{bg};border-bottom:1px solid {BORDER};">
          <td class="cmp-col-day" style="padding:6px 10px;font-size:11px;font-weight:700;color:#94a3b8;
                     vertical-align:top;">{day_name}</td>
          <td class="cmp-col-planned" style="padding:6px 10px;font-size:11px;color:{SLATE};vertical-align:top;">{planned_cell}</td>
          <td class="cmp-col-actual" style="padding:6px 10px;font-size:11px;color:{SLATE};vertical-align:top;">{actual_cell}</td>
          <td class="cmp-col-status" style="padding:6px 10px;text-align:center;vertical-align:top;">
            <span style="font-size:11px;font-weight:700;color:{color};">{label}</span>
          </td>
        </tr>"""

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:#334155;padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Planned vs Actual — Last Week</span>
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <thead class="cmp-thead">
          <tr style="background:#f1f5f9;">
            <th style="padding:5px 10px;text-align:left;font-size:9px;color:#94a3b8;
                       text-transform:uppercase;width:35px;">Day</th>
            <th style="padding:5px 10px;text-align:left;font-size:9px;color:#94a3b8;
                       text-transform:uppercase;">Planned</th>
            <th style="padding:5px 10px;text-align:left;font-size:9px;color:#94a3b8;
                       text-transform:uppercase;">Actual</th>
            <th style="padding:5px 10px;text-align:center;font-size:9px;color:#94a3b8;
                       text-transform:uppercase;width:60px;">Status</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _power_curve_card(power_curve_data, ftp_outdoor, curve_trend=None, is_recovery_week=False):
    """
    Power curve card — best rolling average at 1/5/10/20-min (outdoor only, 8 weeks).
    curve_trend: optional dict from db.power_curve_trend() with rolling peak deltas.
    is_recovery_week: if True, suppress trend display.
    """
    if not power_curve_data:
        return ""
    if not any(v.get("best") for v in power_curve_data.values()):
        return ""

    DURATION_LABELS = {60: "1-min", 300: "5-min", 600: "10-min", 1200: "20-min"}

    rows = ""
    for dur, label in DURATION_LABELS.items():
        entry = power_curve_data.get(dur, {})
        best = entry.get("best")

        if not best:
            watts_cell = '<span style="color:#94a3b8;">—</span>'
            trend_cell = ""
        else:
            ftp_note = ""
            if dur == 1200 and abs(best - ftp_outdoor) / ftp_outdoor <= 0.05:
                ftp_note = (f' <span style="font-size:10px;font-weight:700;color:{PURPLE};">'
                            f'≈ FTP confirm</span>')
            watts_cell = f'<span style="font-size:15px;font-weight:800;color:{SLATE};">{best}w</span>{ftp_note}'

            if is_recovery_week:
                trend_cell = '<span style="color:#94a3b8;font-size:11px;">— recovery week</span>'
            elif curve_trend and dur in curve_trend and curve_trend[dur].get("delta") is not None:
                t = curve_trend[dur]
                delta = t.get("delta")
                if delta > 0:
                    trend_cell = (f'<span style="color:{GREEN};font-size:12px;">'
                                  f'↑ +{delta}w (4wk peak)</span>')
                elif delta < 0:
                    trend_cell = (f'<span style="color:{RED};font-size:12px;">'
                                  f'↓ {delta}w (4wk peak)</span>')
                else:
                    trend_cell = (f'<span style="color:{AMBER};font-size:12px;">'
                                  f'→ flat (4wk peak)</span>')
            else:
                # Fall back to single-week comparison
                best_4wk = entry.get("best_4wk_ago")
                if best_4wk:
                    delta = best - best_4wk
                    if delta > 0:
                        trend_cell = (f'<span style="color:{GREEN};font-size:12px;">'
                                      f'↑ +{delta}w vs 4wk ago</span>')
                    elif delta < 0:
                        trend_cell = (f'<span style="color:{RED};font-size:12px;">'
                                      f'↓ {delta}w vs 4wk ago</span>')
                    else:
                        trend_cell = (f'<span style="color:{AMBER};font-size:12px;">'
                                      f'→ flat vs 4wk ago</span>')
                else:
                    trend_cell = '<span style="color:#94a3b8;font-size:11px;">— no prior data</span>'

        rows += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:7px 14px;font-size:12px;font-weight:700;color:#64748b;width:60px;">{label}</td>
          <td style="padding:7px 14px;width:120px;">{watts_cell}</td>
          <td style="padding:7px 14px;">{trend_cell}</td>
        </tr>"""

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:#0369a1;padding:10px 20px;display:flex;
                  justify-content:space-between;align-items:center;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Power Curve</span>
        <span style="color:rgba(255,255,255,0.4);font-size:10px;">
          Best efforts · outdoor only · 8 weeks
        </span>
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _ef_hr_card(ef_data, interval_hr_data, rpe_signals):
    """
    Aerobic Efficiency + HR Response card.
    Synthesis narrative at top + detail rows below.
    """
    if not ef_data and not interval_hr_data and not rpe_signals:
        return ""

    ef_val = (ef_data or {}).get("ef_recent")
    trend_pct = (ef_data or {}).get("ef_trend_pct")
    dc = (ef_data or {}).get("avg_decoupling")
    rec = (interval_hr_data or {}).get("recovery_60s_recent")
    rec_delta = (interval_hr_data or {}).get("recovery_delta")

    # --- Coach-voice prose synthesis ---
    prose_parts = []
    positives = negatives = 0

    if ef_val:
        if trend_pct is not None and trend_pct > 1:
            prose_parts.append(
                f'Your efficiency factor is <strong>{ef_val}</strong> and climbing '
                f'<span style="color:{GREEN};">+{trend_pct}% vs the prior 4 weeks</span> — '
                f'a clear aerobic adaptation signal.')
            positives += 1
        elif trend_pct is not None and trend_pct < -1:
            prose_parts.append(
                f'Your efficiency factor has slipped to <strong>{ef_val}</strong> '
                f'<span style="color:{RED};">({trend_pct}% vs prior 4wk)</span>. '
                f'Watch for accumulated fatigue or under-recovery.')
            negatives += 1
        elif ef_val:
            prose_parts.append(
                f'Efficiency factor is holding at <strong>{ef_val}</strong> — '
                f'stable but not yet trending up.')

    if dc is not None:
        if dc < 5:
            prose_parts.append(
                f'Z2 decoupling at <span style="color:{GREEN};">{dc}%</span> — '
                f'your aerobic base is solid and cardiac drift is minimal.')
            positives += 1
        elif dc < 10:
            prose_parts.append(
                f'Z2 decoupling at <span style="color:{AMBER};">{dc}%</span> — '
                f'some drift showing in long rides. Keep Z2 volume steady before adding intensity.')
        else:
            prose_parts.append(
                f'Z2 decoupling at <span style="color:{RED};">{dc}%</span> — '
                f'significant cardiac drift. Hold off on intensity until this improves below 10%.')
            negatives += 1

    if rec:
        if rec_delta is not None and rec_delta > 0:
            prose_parts.append(
                f'Post-interval HR recovery improved to <strong>{rec} BPM/60s</strong> '
                f'<span style="color:{GREEN};">(+{rec_delta} vs prior 4wk)</span> — '
                f'cardiac fitness is responding to the interval work.')
            positives += 1
        elif rec_delta is not None and rec_delta < 0:
            prose_parts.append(
                f'Post-interval HR recovery dropped to <strong>{rec} BPM/60s</strong> '
                f'<span style="color:{RED};">({rec_delta} vs prior 4wk)</span> — '
                f'may indicate residual fatigue going into hard sessions.')
            negatives += 1
        elif rec:
            prose_parts.append(
                f'Post-interval HR recovery is <strong>{rec} BPM/60s</strong>.')

    if rpe_signals:
        struggling = [r for r in rpe_signals if r.get("rpe_vs_power") == "struggling"]
        underpacing = [r for r in rpe_signals if r.get("rpe_vs_power") == "underpacing"]
        if struggling:
            prose_parts.append(
                f'<span style="color:{RED};">Ride notes flag {len(struggling)} session(s) where '
                f'effort felt disproportionately hard vs power output</span> — worth monitoring.')
            negatives += 1
        elif underpacing:
            prose_parts.append(
                f'<span style="color:{AMBER};">Notes suggest {len(underpacing)} session(s) '
                f'where power was below target despite low RPE</span> — investigate pacing.')
        else:
            prose_parts.append(
                f'RPE from ride notes is <span style="color:{GREEN};">well-calibrated</span> '
                f'against power delivery.')
            positives += 1

    if negatives > positives:
        overall_color = RED
    elif positives >= 2:
        overall_color = GREEN
    else:
        overall_color = AMBER

    synthesis_html = ""
    if prose_parts:
        prose_html = " ".join(f'<span>{p}</span>' for p in prose_parts)
        synthesis_html = f"""
      <div style="padding:12px 20px;border-bottom:1px solid {BORDER};background:#f8fafc;">
        <p style="margin:0;font-size:13px;color:#334155;line-height:1.75;">
          {prose_html}
        </p>
      </div>"""

    # --- Detail rows ---
    rows = ""

    if ef_data and ef_val:
        if trend_pct is not None:
            if trend_pct > 1:
                trend_html = f'<span style="color:{GREEN};font-weight:700;">↑ +{trend_pct}% vs prior 4wk</span>'
            elif trend_pct < -1:
                trend_html = f'<span style="color:{RED};font-weight:700;">↓ {trend_pct}% vs prior 4wk</span>'
            else:
                trend_html = f'<span style="color:{AMBER};font-weight:700;">→ flat vs prior 4wk</span>'
        else:
            trend_html = '<span style="color:#94a3b8;">— insufficient history</span>'

        rows += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:7px 14px;font-size:12px;font-weight:700;color:#64748b;width:150px;">
            Efficiency Factor</td>
          <td style="padding:7px 14px;font-size:15px;font-weight:800;color:{SLATE};width:80px;">
            {ef_val}</td>
          <td style="padding:7px 14px;font-size:12px;">{trend_html}<br>
            <span style="color:#94a3b8;font-size:10px;">NP / avg HR · rising = aerobic adaptation</span>
          </td>
        </tr>"""

        if dc is not None:
            dc_color = GREEN if dc < 5 else (AMBER if dc < 10 else RED)
            dc_note = "solid aerobic base" if dc < 5 else ("marginal drift" if dc < 10 else "cardiac drift — hold Z2")
            rows += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:7px 14px;font-size:12px;font-weight:700;color:#64748b;">
            Z2 Decoupling</td>
          <td style="padding:7px 14px;font-size:15px;font-weight:800;color:{dc_color};">
            {dc}%</td>
          <td style="padding:7px 14px;font-size:12px;color:#475569;">{dc_note}<br>
            <span style="color:#94a3b8;font-size:10px;">&lt;5% ideal · &gt;10% = cardiac drift</span>
          </td>
        </tr>"""

    if interval_hr_data and rec:
        rec_delta = interval_hr_data.get("recovery_delta")
        if rec_delta is not None and rec_delta > 0:
            rec_trend = f'<span style="color:{GREEN};">↑ +{rec_delta} BPM vs prior 4wk</span>'
        elif rec_delta is not None and rec_delta < 0:
            rec_trend = f'<span style="color:{RED};">↓ {rec_delta} BPM vs prior 4wk</span>'
        else:
            rec_trend = f'<span style="color:{AMBER};">→ flat</span>'

        plateau = interval_hr_data.get("hr_plateau_pct")
        plateau_html = ""
        if plateau:
            p_color = GREEN if plateau >= 92 else AMBER
            plateau_html = (f'<br><span style="font-size:11px;color:{p_color};">'
                            f'HR plateau: {plateau}% of max</span>')

        rows += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:7px 14px;font-size:12px;font-weight:700;color:#64748b;">
            HR Recovery / 60s</td>
          <td style="padding:7px 14px;font-size:15px;font-weight:800;color:{SLATE};">
            {rec} BPM</td>
          <td style="padding:7px 14px;font-size:12px;">{rec_trend}{plateau_html}<br>
            <span style="color:#94a3b8;font-size:10px;">drop after intervals · higher = better recovery</span>
          </td>
        </tr>"""

    if rpe_signals:
        struggling = [r for r in rpe_signals if r.get("rpe_vs_power") == "struggling"]
        underpacing = [r for r in rpe_signals if r.get("rpe_vs_power") == "underpacing"]
        avg_rpe = round(sum(r.get("inferred_rpe", 3) for r in rpe_signals) / len(rpe_signals), 1)

        if struggling:
            rpe_color, rpe_note = RED, f"{len(struggling)} session(s) showing struggle signals"
        elif underpacing:
            rpe_color, rpe_note = AMBER, f"{len(underpacing)} session(s) underpacing"
        else:
            rpe_color, rpe_note = GREEN, "calibrated effort signals"

        sample_keywords = []
        for r in rpe_signals[:3]:
            sample_keywords.extend(r.get("keywords", [])[:2])

        rows += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:7px 14px;font-size:12px;font-weight:700;color:#64748b;">
            RPE (inferred)</td>
          <td style="padding:7px 14px;font-size:15px;font-weight:800;color:{SLATE};">
            {avg_rpe}/5</td>
          <td style="padding:7px 14px;font-size:12px;color:{rpe_color};font-weight:600;">
            {rpe_note}<br>
            <span style="color:#94a3b8;font-size:10px;font-weight:400;">
              {', '.join(sample_keywords[:5]) if sample_keywords else 'from ride descriptions'}
            </span>
          </td>
        </tr>"""

    if not synthesis_html and not rows:
        return ""

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:#7c3aed;padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Aerobic Efficiency + HR Response</span>
      </div>
      {synthesis_html}
      <table style="width:100%;border-collapse:collapse;">
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _block_overview_card(block_overview):
    """Block overview card — shown on --init only."""
    if not block_overview:
        return ""

    goal     = block_overview.get("goal", "")
    struct   = block_overview.get("structure", "")
    rationale = block_overview.get("physiological_rationale", "")
    key_sess = block_overview.get("key_sessions", [])
    tss_prog = block_overview.get("tss_progression", [])

    key_sess_html = "".join(f"<li style='margin:2px 0;font-size:13px;color:#475569;'>{s}</li>"
                            for s in key_sess)
    tss_prog_html = " → ".join(f"<strong>{t}</strong>" for t in tss_prog)

    body_html = f"""
      <p style="margin:0 0 6px;font-size:14px;color:{SLATE};">{goal}</p>
      <p style="margin:0 0 6px;font-size:13px;color:#64748b;">{struct}</p>
      {"<ul style='margin:6px 0 6px 16px;padding:0;'>" + key_sess_html + "</ul>" if key_sess else ""}
      {"<p style='margin:4px 0;font-size:12px;color:#94a3b8;'>TSS progression: " + tss_prog_html + "</p>" if tss_prog else ""}
      {"<p style='margin:6px 0 0;font-size:12px;color:#64748b;font-style:italic;'>" + rationale + "</p>" if rationale else ""}
    """

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:{PURPLE};padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Your 5-Week Training Block</span>
      </div>
      <div style="padding:16px 20px;">{body_html}</div>
    </div>"""


def _fmt_elapsed(seconds):
    """Format elapsed seconds as M:SS."""
    if not seconds:
        return "—"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _segment_card(segment_data):
    """KOM Targets card — Tier 1 segments with rank, gap, achievability score."""
    if not segment_data:
        return ""

    tier1 = [s for s in segment_data if s.get("tier") == 1]
    if not tier1:
        return ""

    def _fmt_rank(rank):
        if rank is None: return ">10"
        suffixes = {1: "st", 2: "nd", 3: "rd"}
        return f"{rank}{suffixes.get(rank, 'th')}"

    rows = ""
    for seg in tier1[:5]:
        seg_id    = seg.get("segment_id", "")
        name      = seg.get("segment_name", "Unknown")
        avg_grade = seg.get("avg_grade") or 0
        elapsed   = seg.get("elapsed_time_s")
        kom       = seg.get("kom_time_s")
        total     = seg.get("total_athletes", "?")
        time_gap  = seg.get("time_gap_s")
        power_gap = seg.get("power_gap_w")
        rank      = seg.get("david_rank")
        pct       = seg.get("pct_power_increase")
        power_tier = seg.get("power_tier")

        pr_str  = _fmt_elapsed(elapsed)
        kom_str = _fmt_elapsed(kom)

        seg_url   = f"https://www.strava.com/segments/{seg_id}"
        rank_str  = _fmt_rank(rank)
        rank_col  = GREEN if rank == 1 else (AMBER if rank and rank <= 5 else (TEAL if rank and rank <= 10 else "#64748b"))

        athletes_str = f"{total:,}" if isinstance(total, int) else str(total)

        # Time gap display
        if time_gap is None:
            gap_color, gap_str = "#94a3b8", "—"
        elif time_gap <= 0:
            gap_color, gap_str = GREEN, "🏆 KOM"
        elif time_gap <= 30:
            gap_color, gap_str = AMBER, f"+{time_gap}s"
        else:
            m, s = divmod(time_gap, 60)
            gap_str = f"+{m}:{s:02d}" if m else f"+{time_gap}s"
            gap_color = RED

        # Power delta (watts needed to close time gap on climb)
        if power_gap is None or power_gap == 0:
            pw_color, pw_str = "#94a3b8", "—"
        elif power_gap <= 20:
            pw_color, pw_str = AMBER, f"+{power_gap}w"
        else:
            pw_color, pw_str = RED, f"+{power_gap}w"

        # +% power needed to match KOM (3-tier metric)
        if pct is None:
            pct_color, pct_str = "#94a3b8", "—"
        elif pct <= 0:
            pct_color, pct_str = GREEN, "KOM 🏆"
        else:
            pct_color = GREEN if pct <= 10 else (AMBER if pct <= 25 else RED)
            prefix = "~" if power_tier in ("B", "C") else ""
            suffix = " (PR)" if power_tier == "B" else (" (est)" if power_tier == "C" else "")
            pct_str = f"{prefix}+{pct:.0f}%{suffix}"

        rows += f"""
        <tr style="border-bottom:1px solid {BORDER};">
          <td style="padding:6px 10px;font-size:11px;color:{SLATE};font-weight:600;">
            <a href="{seg_url}" style="color:{SLATE};text-decoration:none;">{name}</a><br>
            <span style="font-size:10px;color:#94a3b8;font-weight:400;">{avg_grade:.1f}% · {athletes_str} athletes</span>
          </td>
          <td style="padding:6px 10px;font-size:12px;font-weight:700;color:{rank_col};text-align:center;">{rank_str}</td>
          <td style="padding:6px 10px;font-size:11px;color:{SLATE};text-align:center;">{pr_str}</td>
          <td style="padding:6px 10px;font-size:11px;color:{SLATE};text-align:center;">{kom_str}</td>
          <td style="padding:6px 10px;font-size:12px;font-weight:700;color:{gap_color};text-align:center;">{gap_str}</td>
          <td style="padding:6px 10px;font-size:11px;font-weight:600;color:{pw_color};text-align:center;">{pw_str}</td>
          <td style="padding:6px 10px;font-size:11px;font-weight:700;color:{pct_color};text-align:center;">{pct_str}</td>
        </tr>"""

    # Tier 2 opportunity note
    tier2 = [s for s in segment_data if s.get("tier") == 2]
    tier2_html = ""
    if tier2:
        opp = tier2[0]
        t2_rank = opp.get("rank")
        t2_total = opp.get("total_athletes", "?")
        t2_pct = opp.get("pct_power_increase")
        t2_count = opp.get("effort_count", 0)
        rank_str2 = f"{t2_rank} of {t2_total}" if t2_rank else f"—/{t2_total}"
        t2_pct_str = f"+{t2_pct:.0f}% power needed" if t2_pct is not None else "no KOM data"
        tier2_html = f"""
        <div style="padding:10px 14px;background:#fefce8;border-top:1px solid {BORDER};">
          <span style="font-size:10px;font-weight:700;color:{AMBER};">💡 Opportunity: </span>
          <span style="font-size:11px;color:#475569;">
            You've ridden <strong>{opp.get('segment_name','?')}</strong> {t2_count}x
            — ranked {rank_str2}. {t2_pct_str}. Consider adding to targets.
          </span>
        </div>"""

    return f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:{SLATE};padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">KOM Targets</span>
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f1f5f9;">
            <th style="padding:5px 10px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Segment</th>
            <th style="padding:5px 10px;text-align:center;font-size:9px;color:#94a3b8;text-transform:uppercase;">Rank</th>
            <th style="padding:5px 10px;text-align:center;font-size:9px;color:#94a3b8;text-transform:uppercase;">Your PR</th>
            <th style="padding:5px 10px;text-align:center;font-size:9px;color:#94a3b8;text-transform:uppercase;">KOM</th>
            <th style="padding:5px 10px;text-align:center;font-size:9px;color:#94a3b8;text-transform:uppercase;">Gap</th>
            <th style="padding:5px 10px;text-align:center;font-size:9px;color:#94a3b8;text-transform:uppercase;">Δ Watts</th>
            <th style="padding:5px 10px;text-align:center;font-size:9px;color:#94a3b8;text-transform:uppercase;">+% Power</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      {tier2_html}
    </div>"""


def build_email(email_content, pmc_current, last_week_summary,
                session_comparison=None, hr_data=None,
                init_mode=False, block_overview=None,
                data_range_start=None, data_range_end=None,
                zone_ranges=None, ftp_outdoor=323, ftp_indoor=275,
                power_curve_data=None,
                strava_descriptions=None, checkin_data=None,
                segment_data=None,
                ef_data=None, interval_hr_data=None, rpe_signals=None,
                curve_trend=None, is_recovery_week=False):
    """
    Build full HTML email from Claude's email_content dict + PMC data.
    """
    ctl      = pmc_current.get("ctl", 0)
    atl      = pmc_current.get("atl", 0)
    tsb      = pmc_current.get("tsb", 0)
    week_tss = last_week_summary.get("tss", 0)
    week_km  = last_week_summary.get("distance_km", 0)

    today_str = date.today().strftime("%A, %B %-d, %Y")
    tsb_col   = _tsb_color(tsb)

    # Header stats bar
    stats_bar = f"""
    <div style="background:{SLATE};border-radius:8px;padding:16px 20px;
                margin-bottom:16px;text-align:center;">
      <p style="margin:0 0 12px;color:rgba(255,255,255,0.5);font-size:11px;
                text-transform:uppercase;letter-spacing:0.1em;">Performance Management</p>
      <div>
        {_stat_pill("CTL Fitness", f"{ctl:.0f}", TEAL)}
        {_stat_pill("ATL Fatigue", f"{atl:.0f}", AMBER)}
        {_stat_pill("TSB Form", f"{tsb:+.0f}", tsb_col)}
        {_stat_pill("Week TSS", f"{week_tss:.0f}", GREEN)}
        {_stat_pill("Week km", f"{week_km:.0f}", "#94a3b8")}
      </div>
    </div>"""

    # HR insight line for fitness snapshot
    hr_insight = ""
    if hr_data and hr_data.get("power_hr_ratio_last_week"):
        ratio_lw   = hr_data["power_hr_ratio_last_week"]
        ratio_4wk  = hr_data.get("power_hr_ratio_4wk_avg", "")
        trend      = hr_data.get("ratio_trend", "stable")
        trend_icon = {"improving": "↑", "declining": "↓", "stable": "→"}.get(trend, "→")
        trend_col  = {"improving": GREEN, "declining": RED, "stable": AMBER}.get(trend, AMBER)
        flag       = hr_data.get("hr_elevation_flag", False)
        flag_str   = ' <span style="color:#dc2626;font-weight:700;">⚠ HR elevated</span>' if flag else ""
        hr_insight = (f'<p style="margin:6px 0 0;font-size:12px;color:#64748b;">'
                      f'Power:HR ratio: <strong style="color:{trend_col};">'
                      f'{trend_icon} {ratio_lw}</strong> w/bpm '
                      f'(4wk avg {ratio_4wk}){flag_str}</p>')

    # Cards
    fitness  = email_content.get("fitness_snapshot", {})
    recap    = email_content.get("last_week_recap", {})
    progress = email_content.get("block_progress", {})
    note     = email_content.get("coaches_note", {})
    plan     = email_content.get("this_week_plan", {})

    fitness_body = fitness.get("body", "")
    fitness_card = f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:{TEAL};padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Fitness Snapshot</span>
      </div>
      <div style="padding:16px 20px;">
        <p style="margin:0 0 8px;font-size:16px;font-weight:700;color:{SLATE};">{fitness.get("headline", "")}</p>
        <p style="margin:0;font-size:14px;color:#475569;line-height:1.6;">{fitness_body}</p>
        {hr_insight}
      </div>
    </div>"""

    # Data range defaults
    if not data_range_start:
        from datetime import date as d, timedelta
        data_range_start = (d.today() - timedelta(weeks=8)).isoformat()
    if not data_range_end:
        data_range_end = date.today().isoformat()

    cards = (
        _zones_reference_table(zone_ranges, ftp_outdoor, ftp_indoor)
        + _rides_data_section(last_week_summary, data_range_start, data_range_end, ftp_outdoor, ftp_indoor,
                              strava_descriptions=strava_descriptions, checkin_data=checkin_data)
        + (_power_curve_card(power_curve_data, ftp_outdoor, curve_trend=curve_trend, is_recovery_week=is_recovery_week) if power_curve_data else "")
        + (_segment_card(segment_data) if segment_data else "")
        + (_ef_hr_card(ef_data, interval_hr_data, rpe_signals) if (ef_data or interval_hr_data or rpe_signals) else "")
        + fitness_card
        + ((_block_overview_card(block_overview)) if init_mode and block_overview else "")
        + _card("Last Week", recap.get("headline", ""), recap.get("body", ""), SLATE)
        + (_session_comparison_table(session_comparison, zone_ranges, ftp_outdoor, ftp_indoor)
           if session_comparison else "")
        + _week_table(plan, zone_ranges=zone_ranges, ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor)
        + _card("Block Progress", progress.get("headline", ""), progress.get("body", ""), GREEN)
        + _card("Coach's Note", note.get("headline", ""), note.get("body", ""), PURPLE)
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    @media (max-width: 600px) {{
      .week-table    {{ display: block !important; }}
      .week-row      {{ display: block !important; padding: 10px 12px !important;
                       border-bottom: 2px solid #e2e8f0 !important; }}
      .week-col-day  {{ display: inline !important; }}
      .week-col-type {{ display: inline !important; margin-left: 6px !important; }}
      .week-col-dur  {{ display: inline !important; margin-left: 6px !important; color: #64748b !important; }}
      .week-col-tgt  {{ display: block !important; margin: 4px 0 !important; }}
      .week-col-desc {{ display: block !important; margin-top: 4px !important; }}
      .week-thead    {{ display: none !important; }}
      .cmp-thead       {{ display: none !important; }}
      .cmp-row         {{ display: block !important; padding: 10px 12px !important;
                         border-bottom: 2px solid #e2e8f0 !important; }}
      .cmp-col-day     {{ display: inline !important; font-weight: 700 !important;
                         font-size: 12px !important; }}
      .cmp-col-status  {{ display: inline !important; margin-left: 8px !important; }}
      .cmp-col-planned {{ display: block !important; font-size: 11px !important;
                         color: #64748b !important; margin-top: 4px !important; }}
      .cmp-col-actual  {{ display: block !important; font-size: 11px !important;
                         margin-top: 3px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',sans-serif;">
  <div style="max-width:680px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="text-align:center;margin-bottom:20px;">
      <p style="margin:0;font-size:13px;color:#94a3b8;">{today_str}</p>
      <h1 style="margin:4px 0 0;font-size:22px;font-weight:800;color:{SLATE};">
        Weekly Coaching Report
      </h1>
    </div>

    {stats_bar}
    {cards}

    <!-- Footer -->
    <p style="text-align:center;font-size:11px;color:#cbd5e1;margin-top:20px;">
      Powered by Strava + Claude · FTP outdoor {ftp_outdoor}w · FTP indoor {ftp_indoor}w ·
      <a href="file:///Users/davidhey/code/cycling-coach/metrics_guide.md"
         style="color:#cbd5e1;">Metrics Guide</a>
    </p>
  </div>
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Block transition email
# ---------------------------------------------------------------------------

def build_transition_email(email_content, pmc_current, ctl_block_start,
                           debrief, fitness_assessment, new_plan,
                           zone_ranges=None, ftp_outdoor=323, ftp_indoor=275,
                           power_curve_data=None, curve_trend=None,
                           segment_data=None):
    """
    Build the block transition HTML email: block scorecard + next block overview.
    Separate from build_email() (the weekly email).
    """
    today_str = date.today().strftime("%A, %B %-d, %Y")
    ec        = email_content or {}

    ctl     = pmc_current.get("ctl", 0)
    atl     = pmc_current.get("atl", 0)
    tsb     = pmc_current.get("tsb", 0)
    ctl_str = f"{ctl_block_start:.0f}→{ctl:.0f}" if ctl_block_start else f"{ctl:.0f}"

    # Debrief values
    d = debrief or {}
    goal_label = d.get("goal_achieved_label", "—")
    motivation = d.get("motivation_label", "—")
    limiter    = d.get("biggest_limiter") or "None reported"

    # Fitness assessment
    fa       = fitness_assessment or {}
    scenario = fa.get("scenario", "C")
    scenario_colors = {"A": GREEN, "B": AMBER, "C": SLATE}
    scenario_color  = scenario_colors.get(scenario, SLATE)
    scenario_labels = {
        "A": "Scenario A — CP model (sufficient max-effort data)",
        "B": "Scenario B — Max effort test scheduled in week 1",
        "C": "Scenario C — Fitness test scheduled in week 1",
    }

    # Power curve deltas
    pc_rows = ""
    dur_info = [(60, "1-min"), (300, "5-min"), (600, "10-min"), (1200, "20-min")]
    if curve_trend:
        for dur, lbl in dur_info:
            dur_d  = curve_trend.get(dur, {})
            r, p   = dur_d.get("recent"), dur_d.get("prior")
            if r and p:
                delta   = round((r - p) / p * 100, 1)
                sign    = "+" if delta >= 0 else ""
                d_color = GREEN if delta > 0 else (RED if delta < -1 else "#64748b")
                pc_rows += f"""
            <tr style="border-bottom:1px solid {BORDER};">
              <td style="padding:5px 10px;font-size:11px;color:#64748b;">{lbl}</td>
              <td style="padding:5px 10px;font-size:11px;color:{SLATE};">{p}w</td>
              <td style="padding:5px 10px;font-size:11px;font-weight:700;color:{TEAL};">{r}w</td>
              <td style="padding:5px 10px;font-size:11px;font-weight:700;color:{d_color};">{sign}{delta}%</td>
            </tr>"""

    power_curve_section = ""
    if pc_rows:
        power_curve_section = f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};margin-bottom:16px;overflow:hidden;">
      <div style="background:#475569;padding:8px 20px;">
        <span style="color:rgba(255,255,255,0.75);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Power Curve — Block Delta</span>
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <thead><tr style="background:#f8fafc;">
          <th style="padding:4px 10px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Duration</th>
          <th style="padding:4px 10px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Block Start</th>
          <th style="padding:4px 10px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Now</th>
          <th style="padding:4px 10px;text-align:left;font-size:9px;color:#94a3b8;text-transform:uppercase;">Change</th>
        </tr></thead>
        <tbody>{pc_rows}</tbody>
      </table>
    </div>"""

    # Week 1 sessions
    week1_sessions_html = ""
    try:
        week1 = new_plan["weeks"][0]
        week1_plan = {s["day"]: s for s in week1.get("sessions", [])}
        week1_sessions_html = _week_table(
            week1_plan, zone_ranges=zone_ranges,
            ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
        )
    except Exception:
        pass

    # Stats bar
    stats_bar = f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                padding:16px;margin-bottom:16px;text-align:center;">
      {_stat_pill("CTL (block)", ctl_str, TEAL)}
      {_stat_pill("ATL", f"{atl:.0f}", AMBER)}
      {_stat_pill("TSB", f"{tsb:+.0f}", _tsb_color(tsb))}
      {_stat_pill("FTP", f"{ftp_outdoor}w", scenario_color)}
      {_stat_pill("Scenario", scenario, scenario_color)}
    </div>"""

    # Main cards
    cards = ""
    cards += _card(
        "Block Scorecard",
        ec.get("block_scorecard", {}).get("headline", "Block complete"),
        ec.get("block_scorecard", {}).get("body", ""),
        accent=SLATE,
    )
    cards += f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;padding:14px 20px;">
      <div style="font-size:12px;color:#64748b;margin-bottom:4px;">Athlete Debrief</div>
      <div style="font-size:13px;color:{SLATE};line-height:1.7;">
        <b>Goal achieved:</b> {goal_label}<br>
        <b>Motivation:</b> {motivation}<br>
        <b>Biggest limiter:</b> {limiter}
      </div>
    </div>"""
    cards += _card(
        "Fitness Assessment",
        ec.get("fitness_assessment", {}).get("headline",
            scenario_labels.get(scenario, f"Scenario {scenario}")),
        ec.get("fitness_assessment", {}).get("body",
            fa.get("evidence_summary", "")),
        accent=scenario_color,
    )
    cards += power_curve_section
    if ec.get("power_curve_summary"):
        cards += _card(
            "Power Curve Summary",
            ec["power_curve_summary"].get("headline", "Power curve"),
            ec["power_curve_summary"].get("body", ""),
            accent=TEAL,
        )
    cards += _card(
        "Next Block",
        ec.get("next_block_overview", {}).get("headline", "Next block"),
        ec.get("next_block_overview", {}).get("body", ""),
        accent=PURPLE,
    )
    if week1_sessions_html:
        cards += f"""
    <div style="background:#ffffff;border-radius:8px;border:1px solid {BORDER};
                margin-bottom:16px;overflow:hidden;">
      <div style="background:{PURPLE};padding:10px 20px;">
        <span style="color:rgba(255,255,255,0.85);font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em;">Week 1 Sessions</span>
      </div>
      {week1_sessions_html}
    </div>"""
    cards += _card(
        "Coach's Verdict",
        ec.get("coaches_verdict", {}).get("headline", "Verdict"),
        ec.get("coaches_verdict", {}).get("body", ""),
        accent=SLATE,
    )

    # Zones reference
    zones_ref = _zones_reference_table(zone_ranges, ftp_outdoor, ftp_indoor)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',sans-serif;">
  <div style="max-width:680px;margin:0 auto;padding:24px 16px;">
    <div style="text-align:center;margin-bottom:20px;">
      <p style="margin:0;font-size:13px;color:#94a3b8;">{today_str}</p>
      <h1 style="margin:4px 0 0;font-size:22px;font-weight:800;color:{SLATE};">
        Block Transition Report
      </h1>
      <p style="margin:4px 0 0;font-size:13px;color:#64748b;">
        Block complete — new 5-week plan generated
      </p>
    </div>

    {stats_bar}
    {zones_ref}
    {cards}

    <p style="text-align:center;font-size:11px;color:#cbd5e1;margin-top:20px;">
      Powered by Strava + Claude · FTP outdoor {ftp_outdoor}w · FTP indoor {ftp_indoor}w
    </p>
  </div>
</body>
</html>"""

    return html
