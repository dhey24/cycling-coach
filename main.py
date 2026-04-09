"""
main.py — Cycling coach orchestrator.

Usage:
    python main.py          # weekly run: adjust plan + send email
    python main.py --init   # first run: generate initial training block + send email
    python main.py --dry-run # generate email but don't send, print HTML to stdout
"""

import os
import sys
import json
import shutil
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

import strava_client
import metrics
import coach
import email_builder
import email_sender
import db as db_module
import calibrate_targets


def _is_block_complete(plan):
    """Return True if today is past the last week of the current plan."""
    if not plan or "weeks" not in plan or not plan["weeks"]:
        return False
    try:
        last_week_start = date.fromisoformat(plan["weeks"][-1]["week_start"])
        return date.today() > last_week_start + timedelta(days=6)
    except Exception:
        return False


def _build_block_summary_str(compliance_history, pmc_current, ctl_block_start,
                              curve_trend, segment_tracker, plan):
    """Format a concise block summary string for the Claude new-block prompt."""
    lines = []

    # Block goal
    if plan:
        goal = (plan.get("block_overview") or {}).get("goal")
        block_type = plan.get("phase", "unknown")
        if goal:
            lines.append(f"  Block goal ({block_type}): {goal}")

    # CTL change
    ctl_now = pmc_current.get("ctl", 0)
    if ctl_block_start:
        delta = round(ctl_now - ctl_block_start, 1)
        lines.append(f"  CTL: {ctl_block_start} → {ctl_now} ({delta:+.1f})")
    else:
        lines.append(f"  CTL (current): {ctl_now}")

    # Compliance
    if compliance_history and "Less than" not in compliance_history:
        lines.append("  Session compliance (last 5 weeks):")
        for l in compliance_history.split("\n"):
            lines.append(f"  {l.strip()}")
    else:
        lines.append("  Session compliance: insufficient data")

    # Power curve deltas
    if curve_trend:
        dur_labels = {60: "1-min", 300: "5-min", 600: "10-min", 1200: "20-min"}
        pc_lines = []
        for dur, lbl in dur_labels.items():
            d = curve_trend.get(dur, {})
            r, p = d.get("recent"), d.get("prior")
            if r and p:
                delta = round((r - p) / p * 100, 1)
                sign = "+" if delta >= 0 else ""
                pc_lines.append(f"{lbl}: {p}w→{r}w ({sign}{delta}%)")
        if pc_lines:
            lines.append("  Power curve (recent vs prior 4wk): " + " | ".join(pc_lines))

    # Segment progress (tier 1 only)
    if segment_tracker:
        tier1 = [s for s in segment_tracker if s.get("tier") == 1]
        for seg in tier1[:3]:
            name = seg.get("segment_name", "?")
            rank = seg.get("david_rank")
            gap  = seg.get("time_gap_s")
            pw   = seg.get("power_gap_w")
            rank_str = f"rank {rank}" if rank else "unranked"
            gap_str  = f"+{gap}s to KOM" if gap and gap > 0 else ("KOM" if gap is not None and gap <= 0 else "—")
            pw_str   = f"+{pw}w needed" if pw else ""
            lines.append(f"  Segment: {name} — {rank_str}, {gap_str} {pw_str}")

    return "\n".join(lines)


def get_planned_tss(plan, week_offset=1):
    """Get planned TSS for a given week offset from the current plan."""
    if not plan or "weeks" not in plan:
        return 0
    today = date.today()
    for week in plan["weeks"]:
        try:
            week_start = date.fromisoformat(week["week_start"])
            if (today - week_start).days <= 13:  # rough match for last week
                return week.get("target_tss", 0)
        except Exception:
            continue
    return plan["weeks"][0].get("target_tss", 0) if plan["weeks"] else 0


