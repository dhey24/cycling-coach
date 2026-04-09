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
    """CREATE TABLE IF NOT EXISTS power_curve_extended (
        week_end DATE PRIMARY KEY,
        s60      INT,
        s120     INT,
        s180     INT,
        s300     INT,
        s480     INT,
        s600     INT,
        s720     INT,
        s900     INT,
        s1200    INT
    )""",
    """CREATE TABLE IF NOT EXISTS cp_model_weekly (
        week_end             DATE PRIMARY KEY,
        cp_watts             INT,
        w_prime_kj           FLOAT,
        k_constant           FLOAT,
        model_type           VARCHAR,
        r_squared            FLOAT,
        anchor_count         INT,
        max_duration_used_s  INT,
        anchors_only_short   BOOLEAN,
        est_20min            INT,
        est_60min            INT,
        confidence           VARCHAR
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
    # Migrate power_curve_extended: add likelihood columns
    for col in ["s60_likelihood", "s120_likelihood", "s180_likelihood", "s300_likelihood",
                "s480_likelihood", "s600_likelihood", "s720_likelihood", "s900_likelihood",
                "s1200_likelihood"]:
        try:
            conn.execute(f"ALTER TABLE power_curve_extended ADD COLUMN {col} FLOAT")
        except Exception:
            pass
    # Migrate cp_model_weekly: add avg_anchor_likelihood column
    try:
        conn.execute("ALTER TABLE cp_model_weekly ADD COLUMN avg_anchor_likelihood FLOAT")
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
    """INSERT OR REPLACE all PMC rows into pmc_daily (full history for YoY comparison)."""
    if not pmc_series:
        return
    conn = get_conn()
    for row in pmc_series:
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


def append_power_curve_extended(clean_curve, week_end):
    """INSERT OR REPLACE one row into power_curve_extended (clean windows only).

    clean_curve values may be dicts {watts, model_watts, likelihood} or plain ints.
    Stores display watts in the s{d} columns and likelihood in s{d}_likelihood columns.
    """
    if not clean_curve:
        return

    def _watts(v):
        return v.get("watts") if isinstance(v, dict) else v

    def _lk(v):
        return v.get("likelihood") if isinstance(v, dict) else None

    conn = get_conn()
    conn.execute("""
        INSERT INTO power_curve_extended
            (week_end,
             s60, s120, s180, s300, s480, s600, s720, s900, s1200,
             s60_likelihood, s120_likelihood, s180_likelihood, s300_likelihood,
             s480_likelihood, s600_likelihood, s720_likelihood, s900_likelihood,
             s1200_likelihood)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (week_end) DO UPDATE SET
            s60   = EXCLUDED.s60,   s120  = EXCLUDED.s120,
            s180  = EXCLUDED.s180,  s300  = EXCLUDED.s300,
            s480  = EXCLUDED.s480,  s600  = EXCLUDED.s600,
            s720  = EXCLUDED.s720,  s900  = EXCLUDED.s900,
            s1200 = EXCLUDED.s1200,
            s60_likelihood   = EXCLUDED.s60_likelihood,
            s120_likelihood  = EXCLUDED.s120_likelihood,
            s180_likelihood  = EXCLUDED.s180_likelihood,
            s300_likelihood  = EXCLUDED.s300_likelihood,
            s480_likelihood  = EXCLUDED.s480_likelihood,
            s600_likelihood  = EXCLUDED.s600_likelihood,
            s720_likelihood  = EXCLUDED.s720_likelihood,
            s900_likelihood  = EXCLUDED.s900_likelihood,
            s1200_likelihood = EXCLUDED.s1200_likelihood
    """, [
        week_end,
        _watts(clean_curve.get(60)),   _watts(clean_curve.get(120)),
        _watts(clean_curve.get(180)),  _watts(clean_curve.get(300)),
        _watts(clean_curve.get(480)),  _watts(clean_curve.get(600)),
        _watts(clean_curve.get(720)),  _watts(clean_curve.get(900)),
        _watts(clean_curve.get(1200)),
        _lk(clean_curve.get(60)),   _lk(clean_curve.get(120)),
        _lk(clean_curve.get(180)),  _lk(clean_curve.get(300)),
        _lk(clean_curve.get(480)),  _lk(clean_curve.get(600)),
        _lk(clean_curve.get(720)),  _lk(clean_curve.get(900)),
        _lk(clean_curve.get(1200)),
    ])
    conn.close()


def append_cp_model(model_result, week_end):
    """INSERT OR REPLACE one row into cp_model_weekly."""
    if not model_result:
        return
    conn = get_conn()
    conn.execute("""
        INSERT INTO cp_model_weekly
            (week_end, cp_watts, w_prime_kj, k_constant, model_type,
             r_squared, anchor_count, max_duration_used_s, anchors_only_short,
             est_20min, est_60min, confidence, avg_anchor_likelihood)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (week_end) DO UPDATE SET
            cp_watts              = EXCLUDED.cp_watts,
            w_prime_kj            = EXCLUDED.w_prime_kj,
            k_constant            = EXCLUDED.k_constant,
            model_type            = EXCLUDED.model_type,
            r_squared             = EXCLUDED.r_squared,
            anchor_count          = EXCLUDED.anchor_count,
            max_duration_used_s   = EXCLUDED.max_duration_used_s,
            anchors_only_short    = EXCLUDED.anchors_only_short,
            est_20min             = EXCLUDED.est_20min,
            est_60min             = EXCLUDED.est_60min,
            confidence            = EXCLUDED.confidence,
            avg_anchor_likelihood = EXCLUDED.avg_anchor_likelihood
    """, [
        week_end,
        model_result.get("cp_watts"),
        model_result.get("w_prime_kj"),
        model_result.get("k_constant"),
        model_result.get("model_type"),
        model_result.get("r_squared"),
        model_result.get("anchor_count"),
        model_result.get("max_duration_used_s"),
        model_result.get("anchors_only_short"),
        model_result.get("est_20min"),
        model_result.get("est_60min"),
        model_result.get("confidence"),
        model_result.get("avg_anchor_likelihood"),
    ])
    conn.close()


def get_cp_model_current():
    """Return the most recent cp_model_weekly row as a dict, or None."""
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT cp_watts, w_prime_kj, k_constant, model_type, r_squared,
                   anchor_count, max_duration_used_s, anchors_only_short,
                   est_20min, est_60min, confidence, week_end
            FROM cp_model_weekly
            ORDER BY week_end DESC
            LIMIT 1
        """).fetchone()
    except Exception:
        conn.close()
        return None
    conn.close()
    if not row:
        return None
    keys = ["cp_watts", "w_prime_kj", "k_constant", "model_type", "r_squared",
            "anchor_count", "max_duration_used_s", "anchors_only_short",
            "est_20min", "est_60min", "confidence", "week_end"]
    return dict(zip(keys, row))


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


