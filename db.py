"""
db.py — DuckDB persistent store for cycling coach metrics.

Tables:
  pmc_daily           — daily CTL/ATL/TSB (rolling 14-day inserts)
  session_compliance  — planned vs actual per day per week
  power_curve_weekly  — best efforts at 1/5/10/20-min per week
  segment_efforts     — tracked segment efforts with rank + power metric
  segment_history     — full historical segment effort log (backfilled)
  activity_metrics    — per-ride EF, decoupling, HR, inferred RPE
  interval_hr_stats   — per-interval HR response metrics
  coaching_log        — weekly plan snapshots, email content, and block history
"""

import json
import os
import duckdb
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cycling_coach.duckdb")

_TABLES = [
    """CREATE TABLE IF NOT EXISTS pmc_daily (
        date DATE PRIMARY KEY,
        tss FLOAT,
        ctl FLOAT,
        atl FLOAT,
        tsb FLOAT
    )""",
    """CREATE TABLE IF NOT EXISTS session_compliance (
        week_start DATE,
        day VARCHAR,
        session_type VARCHAR,
        planned_duration_min INT,
        planned_tss FLOAT,
        actual_duration_min INT,
        actual_tss FLOAT,
        actual_avg_watts INT,
        efforts_detected INT,
        interval_avg_watts INT,
        interval_target_watts INT,
        status VARCHAR,
        PRIMARY KEY (week_start, day)
    )""",
    """CREATE TABLE IF NOT EXISTS power_curve_weekly (
        week_end DATE PRIMARY KEY,
        s60 INT,
        s300 INT,
        s600 INT,
        s1200 INT
    )""",
    """CREATE TABLE IF NOT EXISTS segment_efforts (
        date DATE,
        segment_id BIGINT,
        segment_name VARCHAR,
        avg_grade FLOAT,
        elapsed_time_s INT,
        avg_watts INT,
        rank INT,
        total_athletes INT,
        tier INT,
        pct_power_increase FLOAT,
        power_tier VARCHAR,
        PRIMARY KEY (date, segment_id)
    )""",
    """CREATE TABLE IF NOT EXISTS segment_history (
        activity_id    BIGINT,
        activity_date  DATE,
        segment_id     BIGINT,
        segment_name   VARCHAR,
        elapsed_time_s INT,
        avg_watts      INT,
        avg_grade      FLOAT,
        distance_m     FLOAT,
        PRIMARY KEY (activity_id, segment_id)
    )""",
    """CREATE TABLE IF NOT EXISTS activity_metrics (
        activity_id    BIGINT PRIMARY KEY,
        activity_date  DATE,
        activity_type  VARCHAR,
        ef             FLOAT,
        decoupling     FLOAT,
        avg_hr         INT,
        max_hr         INT,
        avg_watts      INT,
        np             INT,
        inferred_rpe   INT,
        rpe_sentiment  VARCHAR,
        rpe_keywords   VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS interval_hr_stats (
        activity_id    BIGINT,
        activity_date  DATE,
        interval_num   INT,
        set_label      VARCHAR,
        start_s        INT,
        duration_s     INT,
        avg_watts      INT,
        target_watts   INT,
        peak_watts     INT,
        peak_hr        INT,
        hr_at_60s      INT,
        ramp_rate      FLOAT,
        recovery_60s   INT,
        recovery_120s  INT,
        hr_plateau_pct FLOAT,
        PRIMARY KEY (activity_id, interval_num)
    )""",
    """CREATE TABLE IF NOT EXISTS coaching_log (
        week_start           DATE PRIMARY KEY,
        plan_snapshot_before VARCHAR,
        plan_snapshot_after  VARCHAR,
        email_json           VARCHAR,
        coaches_note         VARCHAR,
        tss_planned          FLOAT,
        tss_actual           FLOAT,
        tsb_at_week_start    FLOAT,
        created_at           TIMESTAMP DEFAULT current_timestamp
    )""",
    """CREATE TABLE IF NOT EXISTS blocks (
        block_id        VARCHAR PRIMARY KEY,
        start_date      DATE NOT NULL,
        end_date        DATE,
        block_type      VARCHAR,
        phase           VARCHAR,
        goal            TEXT,
        ftp_start       INT,
        ftp_end         INT,
        ftp_method      VARCHAR,
        ctl_start       REAL,
        ctl_end         REAL,
        tsb_start       REAL,
        tsb_end         REAL,
        planned_tss     INT,
        actual_tss      INT,
        compliance_pct  REAL,
        p60s_start      INT,
        p60s_end        INT,
        p300s_start     INT,
        p300s_end       INT,
        p1200s_start    INT,
        p1200s_end      INT,
        goal_achieved   VARCHAR,
        biggest_limiter TEXT,
        motivation      INT,
        injuries        TEXT,
        disruptions     TEXT,
        created_at      TIMESTAMP DEFAULT current_timestamp
    )""",
]