def run(init_mode=False, dry_run=False):
    # 1. Refresh Strava token
    strava_client.refresh_access_token()

    # 2. Fetch activities (8 weeks for PMC, force refresh on real runs)
    force = not dry_run
    activities = strava_client.fetch_activities(weeks=8, force_refresh=force)

    # 3. Compute core metrics
    ftp_outdoor, ftp_indoor = metrics.load_ftps()
    zone_ranges = metrics.compute_zone_ranges(ftp_outdoor, ftp_indoor)
    tss_by_day  = metrics.daily_tss(activities, ftp_outdoor, ftp_indoor, weeks=8)
    pmc_series  = metrics.compute_pmc(tss_by_day)
    pmc_current = metrics.current_pmc(pmc_series)

    last_week = metrics.weekly_summary(activities, ftp_outdoor, ftp_indoor, week_offset=1)
    this_week = metrics.weekly_summary(activities, ftp_outdoor, ftp_indoor, week_offset=0)

    # 4. HR analysis
    hr_data = metrics.hr_analysis(activities, weeks=4, ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor)

    # 4e. Write PMC to DuckDB (non-fatal — DB is enhancement, not critical path)
    try:
        db_module.append_pmc_rows(pmc_series)
        print("DB: PMC written.")
    except Exception as e:
        print(f"DB write failed (non-fatal): {e}")

    # 4g. Per-activity HR stream analysis + EF/decoupling/interval HR
    # NOTE: power_curve() is called AFTER this loop so all streams are cached first
    ef_data = None
    interval_hr_data = None
    rpe_signals = None
    curve_trend = None
    curve_data = {}
    cp_model_data = None
    phenotype_data = None
    is_recovery_week = False
    try:
        # Determine recovery week from PMC
        is_recovery_week = pmc_current.get("atl", 0) < 0.6 * pmc_current.get("ctl", 1)

        # Fetch HR streams + compute metrics for recent activities (8 weeks)
        activity_metrics_written = 0
        for act in activities:
            act_id = act.get("id")
            if not act_id:
                continue
            # Fetch power stream first — skip entirely if no power (commutes/junk miles)
            power_stream = strava_client.fetch_power_stream(act_id)
            if not power_stream:
                continue
            hr_stream = strava_client.fetch_hr_stream(act_id)
            act_metrics, interval_hr_list = metrics.compute_activity_metrics(
                act, power_stream, hr_stream,
                ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
            )
            if act_metrics:
                db_module.upsert_activity_metrics([act_metrics])
                if interval_hr_list:
                    db_module.upsert_interval_hr_stats(interval_hr_list)
                activity_metrics_written += 1
        print(f"Activity metrics: wrote {activity_metrics_written} activities to DB.")

        # Power curve — runs after streams are cached so best_4wk_ago is populated
        curve_data = metrics.power_curve(
            activities, strava_client.fetch_power_stream,
            ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
        )
        print(f"Power curve: " + " | ".join(
            f"{s}s={v['best']}w" for s, v in curve_data.items() if v["best"]
        ))
        db_module.append_power_curve(curve_data, date.today())
        print("DB: power curve written.")

        # Extended clean power curve + CP model (additive — does not touch power_curve_weekly)
        try:
            max_hr = metrics.load_max_hr()
            clean_curve = metrics.power_curve_extended(
                activities, strava_client.fetch_power_stream, ftp_outdoor=ftp_outdoor,
                fetch_hr_fn=strava_client.fetch_hr_stream, max_hr=max_hr,
            )
            cp_model_data = metrics.fit_cp_model(clean_curve)
            db_module.append_power_curve_extended(clean_curve, date.today())
            if cp_model_data:
                db_module.append_cp_model(cp_model_data, date.today())
                print(
                    f"CP model: {cp_model_data['model_type']} | "
                    f"CP={cp_model_data['cp_watts']}w | W'={cp_model_data['w_prime_kj']}kJ | "
                    f"R²={cp_model_data['r_squared']:.3f} | "
                    f"anchors={cp_model_data['anchor_count']} | "
                    f"confidence={cp_model_data['confidence']}"
                )
            else:
                print("CP model: insufficient clean anchor data (need ≥3 points, span ≥3×).")
        except Exception as e:
            print(f"Extended power curve / CP model failed (non-fatal): {e}")
            cp_model_data = None

        # Query trend data for email
        ef_data = db_module.ef_trend(weeks=4)
        interval_hr_data = db_module.interval_hr_trend(weeks=4)
        curve_trend = db_module.power_curve_trend()

        phenotype_data = metrics.compute_power_phenotype(curve_data)
        print(f"Phenotype: {phenotype_data['primary']} — {phenotype_data['best_kom_label']}")
    except Exception as e:
        print(f"HR/EF analysis failed (non-fatal): {e}")
        # Fall back to power curve without stream caching benefit
        if curve_data is None:
            try:
                curve_data = metrics.power_curve(
                    activities, strava_client.fetch_power_stream,
                    ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
                )
            except Exception:
                curve_data = {}

    # 4b. YoY comparison (non-fatal — no data in year 1)
    yoy_data = None
    try:
        yoy_pmc = db_module.pmc_yoy(date.today())
        yoy_curve = db_module.power_curve_yoy(date.today())
        if yoy_pmc or yoy_curve:
            yoy_data = {"pmc": yoy_pmc, "curve": yoy_curve}
            print(f"YoY data: CTL {(yoy_pmc or {}).get('ctl', '?')} (last year)")
    except Exception as e:
        print(f"YoY query failed (non-fatal): {e}")

    # 4c. Ride descriptions for feedback signal + RPE inference (done after session_comparison)
    strava_descriptions = metrics.collect_ride_descriptions(
        activities, strava_client.fetch_activity_description, days=7,
    )
    if strava_descriptions:
        print(f"Ride descriptions: {len(strava_descriptions)} found")

    # 4d. Mid-week check-in
    checkin_path = os.path.join(os.path.dirname(__file__), "data", "checkin.json")
    checkin_data = None
    if os.path.exists(checkin_path):
        with open(checkin_path) as f:
            checkin_data = json.load(f)
        print(f"Check-in loaded ({checkin_data.get('timestamp', '?')})")
        archive = os.path.join(os.path.dirname(__file__), "data",
                               f"checkin_{date.today().isoformat()}.json")
        os.rename(checkin_path, archive)

    # 4f. Segment tracking (starred + ride history → tier 1/2)
    compliance_history = "  No compliance history yet."
    segment_tracker = []
    try:
        starred = strava_client.fetch_starred_segments()
        print(f"Starred segments: {len(starred)}")
        ride_segs = metrics.extract_ride_segments(
            activities, strava_client.fetch_activity_segments, weeks=8
        )
        print(f"Ride segments: {len(ride_segs)} unique")
        home_lat, home_lng = metrics.load_home_coords()
        segment_tracker = metrics.build_segment_tracker(
            starred, ride_segs, strava_client.fetch_segment_leaderboard, curve_data,
            home_lat=home_lat, home_lng=home_lng,
        )
        print(f"Segments tracked: {len(segment_tracker)}")
        db_module.append_segment_efforts(segment_tracker)
    except Exception as e:
        print(f"Segment tracking failed (non-fatal): {e}")
        segment_tracker = []

    # 5. Data date range (for email header)
    data_range_end   = date.today().isoformat()
    data_range_start = (date.today() - timedelta(weeks=8)).isoformat()

    print(f"\nCurrent PMC: CTL={pmc_current['ctl']} ATL={pmc_current['atl']} TSB={pmc_current['tsb']}")
    print(f"Last week:   TSS={last_week['tss']} | {last_week['distance_km']}km | "
          f"{last_week['elevation_m']}m elev | {last_week['ride_count']} rides")
    if hr_data:
        print(f"HR analysis: Power:HR ratio {hr_data.get('power_hr_ratio_last_week','N/A')} "
              f"({hr_data.get('ratio_trend','N/A')}) | "
              f"Elevation flag: {hr_data.get('hr_elevation_flag', False)}")

    block_overview    = None
    session_comparison = None

    # Fitness test triggers (non-fatal)
    fitness_test_triggers = None
    try:
        fitness_test_triggers = db_module.fitness_test_triggers()
        stale = fitness_test_triggers.get("stale_durations", [])
        drop = fitness_test_triggers.get("atl_drop_pct")
        if stale or drop:
            print(f"Fitness test triggers: stale={stale} atl_drop={drop}%")
        else:
            print("Fitness test triggers: none active")
    except Exception as e:
        print(f"Fitness test trigger check failed (non-fatal): {e}")

    # 6. Generate plan / email via Claude
    # Fetch block-level history for generate_block() context (non-fatal)
    block_history = ""
    try:
        block_history = db_module.block_history_summary(blocks=2)
        if block_history:
            print("DB: block history loaded.")
    except Exception as e:
        print(f"DB block history fetch failed (non-fatal): {e}")

    plan_before_snapshot = None
    updated_plan = None

    if init_mode:
        plan = coach.generate_block(pmc_current, last_week_tss=last_week["tss"],
                                    block_history=block_history)
        block_overview = plan.get("block_overview")
        planned_tss    = plan["weeks"][0].get("target_tss", 0) if plan.get("weeks") else 0
        # Open block in DB (non-fatal)
        try:
            first_week_start = date.fromisoformat(plan["weeks"][0]["week_start"])
            db_module.open_block(
                start_date=first_week_start,
                block_type=plan.get("phase", "build"),
                phase="pre-season",  # default; user can update preferences.md
                goal=(plan.get("block_overview") or {}).get("goal", ""),
                ftp=ftp_outdoor,
                ctl=pmc_current.get("ctl", 0),
                tsb=pmc_current.get("tsb", 0),
                power_curve=curve_data,
            )
        except Exception as e:
            print(f"DB open_block failed (non-fatal): {e}")

        # Session comparison not meaningful on first run (no prior planned week)
        email_content, updated_plan = coach.generate_weekly_email(
            pmc_current, last_week, planned_tss,
            session_comparison=None, hr_data=hr_data,
            strava_descriptions=strava_descriptions,
            checkin_data=checkin_data,
            compliance_history=compliance_history,
            segment_data=segment_tracker,
            ef_data=ef_data, interval_hr_data=interval_hr_data,
            rpe_signals=rpe_signals,
            phenotype_data=phenotype_data, yoy_data=yoy_data,
            fitness_test_triggers=fitness_test_triggers,
            cp_model_data=cp_model_data,
        )
    else:
        current_plan       = coach._load_plan()

        # Warn if block is complete
        if _is_block_complete(current_plan):
            print("\n" + "=" * 60)
            print("  BLOCK COMPLETE — the 5-week plan has expired.")
            print("  Run the following to start the next block:")
            print("    python block_debrief.py")
            print("    python main.py --new-block [--dry-run]")
            print("=" * 60 + "\n")

        plan_before_snapshot = current_plan
        planned_tss        = get_planned_tss(current_plan, week_offset=1)
        block_overview     = current_plan.get("block_overview") if current_plan else None
        session_comparison = metrics.match_sessions_to_rides(
            current_plan, activities, ftp_outdoor, ftp_indoor, week_offset=1
        )
        session_comparison = metrics.interval_achievement(
            session_comparison, strava_client.fetch_power_stream,
            ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
            fetch_hr_fn=strava_client.fetch_hr_stream,
            fetch_laps_fn=strava_client.fetch_activity_laps,
        )
        session_comparison = metrics.recompute_status(session_comparison)
        # DB: write session compliance + fetch multi-week trends
        try:
            last_week_start = date.fromisoformat(last_week["week_start"])
            db_module.append_session_compliance(session_comparison, week_start=last_week_start)
            compliance_history = db_module.compliance_trends(weeks=4)
            # Save per-interval power + HR rows
            for day in session_comparison:
                a = (day.get("actual") or {})
                db_rows = a.get("interval_db_rows")
                if db_rows:
                    db_module.upsert_interval_hr_stats(db_rows)
            print("DB: session compliance + interval stats written.")
        except Exception as e:
            print(f"DB compliance write failed (non-fatal): {e}")

        # Calibrate outdoor targets — updates preferences.md before coach reads it
        try:
            current_plan = coach._load_plan()
            calibrate_targets.run_calibration(
                activities, current_plan, ftp_outdoor, ftp_indoor,
                max_hr=metrics.load_max_hr(), weeks=4,
            )
        except Exception as e:
            print(f"Calibration run failed (non-fatal): {e}")

        # RPE inference from descriptions (needs session_comparison for calibration)
        if strava_descriptions:
            try:
                rpe_signals = coach.infer_rpe_from_descriptions(
                    strava_descriptions, session_comparison
                )
                if rpe_signals:
                    print(f"RPE: inferred for {len(rpe_signals)} rides")
                    for rpe in rpe_signals:
                        db_module.upsert_rpe(
                            rpe["activity_id"],
                            rpe.get("inferred_rpe"),
                            rpe.get("sentiment"),
                            rpe.get("keywords"),
                        )
            except Exception as e:
                print(f"RPE inference failed (non-fatal): {e}")

        email_content, updated_plan = coach.generate_weekly_email(
            pmc_current, last_week, planned_tss,
            session_comparison=session_comparison, hr_data=hr_data,
            strava_descriptions=strava_descriptions,
            checkin_data=checkin_data,
            compliance_history=compliance_history,
            segment_data=segment_tracker,
            ef_data=ef_data, interval_hr_data=interval_hr_data,
            rpe_signals=rpe_signals,
            phenotype_data=phenotype_data, yoy_data=yoy_data,
            fitness_test_triggers=fitness_test_triggers,
            cp_model_data=cp_model_data,
        )

    # Log coaching history (non-fatal)
    try:
        _log_week_start = date.fromisoformat(last_week["week_start"])
        db_module.insert_coaching_log(
            week_start=_log_week_start,
            plan_before=plan_before_snapshot,
            plan_after=updated_plan,
            email_json=email_content,
            tss_planned=planned_tss,
            tss_actual=last_week.get("tss", 0),
            tsb_at_week_start=pmc_current.get("tsb", 0),
        )
        print("DB: coaching log written.")
    except Exception as e:
        print(f"DB coaching log write failed (non-fatal): {e}")

    # 7. Build HTML
    html = email_builder.build_email(
        email_content, pmc_current, last_week,
        session_comparison=session_comparison,
        hr_data=hr_data,
        init_mode=init_mode,
        block_overview=block_overview,
        data_range_start=data_range_start,
        data_range_end=data_range_end,
        zone_ranges=zone_ranges,
        ftp_outdoor=ftp_outdoor,
        ftp_indoor=ftp_indoor,
        power_curve_data=curve_data,
        strava_descriptions=strava_descriptions,
        checkin_data=checkin_data,
        segment_data=segment_tracker,
        ef_data=ef_data,
        interval_hr_data=interval_hr_data,
        rpe_signals=rpe_signals,
        curve_trend=curve_trend,
        is_recovery_week=is_recovery_week,
        test_prescription=email_content.get("test_prescription"),
    )

    # 8. Send or print
    if dry_run:
        preview_path = "/tmp/cycling_email_preview.html"
        with open(preview_path, "w") as f:
            f.write(html)
        print(f"\n--- EMAIL HTML (dry run) ---")
        print(f"Full HTML saved to {preview_path}")
    else:
        email_sender.send_email(html)