def pmc_yoy(target_date, year_offset=1):
    """
    Return {ctl, atl, tsb, date} for the same date N years ago, or None if no data.
    Requires full pmc_daily history (not rolling-14-day). Useful for YoY fitness comparison.
    """
    yoy_date = target_date - timedelta(days=365 * year_offset)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT ctl, atl, tsb FROM pmc_daily WHERE date = ?", [yoy_date]
        ).fetchone()
    except Exception:
        conn.close()
        return None
    conn.close()
    if not row or row[0] is None:
        return None
    return {"ctl": row[0], "atl": row[1], "tsb": row[2], "date": yoy_date.isoformat()}


def power_curve_yoy(target_date, year_offset=1):
    """
    Return {s60, s300, s600, s1200} for the week closest to same date N years ago, or None.
    Searches within ±14 days of the anniversary date.
    """
    yoy_date = target_date - timedelta(days=365 * year_offset)
    window_start = yoy_date - timedelta(days=14)
    window_end = yoy_date + timedelta(days=14)
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT s60, s300, s600, s1200
            FROM power_curve_weekly
            WHERE week_end BETWEEN ? AND ?
            ORDER BY week_end DESC
            LIMIT 1
        """, [window_start, window_end]).fetchone()
    except Exception:
        conn.close()
        return None
    conn.close()
    if not row or all(v is None for v in row):
        return None
    return {"s60": row[0], "s300": row[1], "s600": row[2], "s1200": row[3]}


def fitness_test_triggers(today=None):
    """
    Check conditions that warrant a mid-block fitness test prescription.

    Returns a dict with keys:
      stale_durations:     list of duration labels with no clean data in power_curve_extended
                           in the last 6 weeks (checks 5-min and 20-min anchors).
      atl_drop_pct:        float if ATL dropped >30% peak-to-trough sustained ≥10 days, else None.
      atl_drop_days:       int — consecutive days ATL was >30% below its 30-day peak. None if no drop.
      low_model_confidence: bool — True if cp_model_weekly confidence is "low" and anchor_count < 3.
      anchors_only_short:  bool — True if longest clean CP anchor < 10min (extrapolation zone).
      max_duration_used_s: int | None — longest clean anchor from cp_model_weekly.
      model_type:          str | None — "3param" / "2param" / None if no model yet.
    """
    from datetime import date as _date
    today = today or _date.today()
    cutoff_6wk = today - timedelta(weeks=6)

    conn = get_conn()
    result = {
        "stale_durations":      [],
        "atl_drop_pct":         None,
        "atl_drop_days":        None,
        "low_model_confidence": False,
        "anchors_only_short":   False,
        "max_duration_used_s":  None,
        "model_type":           None,
        "case_a_durations":     [],   # no clean window at all (venue problem)
        "case_b_durations":     [],   # clean but low likelihood (session structure problem)
        "duration_likelihoods": {},   # raw per-duration likelihood for inspection
    }

    # Key durations to assess for Case A/B (seconds → label)
    _KEY_DURATIONS = {300: "5-min", 480: "8-min", 720: "12-min", 1200: "20-min"}
    _CASE_B_THRESHOLD = 0.4

    # --- Stale clean power curve durations + Case A/B (uses power_curve_extended) ---
    try:
        row = conn.execute("""
            SELECT
                MAX(CASE WHEN s300  IS NOT NULL THEN week_end END) AS last_5min,
                MAX(CASE WHEN s1200 IS NOT NULL THEN week_end END) AS last_20min,
                MAX(s300_likelihood)  AS lk_300,
                MAX(s480_likelihood)  AS lk_480,
                MAX(s720_likelihood)  AS lk_720,
                MAX(s1200_likelihood) AS lk_1200,
                MAX(s300)  AS w_300,
                MAX(s480)  AS w_480,
                MAX(s720)  AS w_720,
                MAX(s1200) AS w_1200
            FROM power_curve_extended
            WHERE week_end >= ?
        """, [cutoff_6wk]).fetchone()
        if row:
            last_5min, last_20min = row[0], row[1]
            lk_map = {300: row[2], 480: row[3], 720: row[4], 1200: row[5]}
            w_map  = {300: row[6], 480: row[7], 720: row[8], 1200: row[9]}

            if last_5min is None or last_5min < cutoff_6wk:
                result["stale_durations"].append("5-min")
            if last_20min is None or last_20min < cutoff_6wk:
                result["stale_durations"].append("20-min")

            for dur_s, label in _KEY_DURATIONS.items():
                w = w_map.get(dur_s)
                lk = lk_map.get(dur_s)
                result["duration_likelihoods"][str(dur_s)] = lk
                if w is None:
                    result["case_a_durations"].append(label)
                elif lk is not None and lk < _CASE_B_THRESHOLD:
                    result["case_b_durations"].append(label)
    except Exception:
        pass

    # --- ATL drop: peak-to-trough in last 30 days ---
    try:
        cutoff_30d = today - timedelta(days=30)
        rows = conn.execute("""
            SELECT date, atl FROM pmc_daily
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC
        """, [cutoff_30d, today]).fetchall()

        if rows and len(rows) >= 10:
            atl_values = [(r[0], r[1]) for r in rows if r[1] is not None]
            if atl_values:
                peak_atl = max(v for _, v in atl_values)
                if peak_atl > 0:
                    threshold = peak_atl * 0.70
                    max_run = 0
                    cur_run = 0
                    trough_atl = None
                    for _, atl_val in atl_values:
                        if atl_val < threshold:
                            cur_run += 1
                            trough_atl = min(trough_atl, atl_val) if trough_atl is not None else atl_val
                            max_run = max(max_run, cur_run)
                        else:
                            cur_run = 0
                    if max_run >= 10 and trough_atl is not None:
                        drop_pct = round((peak_atl - trough_atl) / peak_atl * 100, 1)
                        result["atl_drop_pct"] = drop_pct
                        result["atl_drop_days"] = max_run
    except Exception:
        pass

    # --- CP model quality from cp_model_weekly ---
    try:
        row = conn.execute("""
            SELECT confidence, anchor_count, anchors_only_short, max_duration_used_s, model_type
            FROM cp_model_weekly
            ORDER BY week_end DESC
            LIMIT 1
        """).fetchone()
        if row:
            confidence, anchor_count, anchors_only_short, max_dur, model_type = row
            result["model_type"] = model_type
            result["max_duration_used_s"] = max_dur
            result["anchors_only_short"] = bool(anchors_only_short)
            result["low_model_confidence"] = (confidence == "low" and (anchor_count or 0) < 3)
    except Exception:
        pass

    conn.close()
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