def get_conn():
    """Return a duckdb connection, creating tables on first use."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = duckdb.connect(DB_PATH)
    for ddl in _TABLES:
        conn.execute(ddl)
    # Migrate segment_efforts: replace achievability with power metric columns
    for stmt in [
        "ALTER TABLE segment_efforts ADD COLUMN pct_power_increase FLOAT",
        "ALTER TABLE segment_efforts ADD COLUMN power_tier VARCHAR",
    ]:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE segment_efforts DROP COLUMN achievability")
    except Exception:
        pass
    # Migrate interval_hr_stats: add power + boundary columns
    for stmt in [
        "ALTER TABLE interval_hr_stats ADD COLUMN activity_date DATE",
        "ALTER TABLE interval_hr_stats ADD COLUMN set_label VARCHAR",
        "ALTER TABLE interval_hr_stats ADD COLUMN start_s INT",
        "ALTER TABLE interval_hr_stats ADD COLUMN duration_s INT",
        "ALTER TABLE interval_hr_stats ADD COLUMN target_watts INT",
        "ALTER TABLE interval_hr_stats ADD COLUMN peak_watts INT",
    ]:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    return conn


def append_pmc_rows(pmc_series):
    """INSERT OR REPLACE last 14 days of PMC into pmc_daily."""
    if not pmc_series:
        return
    conn = get_conn()
    for row in pmc_series[-14:]:
        conn.execute("""
            INSERT INTO pmc_daily (date, tss, ctl, atl, tsb)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (date) DO UPDATE SET
                tss = EXCLUDED.tss,
                ctl = EXCLUDED.ctl,
                atl = EXCLUDED.atl,
                tsb = EXCLUDED.tsb
        """, [row["date"], row["tss"], row["ctl"], row["atl"], row["tsb"]])
    conn.close()


def append_session_compliance(session_comparison, week_start):
    """INSERT OR REPLACE 7 rows for the given week into session_compliance."""
    if not session_comparison:
        return
    conn = get_conn()
    for day in session_comparison:
        p = day.get("planned") or {}
        a = day.get("actual")
        istats = (a or {}).get("interval_stats") or {}
        conn.execute("""
            INSERT INTO session_compliance
            (week_start, day, session_type, planned_duration_min, planned_tss,
             actual_duration_min, actual_tss, actual_avg_watts,
             efforts_detected, interval_avg_watts, interval_target_watts, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (week_start, day) DO UPDATE SET
                session_type = EXCLUDED.session_type,
                planned_duration_min = EXCLUDED.planned_duration_min,
                planned_tss = EXCLUDED.planned_tss,
                actual_duration_min = EXCLUDED.actual_duration_min,
                actual_tss = EXCLUDED.actual_tss,
                actual_avg_watts = EXCLUDED.actual_avg_watts,
                efforts_detected = EXCLUDED.efforts_detected,
                interval_avg_watts = EXCLUDED.interval_avg_watts,
                interval_target_watts = EXCLUDED.interval_target_watts,
                status = EXCLUDED.status
        """, [
            week_start,
            day.get("day"),
            p.get("type"),
            p.get("duration_min"),
            p.get("target_tss"),
            (a or {}).get("duration_min"),
            (a or {}).get("tss"),
            (a or {}).get("avg_watts"),
            istats.get("efforts_detected"),
            istats.get("avg_interval_watts"),
            istats.get("target_watts"),
            day.get("status"),
        ])
    conn.close()


def append_power_curve(curve_data, week_end):
    """INSERT OR REPLACE one row into power_curve_weekly."""
    if not curve_data:
        return
    conn = get_conn()
    conn.execute("""
        INSERT INTO power_curve_weekly (week_end, s60, s300, s600, s1200)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (week_end) DO UPDATE SET
            s60 = EXCLUDED.s60,
            s300 = EXCLUDED.s300,
            s600 = EXCLUDED.s600,
            s1200 = EXCLUDED.s1200
    """, [
        week_end,
        (curve_data.get(60) or {}).get("best"),
        (curve_data.get(300) or {}).get("best"),
        (curve_data.get(600) or {}).get("best"),
        (curve_data.get(1200) or {}).get("best"),
    ])
    conn.close()


def append_segment_efforts(segment_list):
    """INSERT OR REPLACE segment effort rows (keyed by today's date + segment_id)."""
    if not segment_list:
        return
    conn = get_conn()
    today = date.today()
    for seg in segment_list:
        conn.execute("""
            INSERT INTO segment_efforts
            (date, segment_id, segment_name, avg_grade, elapsed_time_s,
             avg_watts, rank, total_athletes, tier, pct_power_increase, power_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (date, segment_id) DO UPDATE SET
                segment_name = EXCLUDED.segment_name,
                avg_grade = EXCLUDED.avg_grade,
                elapsed_time_s = EXCLUDED.elapsed_time_s,
                avg_watts = EXCLUDED.avg_watts,
                rank = EXCLUDED.rank,
                total_athletes = EXCLUDED.total_athletes,
                tier = EXCLUDED.tier,
                pct_power_increase = EXCLUDED.pct_power_increase,
                power_tier = EXCLUDED.power_tier
        """, [
            today,
            seg.get("segment_id"),
            seg.get("segment_name"),
            seg.get("avg_grade"),
            seg.get("elapsed_time_s"),
            seg.get("avg_watts"),
            seg.get("rank"),
            seg.get("total_athletes"),
            seg.get("tier"),
            seg.get("pct_power_increase"),
            seg.get("power_tier"),
        ])
    conn.close()


def write_segment_history(efforts):
    """Upsert list of segment effort dicts into segment_history."""
    if not efforts:
        return
    conn = get_conn()
    for e in efforts:
        conn.execute("""
            INSERT INTO segment_history
            (activity_id, activity_date, segment_id, segment_name,
             elapsed_time_s, avg_watts, avg_grade, distance_m)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (activity_id, segment_id) DO UPDATE SET
                activity_date  = EXCLUDED.activity_date,
                segment_name   = EXCLUDED.segment_name,
                elapsed_time_s = EXCLUDED.elapsed_time_s,
                avg_watts      = EXCLUDED.avg_watts,
                avg_grade      = EXCLUDED.avg_grade,
                distance_m     = EXCLUDED.distance_m
        """, [
            e.get("activity_id"),
            e.get("activity_date"),
            e.get("segment_id"),
            e.get("segment_name"),
            e.get("elapsed_time_s"),
            e.get("avg_watts"),
            e.get("avg_grade"),
            e.get("distance_m"),
        ])
    conn.close()


def query_segment_history(min_grade=5.0, min_time_s=60, max_time_s=600):
    """
    Return aggregated segment stats from segment_history.
    Returns list of row dicts, or [] if table is empty.
    """
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT
                segment_id,
                segment_name,
                avg_grade,
                distance_m,
                COUNT(*) AS effort_count,
                MIN(elapsed_time_s) AS best_elapsed_time_s,
                MAX(activity_date) AS last_date,
                arg_min(avg_watts, elapsed_time_s) AS best_avg_watts
            FROM segment_history
            WHERE avg_grade >= ?
              AND elapsed_time_s BETWEEN ? AND ?
            GROUP BY segment_id, segment_name, avg_grade, distance_m
        """, [min_grade, min_time_s, max_time_s]).fetchall()
    except Exception:
        conn.close()
        return []
    conn.close()

    cols = ["segment_id", "segment_name", "avg_grade", "distance_m",
            "effort_count", "best_elapsed_time_s", "last_date", "best_avg_watts"]
    return [dict(zip(cols, row)) for row in rows]


def upsert_activity_metrics(metrics_list):
    """Upsert activity_metrics rows. metrics_list is a list of dicts."""
    if not metrics_list:
        return
    conn = get_conn()
    for m in metrics_list:
        conn.execute("""
            INSERT INTO activity_metrics
            (activity_id, activity_date, activity_type, ef, decoupling,
             avg_hr, max_hr, avg_watts, np, inferred_rpe, rpe_sentiment, rpe_keywords)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (activity_id) DO UPDATE SET
                activity_date = EXCLUDED.activity_date,
                activity_type = EXCLUDED.activity_type,
                ef            = EXCLUDED.ef,
                decoupling    = EXCLUDED.decoupling,
                avg_hr        = EXCLUDED.avg_hr,
                max_hr        = EXCLUDED.max_hr,
                avg_watts     = EXCLUDED.avg_watts,
                np            = EXCLUDED.np,
                inferred_rpe  = COALESCE(EXCLUDED.inferred_rpe, activity_metrics.inferred_rpe),
                rpe_sentiment = COALESCE(EXCLUDED.rpe_sentiment, activity_metrics.rpe_sentiment),
                rpe_keywords  = COALESCE(EXCLUDED.rpe_keywords, activity_metrics.rpe_keywords)
        """, [
            m.get("activity_id"), m.get("activity_date"), m.get("activity_type"),
            m.get("ef"), m.get("decoupling"),
            m.get("avg_hr"), m.get("max_hr"), m.get("avg_watts"), m.get("np"),
            m.get("inferred_rpe"), m.get("rpe_sentiment"), m.get("rpe_keywords"),
        ])
    conn.close()


def upsert_rpe(activity_id, inferred_rpe, rpe_sentiment, rpe_keywords):
    """Update only RPE fields for an existing activity_metrics row."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO activity_metrics (activity_id, inferred_rpe, rpe_sentiment, rpe_keywords)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (activity_id) DO UPDATE SET
            inferred_rpe  = EXCLUDED.inferred_rpe,
            rpe_sentiment = EXCLUDED.rpe_sentiment,
            rpe_keywords  = EXCLUDED.rpe_keywords
    """, [activity_id, inferred_rpe, rpe_sentiment, rpe_keywords])
    conn.close()


def upsert_interval_hr_stats(stats_list):
    """Upsert interval_hr_stats rows — power + HR + boundary data."""
    if not stats_list:
        return
    conn = get_conn()
    for s in stats_list:
        conn.execute("""
            INSERT INTO interval_hr_stats
            (activity_id, activity_date, interval_num, set_label,
             start_s, duration_s, avg_watts, target_watts, peak_watts,
             peak_hr, hr_at_60s, ramp_rate, recovery_60s, recovery_120s, hr_plateau_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (activity_id, interval_num) DO UPDATE SET
                activity_date  = EXCLUDED.activity_date,
                set_label      = EXCLUDED.set_label,
                start_s        = EXCLUDED.start_s,
                duration_s     = EXCLUDED.duration_s,
                avg_watts      = EXCLUDED.avg_watts,
                target_watts   = EXCLUDED.target_watts,
                peak_watts     = EXCLUDED.peak_watts,
                peak_hr        = COALESCE(EXCLUDED.peak_hr,        interval_hr_stats.peak_hr),
                hr_at_60s      = COALESCE(EXCLUDED.hr_at_60s,      interval_hr_stats.hr_at_60s),
                ramp_rate      = COALESCE(EXCLUDED.ramp_rate,       interval_hr_stats.ramp_rate),
                recovery_60s   = COALESCE(EXCLUDED.recovery_60s,    interval_hr_stats.recovery_60s),
                recovery_120s  = COALESCE(EXCLUDED.recovery_120s,   interval_hr_stats.recovery_120s),
                hr_plateau_pct = COALESCE(EXCLUDED.hr_plateau_pct,  interval_hr_stats.hr_plateau_pct)
        """, [
            s.get("activity_id"), s.get("activity_date"), s.get("interval_num"),
            s.get("set_label"), s.get("start_s"), s.get("duration_s"),
            s.get("avg_watts"), s.get("target_watts"), s.get("peak_watts"),
            s.get("peak_hr"), s.get("hr_at_60s"), s.get("ramp_rate"),
            s.get("recovery_60s"), s.get("recovery_120s"), s.get("hr_plateau_pct"),
        ])
    conn.close()


def ef_trend(weeks=8):
    """
    Return 4-week rolling EF peak vs prior 4-week rolling EF peak.
    Also returns average decoupling for Z2 rides.
    Returns dict or None if insufficient data.
    """
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT
                AVG(CASE WHEN activity_date >= current_date - 28
                          AND ef IS NOT NULL THEN ef END) AS ef_recent,
                AVG(CASE WHEN activity_date BETWEEN current_date - 56 AND current_date - 29
                          AND ef IS NOT NULL THEN ef END) AS ef_prior,
                AVG(CASE WHEN activity_date >= current_date - 28
                          AND decoupling IS NOT NULL THEN decoupling END) AS avg_decoupling,
                COUNT(CASE WHEN activity_date >= current_date - 28
                            AND ef IS NOT NULL THEN 1 END) AS recent_count
            FROM activity_metrics
        """).fetchone()
    except Exception:
        conn.close()
        return None
    conn.close()

    if not row or not row[0]:
        return None

    ef_recent, ef_prior, avg_decoupling, recent_count = row
    ef_trend_pct = None
    if ef_recent and ef_prior and ef_prior > 0:
        ef_trend_pct = round((ef_recent - ef_prior) / ef_prior * 100, 1)

    return {
        "ef_recent":      round(ef_recent, 3) if ef_recent else None,
        "ef_prior":       round(ef_prior, 3) if ef_prior else None,
        "ef_trend_pct":   ef_trend_pct,
        "avg_decoupling": round(avg_decoupling, 1) if avg_decoupling else None,
        "recent_count":   recent_count,
    }


def interval_hr_trend(weeks=8):
    """
    Return avg HR recovery (60s drop) and peak HR plateau % over recent vs prior 4 weeks.
    Returns dict or None if insufficient data.
    """
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT
                AVG(CASE WHEN am.activity_date >= current_date - 28
                          AND ih.recovery_60s IS NOT NULL THEN ih.recovery_60s END) AS rec_recent,
                AVG(CASE WHEN am.activity_date BETWEEN current_date - 56 AND current_date - 29
                          AND ih.recovery_60s IS NOT NULL THEN ih.recovery_60s END) AS rec_prior,
                AVG(CASE WHEN am.activity_date >= current_date - 28
                          AND ih.hr_plateau_pct IS NOT NULL THEN ih.hr_plateau_pct END) AS plateau_recent
            FROM interval_hr_stats ih
            JOIN activity_metrics am ON am.activity_id = ih.activity_id
        """).fetchone()
    except Exception:
        conn.close()
        return None
    conn.close()

    if not rows or rows[0] is None:
        return None

    rec_recent, rec_prior, plateau = rows
    rec_delta = None
    if rec_recent and rec_prior:
        rec_delta = round(rec_recent - rec_prior, 1)

    return {
        "recovery_60s_recent": round(rec_recent, 1) if rec_recent else None,
        "recovery_60s_prior":  round(rec_prior, 1) if rec_prior else None,
        "recovery_delta":      rec_delta,
        "hr_plateau_pct":      round(plateau, 1) if plateau else None,
    }


def power_curve_trend():
    """
    Return 4-week rolling peak vs prior 4-week rolling peak for power curve durations.
    Returns dict {60: {recent, prior, delta}, ...} or empty if insufficient data.
    """
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT
                MAX(CASE WHEN week_end >= current_date - 28 THEN s60 END)   AS s60_r,
                MAX(CASE WHEN week_end BETWEEN current_date - 56
                              AND current_date - 29 THEN s60 END)           AS s60_p,
                MAX(CASE WHEN week_end >= current_date - 28 THEN s300 END)  AS s300_r,
                MAX(CASE WHEN week_end BETWEEN current_date - 56
                              AND current_date - 29 THEN s300 END)          AS s300_p,
                MAX(CASE WHEN week_end >= current_date - 28 THEN s600 END)  AS s600_r,
                MAX(CASE WHEN week_end BETWEEN current_date - 56
                              AND current_date - 29 THEN s600 END)          AS s600_p,
                MAX(CASE WHEN week_end >= current_date - 28 THEN s1200 END) AS s1200_r,
                MAX(CASE WHEN week_end BETWEEN current_date - 56
                              AND current_date - 29 THEN s1200 END)         AS s1200_p,
                COUNT(CASE WHEN week_end >= current_date - 28 THEN 1 END)   AS recent_weeks
            FROM power_curve_weekly
        """).fetchone()
    except Exception:
        conn.close()
        return {}
    conn.close()

    if not row or row[8] is None or row[8] < 2:
        return {}

    s60_r, s60_p, s300_r, s300_p, s600_r, s600_p, s1200_r, s1200_p, _ = row
    result = {}
    for dur, recent, prior in [(60, s60_r, s60_p), (300, s300_r, s300_p),
                                (600, s600_r, s600_p), (1200, s1200_r, s1200_p)]:
        delta = (recent - prior) if (recent and prior) else None
        result[dur] = {"recent": recent, "prior": prior, "delta": delta}
    return result


def compliance_trends(weeks=4):
    """
    Query session_compliance for the past N weeks.
    Returns formatted string for Claude context. Skips if < 2 weeks of data.
    """
    cutoff = date.today() - timedelta(weeks=weeks)
    conn = get_conn()
    try:
        week_count = conn.execute("""
            SELECT COUNT(DISTINCT week_start)
            FROM session_compliance
            WHERE week_start >= ?
              AND session_type IS NOT NULL
              AND session_type != 'rest'
        """, [cutoff]).fetchone()[0]

        if week_count < 2:
            conn.close()
            return "  Less than 2 weeks of data — trends not yet available."

        rows = conn.execute("""
            SELECT
                session_type,
                COUNT(*) AS sessions,
                AVG(CASE WHEN status IN ('hit','done','partial') THEN 1.0 ELSE 0.0 END) AS completion_rate,
                AVG(CASE WHEN actual_avg_watts IS NOT NULL
                         AND interval_target_watts IS NOT NULL
                         AND interval_target_watts > 0
                         THEN actual_avg_watts - interval_target_watts
                         ELSE NULL END) AS avg_watts_delta,
                SUM(CASE WHEN status = 'missed' THEN 1 ELSE 0 END) AS missed_count,
                SUM(CASE WHEN status = 'hit' THEN 1 ELSE 0 END) AS hit_count
            FROM session_compliance
            WHERE week_start >= ?
              AND session_type IS NOT NULL
              AND session_type != 'rest'
            GROUP BY session_type
            ORDER BY session_type
        """, [cutoff]).fetchall()
    except Exception as e:
        conn.close()
        return f"  Compliance query failed: {e}"
    conn.close()

    if not rows:
        return "  No multi-week compliance data yet."

    lines = []
    for row in rows:
        session_type, sessions, completion_rate, watts_delta, missed, hits = row
        pct = round(completion_rate * 100)
        delta_str = ""
        if watts_delta is not None:
            sign = "+" if watts_delta >= 0 else ""
            delta_str = f" | avg watts vs target: {sign}{round(watts_delta)}w"
        lines.append(
            f"  {session_type.upper()}: {pct}% completion "
            f"({hits} hit / {missed} missed of {sessions} sessions){delta_str}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coaching log — plan snapshots, email archive, block history
# ---------------------------------------------------------------------------

def _diff_plan_snapshots(before_json, after_json):
    """
    Compare two plan JSON strings. Returns list of human-readable change strings.
    Only compares session type and target_watts per (week_number, day) for future weeks.
    Skips week 1 (the just-completed week). Returns [] on any parse error.
    """
    try:
        before = json.loads(before_json)
        after  = json.loads(after_json)
    except Exception:
        return []

    before_weeks = {w["week_number"]: w for w in before.get("weeks", [])}
    after_weeks  = {w["week_number"]: w for w in after.get("weeks", [])}

    # Skip week 1 — it just completed; only diff future weeks
    week_numbers = sorted(set(before_weeks) & set(after_weeks))[1:]

    changes = []
    for wn in week_numbers:
        before_sessions = {s["day"]: s for s in before_weeks[wn].get("sessions", [])}
        after_sessions  = {s["day"]: s for s in after_weeks[wn].get("sessions", [])}

        for day, bs in before_sessions.items():
            as_ = after_sessions.get(day)
            if not as_:
                continue
            b_type, a_type = bs.get("type"), as_.get("type")
            b_watts, a_watts = bs.get("target_watts"), as_.get("target_watts")

            if b_type != a_type:
                watt_note = f" ({b_watts}w → {a_watts}w)" if b_watts and a_watts else ""
                changes.append(f"Wk{wn} {day}: {b_type} → {a_type}{watt_note}")
            elif b_watts and a_watts and abs(b_watts - a_watts) >= 10:
                direction = "↓" if a_watts < b_watts else "↑"
                changes.append(f"Wk{wn} {day}: {b_type} {b_watts}w {direction} {a_watts}w")

    return changes


def insert_coaching_log(week_start, plan_before, plan_after, email_json,
                        tss_planned, tss_actual, tsb_at_week_start):
    """Upsert one row into coaching_log for the given week."""
    coaches_note = None
    if email_json and isinstance(email_json, dict):
        coaches_note = (email_json.get("coaches_note") or {}).get("body")

    conn = get_conn()
    conn.execute("""
        INSERT INTO coaching_log
        (week_start, plan_snapshot_before, plan_snapshot_after,
         email_json, coaches_note, tss_planned, tss_actual, tsb_at_week_start)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (week_start) DO UPDATE SET
            plan_snapshot_before = EXCLUDED.plan_snapshot_before,
            plan_snapshot_after  = EXCLUDED.plan_snapshot_after,
            email_json           = EXCLUDED.email_json,
            coaches_note         = EXCLUDED.coaches_note,
            tss_planned          = EXCLUDED.tss_planned,
            tss_actual           = EXCLUDED.tss_actual,
            tsb_at_week_start    = EXCLUDED.tsb_at_week_start
    """, [
        week_start,
        json.dumps(plan_before, default=str) if plan_before else None,
        json.dumps(plan_after, default=str) if plan_after else None,
        json.dumps(email_json, default=str) if email_json else None,
        coaches_note,
        tss_planned,
        tss_actual,
        tsb_at_week_start,
    ])
    conn.close()


def block_history_summary(blocks=2):
    """
    Return a formatted string summarising the last N blocks worth of coaching log data
    for injection into generate_block() prompts. Covers block-level progression —
    what the block goal was, TSS compliance trends, recurring coach notes, and what
    plan adjustments were made. Distinct from compliance_trends() which covers
    session-level weekly patterns.

    Returns "" if fewer than 2 rows (not enough history yet).
    """
    max_weeks = min(blocks * 5, 8)
    cutoff = date.today() - timedelta(weeks=max_weeks)

    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT week_start, plan_snapshot_before, plan_snapshot_after,
                   coaches_note, tss_planned, tss_actual, tsb_at_week_start
            FROM coaching_log
            WHERE week_start >= ?
            ORDER BY week_start ASC
        """, [cutoff]).fetchall()
    except Exception:
        conn.close()
        return ""
    conn.close()

    if len(rows) < 2:
        return ""

    # Extract block overview goal from the oldest row's plan_snapshot_after
    block_goal = None
    try:
        oldest_plan = json.loads(rows[0][2])
        block_goal = (oldest_plan.get("block_overview") or {}).get("goal")
    except Exception:
        pass

    lines = ["## PRIOR BLOCK HISTORY"]
    if block_goal:
        lines.append(f"  Block goal: {block_goal}")
    lines.append("")

    diff_total = 0
    for row in rows:
        (week_start, snap_before, snap_after,
         coaches_note, tss_planned, tss_actual, tsb) = row

        tss_pct = round(tss_actual / tss_planned * 100) if tss_planned else None
        tss_str = (f"TSS {tss_actual:.0f}/{tss_planned:.0f} ({tss_pct}%)"
                   if tss_pct else f"TSS actual={tss_actual:.0f}")

        lines.append(f"  Week of {week_start} (TSB={tsb:.1f}): {tss_str}")

        if coaches_note:
            lines.append(f"    Coach note: {coaches_note}")

        if snap_before and snap_after and diff_total < 8:
            diffs = _diff_plan_snapshots(snap_before, snap_after)
            for d in diffs[:3]:
                lines.append(f"    Adjustment: {d}")
                diff_total += 1
                if diff_total >= 8:
                    break

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Blocks — mesocycle-level tracking across seasons
# ---------------------------------------------------------------------------

def open_block(start_date, block_type, phase, goal, ftp, ctl, tsb, power_curve=None):
    """Insert a new block row (end_date=NULL = active block)."""
    pc = power_curve or {}
    conn = get_conn()
    conn.execute("""
        INSERT INTO blocks
        (block_id, start_date, block_type, phase, goal,
         ftp_start, ctl_start, tsb_start,
         p60s_start, p300s_start, p1200s_start)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (block_id) DO NOTHING
    """, [
        str(start_date), start_date, block_type, phase, goal,
        ftp, ctl, tsb,
        (pc.get(60) or {}).get("best") or pc.get("s60"),
        (pc.get(300) or {}).get("best") or pc.get("s300"),
        (pc.get(1200) or {}).get("best") or pc.get("s1200"),
    ])
    conn.close()
    print(f"DB: block opened ({start_date}, {block_type}, {phase})")


def close_block(start_date, end_date, ftp_end, ftp_method,
                ctl_end, tsb_end, actual_tss, compliance_pct,
                power_curve_end=None, debrief=None):
    """Update an existing block row with end-of-block metrics and debrief data."""
    pc = power_curve_end or {}
    d = debrief or {}
    conn = get_conn()
    conn.execute("""
        UPDATE blocks SET
            end_date        = ?,
            ftp_end         = ?,
            ftp_method      = ?,
            ctl_end         = ?,
            tsb_end         = ?,
            actual_tss      = ?,
            compliance_pct  = ?,
            p60s_end        = ?,
            p300s_end       = ?,
            p1200s_end      = ?,
            goal_achieved   = ?,
            biggest_limiter = ?,
            motivation      = ?,
            injuries        = ?,
            disruptions     = ?
        WHERE block_id = ?
    """, [
        end_date, ftp_end, ftp_method, ctl_end, tsb_end,
        actual_tss, compliance_pct,
        (pc.get(60) or {}).get("best") or pc.get("s60"),
        (pc.get(300) or {}).get("best") or pc.get("s300"),
        (pc.get(1200) or {}).get("best") or pc.get("s1200"),
        d.get("goal_achieved"),
        d.get("biggest_limiter"),
        d.get("motivation"),
        d.get("injuries"),
        d.get("disruptions"),
        str(start_date),
    ])
    conn.close()
    print(f"DB: block closed ({start_date} → {end_date})")


def current_block():
    """Return the active block row (end_date IS NULL), or None."""
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT block_id, start_date, block_type, phase, goal,
                   ftp_start, ctl_start, tsb_start,
                   p60s_start, p300s_start, p1200s_start
            FROM blocks WHERE end_date IS NULL
            ORDER BY start_date DESC LIMIT 1
        """).fetchone()
    except Exception:
        conn.close()
        return None
    conn.close()
    if not row:
        return None
    cols = ["block_id", "start_date", "block_type", "phase", "goal",
            "ftp_start", "ctl_start", "tsb_start",
            "p60s_start", "p300s_start", "p1200s_start"]
    return dict(zip(cols, row))


def block_history(n=10):
    """Return recent N closed blocks as list of dicts (most recent first)."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT block_id, start_date, end_date, block_type, phase, goal,
                   ftp_start, ftp_end, ftp_method,
                   ctl_start, ctl_end, tsb_start, tsb_end,
                   planned_tss, actual_tss, compliance_pct,
                   p60s_start, p60s_end, p300s_start, p300s_end,
                   p1200s_start, p1200s_end,
                   goal_achieved, biggest_limiter, motivation
            FROM blocks WHERE end_date IS NOT NULL
            ORDER BY start_date DESC LIMIT ?
        """, [n]).fetchall()
    except Exception:
        conn.close()
        return []
    conn.close()
    cols = ["block_id", "start_date", "end_date", "block_type", "phase", "goal",
            "ftp_start", "ftp_end", "ftp_method",
            "ctl_start", "ctl_end", "tsb_start", "tsb_end",
            "planned_tss", "actual_tss", "compliance_pct",
            "p60s_start", "p60s_end", "p300s_start", "p300s_end",
            "p1200s_start", "p1200s_end",
            "goal_achieved", "biggest_limiter", "motivation"]
    return [dict(zip(cols, row)) for row in rows]