def run_new_block(dry_run=False):
    """
    Block transition flow: close the current block, generate the next one, send email.

    Requires data/block_debrief.json to exist (run block_debrief.py first).
    """
    debrief_path = os.path.join(os.path.dirname(__file__), "data", "block_debrief.json")
    if not os.path.exists(debrief_path):
        print("\nERROR: data/block_debrief.json not found.")
        print("Run block_debrief.py first:\n  python block_debrief.py")
        sys.exit(1)

    with open(debrief_path) as f:
        debrief = json.load(f)
    print(f"Debrief loaded ({debrief.get('timestamp', '?')})")

    # --- Data collection (same as normal run) ---
    strava_client.refresh_access_token()
    activities = strava_client.fetch_activities(weeks=8, force_refresh=not dry_run)

    ftp_outdoor, ftp_indoor = metrics.load_ftps()
    zone_ranges  = metrics.compute_zone_ranges(ftp_outdoor, ftp_indoor)
    tss_by_day   = metrics.daily_tss(activities, ftp_outdoor, ftp_indoor, weeks=8)
    pmc_series   = metrics.compute_pmc(tss_by_day)
    pmc_current  = metrics.current_pmc(pmc_series)

    try:
        db_module.append_pmc_rows(pmc_series)
    except Exception as e:
        print(f"DB PMC write failed (non-fatal): {e}")

    # Power curve
    curve_data  = {}
    curve_trend = {}
    try:
        for act in activities:
            act_id = act.get("id")
            if not act_id:
                continue
            power_stream = strava_client.fetch_power_stream(act_id)
            if not power_stream:
                continue
            hr_stream = strava_client.fetch_hr_stream(act_id)
            act_m, ih_list = metrics.compute_activity_metrics(
                act, power_stream, hr_stream,
                ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
            )
            if act_m:
                db_module.upsert_activity_metrics([act_m])
                if ih_list:
                    db_module.upsert_interval_hr_stats(ih_list)

        curve_data  = metrics.power_curve(
            activities, strava_client.fetch_power_stream,
            ftp_outdoor=ftp_outdoor, ftp_indoor=ftp_indoor,
        )
        db_module.append_power_curve(curve_data, date.today())
        curve_trend = db_module.power_curve_trend()
    except Exception as e:
        print(f"Power curve collection failed (non-fatal): {e}")

    # Segments
    segment_tracker = []
    try:
        starred  = strava_client.fetch_starred_segments()
        ride_segs = metrics.extract_ride_segments(
            activities, strava_client.fetch_activity_segments, weeks=8
        )
        home_lat, home_lng = metrics.load_home_coords()
        segment_tracker = metrics.build_segment_tracker(
            starred, ride_segs, strava_client.fetch_segment_leaderboard, curve_data,
            home_lat=home_lat, home_lng=home_lng,
        )
        db_module.append_segment_efforts(segment_tracker)
    except Exception as e:
        print(f"Segment tracking failed (non-fatal): {e}")

    # Compliance history (5 weeks for full block view)
    compliance_history = "  No compliance history yet."
    try:
        compliance_history = db_module.compliance_trends(weeks=5)
    except Exception as e:
        print(f"Compliance trends failed (non-fatal): {e}")

    # Get current block info for CTL start
    ctl_block_start = None
    current_block_row = None
    try:
        current_block_row = db_module.current_block()
        if current_block_row:
            ctl_block_start = current_block_row.get("ctl_start")
    except Exception as e:
        print(f"current_block() failed (non-fatal): {e}")

    current_plan = coach._load_plan()

    # --- Fitness reassessment ---
    fitness_assessment = {"scenario": "C", "estimated_ftp": None,
                          "evidence_summary": "No power data available."}
    try:
        fitness_assessment = coach.assess_fitness_evidence(curve_data, ftp_outdoor)
        print(f"Fitness scenario: {fitness_assessment['scenario']} — "
              f"{fitness_assessment['evidence_summary'][:80]}")
    except Exception as e:
        print(f"Fitness evidence assessment failed (non-fatal): {e}")

    # Update FTP in preferences.md if Scenario A gave a new estimate
    if fitness_assessment.get("estimated_ftp") and fitness_assessment["scenario"] == "A":
        new_ftp = fitness_assessment["estimated_ftp"]
        try:
            metrics.update_ftp(new_ftp, "outdoor")
            ftp_outdoor = new_ftp
            zone_ranges = metrics.compute_zone_ranges(ftp_outdoor, ftp_indoor)
            print(f"FTP updated to {new_ftp}w (CP model estimate)")
        except Exception as e:
            print(f"FTP update failed (non-fatal): {e}")

    # --- Build block summary string ---
    block_summary_str = _build_block_summary_str(
        compliance_history, pmc_current, ctl_block_start,
        curve_trend, segment_tracker, current_plan,
    )
    block_history_str = ""
    try:
        block_history_str = db_module.block_history_summary(blocks=2)
    except Exception as e:
        print(f"block_history_summary failed (non-fatal): {e}")

    # --- Compute block-level compliance % for DB close ---
    block_compliance_pct = None
    try:
        if current_block_row:
            start_d = current_block_row["start_date"]
            conn = db_module.get_conn()
            row = conn.execute("""
                SELECT AVG(CASE WHEN status IN ('hit','done','partial') THEN 1.0 ELSE 0.0 END)
                FROM session_compliance
                WHERE week_start >= ?
                  AND session_type IS NOT NULL AND session_type != 'rest'
            """, [start_d]).fetchone()
            conn.close()
            if row and row[0] is not None:
                block_compliance_pct = round(row[0] * 100, 1)
    except Exception as e:
        print(f"Block compliance pct query failed (non-fatal): {e}")

    # --- Close current block in DB ---
    try:
        if current_block_row:
            # Sum actual TSS from last 5 weeks
            total_actual_tss = sum(
                tss_by_day.get(str(d), 0)
                for d in [date.today() - timedelta(days=i) for i in range(35)]
            )
            db_module.close_block(
                start_date=current_block_row["start_date"],
                end_date=date.today(),
                ftp_end=ftp_outdoor,
                ftp_method=fitness_assessment["scenario"].lower().replace("a", "cp_model")
                            .replace("b", "kom_effort").replace("c", "carry_forward"),
                ctl_end=pmc_current.get("ctl", 0),
                tsb_end=pmc_current.get("tsb", 0),
                actual_tss=int(total_actual_tss),
                compliance_pct=block_compliance_pct,
                power_curve_end=curve_data,
                debrief=debrief,
            )
    except Exception as e:
        print(f"close_block failed (non-fatal): {e}")

    # --- Generate new block ---
    new_plan = coach.generate_new_block(
        pmc_current=pmc_current,
        ctl_block_start=ctl_block_start,
        block_summary_str=block_summary_str,
        debrief=debrief,
        fitness_assessment=fitness_assessment,
        block_history_str=block_history_str,
    )

    # --- Archive old plan ---
    old_plan_path = os.path.join(os.path.dirname(__file__), "data", "plan.json")
    archive_dir   = os.path.join(os.path.dirname(__file__), "data", "plans")
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f"plan_transition_{date.today().isoformat()}.json")
    if os.path.exists(old_plan_path):
        shutil.copy2(old_plan_path, archive_path)
        print(f"Old plan archived to {archive_path}")

    # --- Open new block in DB ---
    try:
        first_week = new_plan["weeks"][0]
        new_start  = date.fromisoformat(first_week["week_start"])
        db_module.open_block(
            start_date=new_start,
            block_type=new_plan.get("phase", "build"),
            phase=debrief.get("goal_description", "in-season") or "in-season",
            goal=(new_plan.get("block_overview") or {}).get("goal", ""),
            ftp=ftp_outdoor,
            ctl=pmc_current.get("ctl", 0),
            tsb=pmc_current.get("tsb", 0),
            power_curve=curve_data,
        )
    except Exception as e:
        print(f"open_block (new) failed (non-fatal): {e}")

    # --- Generate transition email content ---
    email_content = coach.generate_block_transition_email(
        pmc_current=pmc_current,
        ctl_block_start=ctl_block_start,
        block_summary_str=block_summary_str,
        debrief=debrief,
        fitness_assessment=fitness_assessment,
        new_plan=new_plan,
    )

    # --- Build HTML ---
    week1_plan = {}
    try:
        for session in new_plan["weeks"][0]["sessions"]:
            week1_plan[session["day"]] = session
    except Exception:
        pass

    html = email_builder.build_transition_email(
        email_content=email_content,
        pmc_current=pmc_current,
        ctl_block_start=ctl_block_start,
        debrief=debrief,
        fitness_assessment=fitness_assessment,
        new_plan=new_plan,
        zone_ranges=zone_ranges,
        ftp_outdoor=ftp_outdoor,
        ftp_indoor=ftp_indoor,
        power_curve_data=curve_data,
        curve_trend=curve_trend,
        segment_data=segment_tracker,
    )

    # --- Send or print ---
    if dry_run:
        preview_path = "/tmp/cycling_email_preview.html"
        with open(preview_path, "w") as f:
            f.write(html)
        print(f"\n--- TRANSITION EMAIL (dry run) ---")
        print(f"Full HTML saved to {preview_path}")
    else:
        email_sender.send_email(html)

    # --- Archive debrief ---
    archive_debrief = os.path.join(
        os.path.dirname(__file__), "data",
        f"block_debrief_{date.today().isoformat()}.json"
    )
    shutil.move(debrief_path, archive_debrief)
    print(f"Debrief archived to {archive_debrief}")


if __name__ == "__main__":
    init_mode  = "--init" in sys.argv
    new_block  = "--new-block" in sys.argv
    dry_run    = "--dry-run" in sys.argv
    date_arg   = next((a for a in sys.argv if a.startswith("--date=")), None)

    def _do_run():
        try:
            if new_block:
                run_new_block(dry_run=dry_run)
            else:
                run(init_mode=init_mode, dry_run=dry_run)
        except Exception as e:
            print(f"\nFATAL ERROR: {e}")
            import traceback
            traceback.print_exc()
            try:
                email_sender.send_error_email(e)
            except Exception:
                pass
            sys.exit(1)

    if date_arg:
        # Override date.today() across all app modules so week calculations
        # use the specified date instead of the actual calendar date.
        # Useful for triggering Monday's run on Sunday (--date=2026-03-30).
        from unittest.mock import patch as _mock_patch
        import contextlib as _contextlib
        _fixed = date.fromisoformat(date_arg.split("=", 1)[1])
        print(f"Date override: treating today as {_fixed.isoformat()}")

        class _FakeDate(date):
            @classmethod
            def today(cls):
                return _fixed

        import sys as _sys
        _this_mod = _sys.modules[__name__]
        with _contextlib.ExitStack() as _stack:
            for _mod in [metrics, coach, email_builder, email_sender, _this_mod]:
                _stack.enter_context(_mock_patch.object(_mod, "date", _FakeDate))
            _do_run()
    else:
        _do_run()
