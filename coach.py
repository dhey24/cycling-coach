"""
coach.py — Claude API: training block generation and weekly adjustment.
"""

import os
import json
import shutil
import tempfile
import time
import re
import anthropic
from datetime import date

PLAN_PATH = os.path.join(os.path.dirname(__file__), "data", "plan.json")
PLAN_ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "data", "plans")
PREFS_PATH = os.path.join(os.path.dirname(__file__), "preferences.md")

MODEL = "claude-opus-4-6"
MAX_TOKENS = 8000
RETRIES = 3
RETRY_DELAY = 15


def _load_preferences():
    try:
        with open(PREFS_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _load_plan():
    try:
        with open(PLAN_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _save_plan(plan):
    os.makedirs(os.path.dirname(PLAN_PATH), exist_ok=True)
    os.makedirs(PLAN_ARCHIVE_DIR, exist_ok=True)
    if os.path.exists(PLAN_PATH):
        shutil.copy2(PLAN_PATH, os.path.join(PLAN_ARCHIVE_DIR,
                     f"plan_{date.today().isoformat()}.json"))
    dir_ = os.path.dirname(PLAN_PATH)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(plan, f, indent=2, default=str)
        os.replace(tmp_path, PLAN_PATH)
    except Exception:
        os.unlink(tmp_path)
        raise


def _strip_markdown_json(text):
    """Extract JSON from a response that may be wrapped in ```json ... ```."""
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        return match.group(1)
    return text.strip()


def _call_claude(system_prompt, user_prompt):
    """Call Claude with retry logic. Returns response text."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    last_err = None

    for attempt in range(1, RETRIES + 1):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return msg.content[0].text
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                print(f"Claude attempt {attempt} failed ({e}), retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

    raise last_err


# ---------------------------------------------------------------------------
# Block generation (first run / --init)
# ---------------------------------------------------------------------------

BLOCK_SYSTEM = """You are a professional cycling coach modeled on Javier Sola (Tadej Pogačar's coach).
You are data-driven, direct, and precise. You generate periodized training blocks with exact power targets.
Never use vague language. Every workout has a specific duration, zone label, and watt target.
Output only valid JSON — no commentary before or after.

## Physiological Constraints (enforce strictly)

**W′ budget for compound anaerobic sets**: When multiple efforts are joined with no rest (different power targets within one block), the total kJ above CP within that block must not exceed 18 kJ. CP ≈ FTP − 10w. Formula: Σ[(target_w − CP) × duration_s] ≤ 18,000 J per compound block. Rest between compound blocks must be ≥ 6 min when per-block W′ depletion exceeds 15 kJ.

**Rest ratio for anaerobic repeats**: For 1-min efforts above 130% FTP, minimum rest is 2 min (1:2 work:rest). 1:1 work:rest (e.g. 1min on / 1min off) is only appropriate up to ~120% FTP. Violating this means athletes will fail to hit targets by rep 3–4.

**48-hour rule covers anaerobic sessions too**: Do not schedule a VO2max session within 48 hours of a W′-depleting anaerobic session (efforts >130% FTP with meaningful duration above CP). TSB is a lagging indicator and will not capture acute neuromuscular fatigue from a single hard session — do not use TSB thresholds as the sole guard after maximal anaerobic work.

**Tempo/sweet spot duration progression**: Continuous tempo (75–90% FTP) duration must not increase by more than 15 min relative to the athlete's established maximum in a single session step. If the athlete's current max continuous Z3 is ~12 min, appropriate first steps are 20 min, then 30 min over multiple sessions — not a jump to 60–70 min. Continuous tempo >60 min is appropriate only for athletes with CTL 80+."""

BLOCK_USER_TEMPLATE = """
Generate a 5-week training block for this athlete. Today is {today}.

## Athlete Profile
{preferences}

## Current Fitness
- CTL (fitness): {ctl}
- ATL (fatigue): {atl}
- TSB (form): {tsb}
- Last week TSS: {last_week_tss}

{history_section}## Output Format
Return a JSON object exactly matching this schema:

{{
  "generated_date": "YYYY-MM-DD",
  "phase": "build",
  "block_overview": {{
    "goal": "One sentence: the primary physiological adaptation this block targets",
    "structure": "Weeks 1-2: base build, Week 3: peak intensity, Week 4: recovery, Week 5: specific",
    "key_sessions": ["Tuesday/Thursday VO2max 5x4min Z5", "Sunday long Z2 90min"],
    "tss_progression": [280, 320, 350, 210, 340],
    "physiological_rationale": "2-3 sentences explaining the science behind this block structure"
  }},
  "weeks": [
    {{
      "week_number": 1,
      "week_start": "YYYY-MM-DD",
      "phase_label": "base|build|peak|recovery",
      "target_tss": 350,
      "target_weekly_hours": 5.5,
      "sessions": [
        {{
          "day": "monday",
          "type": "rest|z2|tempo|sweet_spot|vo2max|anaerobic|race",
          "duration_min": 60,
          "zone_label": "Z2",
          "target_watts": 225,
          "target_watts_range": [194, 259],
          "environment": "flexible",
          "optional": false,
          "interval_minutes": [10, 40, 10],
          "interval_sets": [
            {{"count": 8, "duration_s": 60, "target_watts": 420, "rest_s": 60}},
            {{"count": 2, "duration_s": 180, "target_watts": 370, "rest_s": 180}}
          ],
          "description": "• Warmup: 10min Z1\\n• Main: 40min Z2 steady (194-259w)\\n• Cooldown: 10min Z1\\n• Focus: stay aerobic, no surges"
        }}
      ],
      "coaching_note": "Direct, specific note about this week's focus and physiological target"
    }}
  ],
  "block_goal": "One sentence: what this block achieves and why"
}}

Session rules:
- Plan 5 required sessions per week (Mon/Wed/Thu/Fri/Sun, skip one weekday for rest)
- Tuesday OR Thursday is the rest day — choose based on session sequencing
- Saturday is OPTIONAL — include if week needs volume, omit if recovery week. Mark optional: true
- Never plan more than 6 sessions in a week
- Never back-to-back hard sessions (VO2max/anaerobic) — always a Z1/Z2 or rest between
- Never back-to-back hard sessions across the Sat/Sun boundary if Saturday is riding
- Week 4 or 5 must be a recovery week (TSS drop ~40%)
- `interval_minutes`: REQUIRED for all non-rest sessions. List of ints, each the duration in minutes of one named segment (warmup, each work rep, each recovery, cooldown). MUST sum exactly to `duration_min`. Verify your arithmetic before outputting.
- `interval_sets`: REQUIRED for vo2max/anaerobic/sweet_spot/tempo sessions. Never omit. List every set with count, duration_s (seconds per rep), target_watts, and rest_s (recovery between reps). List each set separately if intensity or duration differs. Example: [{{"count": 5, "duration_s": 240, "target_watts": 382, "rest_s": 240}}]. Omit only for z2/z1/rest.
- `warmup_min`: REQUIRED for z2/z1 sessions. Integer number of planned warmup minutes.
- `cooldown_min`: REQUIRED for z2/z1 sessions. Integer number of planned cooldown minutes.

Zone and environment rules:
- Use zone_label (Z1–Z7) from the zone tables in the athlete profile
- Set target_watts and target_watts_range from the correct zone table
- environment: "flexible" = rider can do indoor or outdoor (most sessions)
- environment: "indoor" = precision intervals where exact watts matter (Peloton VO2max work)
- environment: "outdoor" = terrain is the point (climbing repeats, KOM segments)
- For indoor sessions, use Indoor FTP zones; for outdoor/flexible, use Outdoor FTP zones

Description format (REQUIRED):
- Always use bullet format with \\n between bullets
- Format: "• Warmup: Xmin Z1\\n• Main: [interval details]\\n• Cooldown: Xmin Z1\\n• Focus: [one tactical cue]"
- For intervals: specify sets x duration @ zone/watts with recovery
- For VO2max: "5x4min Z5 (355w+) / 4min Z1 recovery"

Session type targets:
- VO2max: 4-6x4min @ Z5 (110-120% FTP) or 8-12x1min @ Z6 (130%+ FTP) with equal rest
- Z2: 55-75% FTP, steady — the aerobic base
- Anaerobic: 6-10x30s-90s @ Z6-Z7 with 3-5x rest
- KOM target efforts: anaerobic/VO2 blend — train both systems 2x/week
"""


def _validate_plan(plan):
    """Warn to stderr if any session's interval_minutes don't sum to duration_min."""
    import sys
    for week in plan.get("weeks", []):
        for session in week.get("sessions", []):
            intervals = session.get("interval_minutes", [])
            if not intervals:
                continue
            total = sum(intervals)
            dur   = session.get("duration_min", 0)
            if dur > 0 and abs(total - dur) > 2:
                print(
                    f"WARNING: {week.get('week_start','?')} {session.get('day','?')} "
                    f"intervals sum to {total}min but duration_min={dur}",
                    file=sys.stderr,
                )


def generate_block(pmc_current, last_week_tss, block_history=None):
    """Generate a new 5-week training block. Saves to data/plan.json."""
    prefs = _load_preferences()
    today = date.today().isoformat()
    history_section = f"{block_history}\n\n" if block_history else ""

    user_prompt = BLOCK_USER_TEMPLATE.format(
        today=today,
        preferences=prefs,
        ctl=pmc_current.get("ctl", 0),
        atl=pmc_current.get("atl", 0),
        tsb=pmc_current.get("tsb", 0),
        last_week_tss=last_week_tss,
        history_section=history_section,
    )

    print("Generating training block with Claude...")
    raw = _call_claude(BLOCK_SYSTEM, user_prompt)
    plan = json.loads(_strip_markdown_json(raw))
    _validate_plan(plan)
    _save_plan(plan)
    print(f"Training block saved to {PLAN_PATH}")
    return plan


# ---------------------------------------------------------------------------
# Weekly adjustment + email content generation
# ---------------------------------------------------------------------------

WEEKLY_SYSTEM = """You are a professional cycling coach modeled on Javier Sola (Tadej Pogačar's coach).
You are data-driven, direct, and precise. No fluff. No generic encouragement.
You analyze actual performance data, adjust the training block, and write the weekly coaching email.
Output only valid JSON — no commentary before or after.

Recovery rule: Hard sessions (vo2max, anaerobic) require at least 48 hours before the next hard session.
Inspect the LAST DAY ACTUALLY RIDDEN in last week's session_comparison. If it was a hard session (vo2max or anaerobic), the following Monday must be rest or Z2 — not another hard session.

## Physiological Constraints (enforce strictly)

**W′ budget for compound anaerobic sets**: When multiple efforts are joined with no rest (different power targets within one block), the total kJ above CP within that block must not exceed 18 kJ. CP ≈ FTP − 10w. Formula: Σ[(target_w − CP) × duration_s] ≤ 18,000 J per compound block. Rest between compound blocks must be ≥ 6 min when per-block W′ depletion exceeds 15 kJ.

**Rest ratio for anaerobic repeats**: For 1-min efforts above 130% FTP, minimum rest is 2 min (1:2 work:rest). 1:1 work:rest (e.g. 1min on / 1min off) is only appropriate up to ~120% FTP. Violating this means athletes will fail to hit targets by rep 3–4.

**48-hour rule covers anaerobic sessions too**: Do not schedule a VO2max session within 48 hours of a W′-depleting anaerobic session (efforts >130% FTP with meaningful duration above CP). TSB is a lagging indicator and will not capture acute neuromuscular fatigue from a single hard session — do not use TSB thresholds as the sole guard after maximal anaerobic work.

**Tempo/sweet spot duration progression**: Continuous tempo (75–90% FTP) duration must not increase by more than 15 min relative to the athlete's established maximum in a single session step. If the athlete's current max continuous Z3 is ~12 min, appropriate first steps are 20 min, then 30 min over multiple sessions — not a jump to 60–70 min. Continuous tempo >60 min is appropriate only for athletes with CTL 80+.

**Fitness test prescription**: When the FITNESS TEST TRIGGERS section in the user prompt reports stale power curve data (>6 weeks without a max effort at a key duration) or a sustained ATL drop (>30% for >10 days), you MUST set `test_prescription` in your output. The prescription must be specific — name the format, venue/segment if applicable, and which day this week or next to target. Do NOT prescribe a test in recovery weeks or when TSB < -15. If no triggers are active, set `test_prescription` to null."""

WEEKLY_USER_TEMPLATE = """
Today is {today} (Monday). Generate this week's coaching report and update the training block.

## Week Boundary Context
Last session actually completed: {last_session_summary}

## Athlete Profile
{preferences}

## Current Fitness (PMC)
- CTL (fitness): {ctl}
- ATL (fatigue): {atl}
- TSB (form): {tsb}

## YEAR-OVER-YEAR (same week last year)
{yoy_data}

## Last Week Summary
- Total TSS: {actual_tss} (planned: {planned_tss})
- Distance: {distance_km}km
- Elevation: {elevation_m}m
- Duration: {duration_min} min
- Rides: {ride_count}

## Last Week: Planned vs Actual (Day by Day)
Note: this table always has 7 rows (Mon–Sun). "No ride" and "REST" rows are not rides.
Use ride_count ({ride_count}) and TSS ({actual_tss}) above as ground truth — do not recount from this table.
{session_comparison}

## HR / Fatigue Signals (Last 4 Weeks)
{hr_data}

## MULTI-WEEK COMPLIANCE PATTERNS (last 4 weeks)
{compliance_history}

## SEGMENT PROGRESS (KOM targets)
{segment_progress}

## ATHLETE PHENOTYPE (power-duration profile)
{phenotype}

## CP MODEL (estimated from clean outdoor efforts)
{cp_model}

## ATHLETE FEEDBACK — STRAVA RIDE DESCRIPTIONS (last 7 days)
{strava_descriptions}

## MID-WEEK CHECK-IN
{checkin_data}

## FITNESS TEST TRIGGERS
{fitness_test_triggers}

## AEROBIC EFFICIENCY + HR RESPONSE (structured signals)
{ef_hr_signals}

## RPE CALIBRATION (inferred from ride descriptions)
{rpe_signals}

## CALIBRATION GUIDANCE
When interpreting feedback, apply these rules in order:
1. Phase context first: Week 1–2 of a build block SHOULD feel manageable (7/10 effort).
   "Felt easy" in week 1 is correct calibration — do NOT adjust intensity down.
2. Only flag miscalibration after 2+ consecutive weeks of feedback inconsistent
   with expected phase exertion level.
3. If systematically too hard: reduce interval count (drop 1 rep) before dropping
   zone; do not compress recovery week timing.
4. If systematically too easy: add 1 interval rep before increasing zone/watts;
   confirm with power data first.
5. Never add load in the same week a red flag appeared. Spread changes across
   future weeks only.
6. TSB < -20 overrides all feedback — prioritize recovery regardless of feel.
7. Explain calibration reasoning in coaches_note. If no adjustment, state why briefly.

## Current Training Block
{plan_json}

## Output Format
Return a JSON object exactly matching this schema:

{{
  "updated_plan": {{ <same schema as input plan, with remaining weeks adjusted based on actuals> }},
  "email": {{
    "fitness_snapshot": {{
      "headline": "CTL {ctl} / ATL {atl} / TSB {tsb}",
      "body": "2-3 sentences: current form, trend, what it means for this week. Include power:HR ratio trend if meaningful."
    }},
    "last_week_recap": {{
      "headline": "short punchy headline about last week",
      "body": "Direct assessment: what was hit, what was missed, why it matters. Be specific about watts and TSS."
    }},
    "this_week_plan": {{
      "mon": {{
        "type": "rest|z2|vo2max|etc",
        "duration_min": 60,
        "zone_label": "Z2",
        "target_watts": 225,
        "target_watts_range": [194, 259],
        "environment": "flexible",
        "optional": false,
        "interval_minutes": [10, 40, 10],
        "interval_sets": [
          {{"count": 8, "duration_s": 60, "target_watts": 420, "rest_s": 60}},
          {{"count": 2, "duration_s": 180, "target_watts": 370, "rest_s": 180}}
        ],
        "description": "• Warmup: 10min Z1\\n• Main: 40min Z2 steady\\n• Cooldown: 10min Z1\\n• Focus: aerobic base"
      }},
      "tue": {{}},
      "wed": {{}},
      "thu": {{}},
      "fri": {{}},
      "sat": {{}},
      "sun": {{}}
    }},
    "block_progress": {{
      "headline": "Week N of 5 — Phase label",
      "body": "Where we are in the block, TSS trajectory, what's coming"
    }},
    "coaches_note": {{
      "headline": "short direct headline",
      "body": "1-2 sentences max. Specific, actionable. What to focus on this week and why."
    }},
    "test_prescription": null
  }}
}}

`test_prescription` rules:
- Set to null if NO fitness test trigger is active (the normal case).
- Set to an object only when one or more trigger conditions are present in FITNESS TEST TRIGGERS:
  - Stale 20-min data (>6 weeks): prescribe a 20-min all-out TT or max climb (match athlete's stated preference from preferences.md).
  - Stale 5-min data (>6 weeks): prescribe 2x all-out 5-min efforts with full recovery.
  - ATL drop >30% for >10 days: prescribe a ramp test (safest when fatigued) to re-anchor fitness before building load.
  - If both stale durations are triggered, address both in one prescription.
- When prescribing, be SPECIFIC: name the format, the segment/venue if known from preferences.md (Pantoll for 20-min climbs), and why we need it.
- Schema when active:
  {{
    "trigger": "stale_20min|stale_5min|stale_both|atl_drop",
    "urgency": "this_week|next_week",
    "format": "ramp_test|20min_tt|5min_efforts|max_climb",
    "headline": "short punchy headline (e.g. 'Time to test — FTP anchor needed')",
    "instruction": "Specific, direct instruction. Include venue/segment name, how many reps, when in the week."
  }}

Session rules (same as block generation):
- Plan 5 required sessions; Tuesday OR Thursday is the weekday rest day
- Saturday is optional (mark optional: true) — include if volume needed, skip if fatigue high
- Never back-to-back hard sessions; check Sat/Sun boundary
- Use zone_label (Z1–Z7) and target_watts_range from correct FTP table (indoor vs outdoor)
- Description: always bullet format "• Warmup: ...\\n• Main: ...\\n• Cooldown: ...\\n• Focus: ..."
- `interval_minutes`: REQUIRED for all non-rest sessions. List of ints, each the duration in minutes of one named segment (warmup, each work rep, each recovery, cooldown). MUST sum exactly to `duration_min`. Verify your arithmetic before outputting.
- `interval_sets`: REQUIRED for vo2max/anaerobic/sweet_spot/tempo sessions. Never omit. List every set with count, duration_s (seconds per rep), target_watts, and rest_s (recovery between reps). List each set separately if intensity or duration differs. Example: [{{"count": 5, "duration_s": 240, "target_watts": 382, "rest_s": 240}}]. Omit only for z2/z1/rest.
- `warmup_min`: REQUIRED for z2/z1 sessions. Integer number of planned warmup minutes.
- `cooldown_min`: REQUIRED for z2/z1 sessions. Integer number of planned cooldown minutes.

Adjustment rules:
- If TSB < -20: flag overreaching, reduce intensity this week
- If actual TSS < 70% of planned: investigate and adjust block downward
- If actual TSS > 110% of planned: note positive deviation, consider increasing next week
- KOM target: prioritize VO2max and anaerobic work over pure Z2 volume
- Be direct. No generic phrases like "great job" or "keep it up"
"""


def _format_segment_progress(segment_data):
    """Format segment tracker list into a plain-text string for the Claude prompt."""
    if not segment_data:
        return "  No segment data available yet."

    tier1 = [s for s in segment_data if s.get("tier") == 1]
    if not tier1:
        return "  No tracked segments yet."

    lines = []
    for seg in tier1:
        name      = seg.get("segment_name", "?")
        total     = seg.get("total_athletes", "?")
        score     = seg.get("achievability", "?")
        elapsed   = seg.get("elapsed_time_s")
        kom       = seg.get("kom_time_s")
        gap_s     = seg.get("time_gap_s")
        power_gap = seg.get("power_gap_w")
        rank      = seg.get("david_rank")
        pr_str    = f"{elapsed//60}:{elapsed%60:02d}" if elapsed else "—"
        kom_str   = f"{kom//60}:{kom%60:02d}" if kom else "—"
        rank_str  = (f"KOM (1st)" if rank == 1
                     else f"{rank}th" if rank else ">10")
        gap_str   = (f"🏆 KOM" if gap_s is not None and gap_s <= 0
                     else f"+{gap_s}s behind KOM" if gap_s else "—")
        pw_str    = f"+{power_gap}w needed" if power_gap else "—"
        lines.append(
            f"  {name}: Rank {rank_str} | PR {pr_str} | KOM {kom_str} | "
            f"Gap {gap_str} | {pw_str} | {total:,} athletes | Score {score}/10"
        )

    tier2 = [s for s in segment_data if s.get("tier") == 2]
    if tier2:
        opp = tier2[0]
        lines.append(
            f"  [Opportunity] {opp.get('segment_name','?')}: "
            f"{opp.get('effort_count',0)} efforts, Achievability {opp.get('achievability','?')}/10"
        )

    return "\n".join(lines)


def generate_weekly_email(pmc_current, last_week_actual, last_week_planned_tss,
                          session_comparison=None, hr_data=None,
                          strava_descriptions=None, checkin_data=None,
                          compliance_history=None, segment_data=None,
                          ef_data=None, interval_hr_data=None, rpe_signals=None,
                          phenotype_data=None, yoy_data=None,
                          fitness_test_triggers=None, cp_model_data=None):
    """
    Generate weekly coaching email + update plan.
    Returns email content dict.
    """
    prefs = _load_preferences()
    plan = _load_plan()
    today = date.today().isoformat()

    # Compute last session actually completed (for week boundary recovery check)
    last_session_summary = "Unknown — no session comparison available."
    if session_comparison:
        hard_types = {"vo2max", "anaerobic"}
        for day in reversed(session_comparison):
            a = day.get("actual")
            p = day.get("planned") or {}
            if a is not None:
                session_type = p.get("type", "unknown")
                day_name = day.get("day", "?").capitalize()
                day_date = day.get("date", "?")
                watts_str = f"{a.get('avg_watts', '?')}w avg"
                hardness = "HARD (vo2max/anaerobic)" if session_type in hard_types else session_type.upper()
                last_session_summary = (
                    f"{day_name} {day_date}: {hardness} | {a.get('name', 'ride')} | "
                    f"{a.get('duration_min', '?')}min | {watts_str} | TSS={a.get('tss', '?')}"
                )
                break

    # Format session comparison for prompt
    if session_comparison:
        comp_lines = []
        for day in session_comparison:
            p = day.get("planned") or {}
            a = day.get("actual")
            status = day.get("status", "")
            day_str = day["day"].capitalize()

            if p.get("type") == "rest" or (not p and not a):
                comp_lines.append(f"  {day_str}: REST")
                continue

            planned_str = "—"
            if p:
                planned_str = (f"{p.get('type','').upper()} {p.get('duration_min',0)}min "
                               f"{p.get('zone_label','')} {p.get('target_watts_range', [])}")

            actual_str = "NO RIDE"
            if a:
                base = (f"{a.get('name','')} {a.get('duration_min',0)}min "
                        f"{a.get('avg_watts',0)}w avg TSS={a.get('tss','?')}")
                istats = a.get("interval_stats")
                if istats:
                    delta = istats["pct_delta"]
                    sign = "+" if delta >= 0 else ""
                    base += (f" | intervals: {istats['efforts_detected']} efforts "
                             f"avg {istats['avg_interval_watts']}w "
                             f"(target {istats['target_watts']}w, {sign}{delta}%)")
                actual_str = base

            comp_lines.append(f"  {day_str}: Planned={planned_str} | Actual={actual_str} [{status.upper()}]")

        session_comparison_str = "\n".join(comp_lines)
    else:
        # Fallback: simple ride list
        session_comparison_str = "\n".join([
            f"  - {r['date']} {r['name']}: {r['duration_min']}min, "
            f"{r['avg_watts']}w avg, {r['tss']} TSS, "
            f"{'indoor' if r['trainer'] else 'outdoor'}, {r['elevation_m']}m elev"
            for r in last_week_actual.get("rides", [])
        ]) or "  No rides logged."

    # Format HR data for prompt
    if hr_data:
        hr_lines = [
            f"  Power:HR ratio — 4wk avg: {hr_data.get('power_hr_ratio_4wk_avg','N/A')} "
            f"| Last week: {hr_data.get('power_hr_ratio_last_week','N/A')} "
            f"| Trend: {hr_data.get('ratio_trend','N/A')}",
            f"  HR elevation flag: {hr_data.get('hr_elevation_flag', False)} "
            f"(last week avg HR: {hr_data.get('avg_hr_last_week','N/A')}bpm "
            f"vs 4wk avg: {hr_data.get('avg_hr_4wk','N/A')}bpm)",
            f"  Suffer score — 4wk avg: {hr_data.get('avg_suffer_score_4wk','N/A')} "
            f"| Last week: {hr_data.get('suffer_score_last_week','N/A')}",
        ]
        hr_data_str = "\n".join(hr_lines)
    else:
        hr_data_str = "  No HR data available."

    # Format Strava descriptions
    if strava_descriptions:
        desc_lines = [
            f"  {d['date']} — {d['ride_name']}: {d['description']}"
            for d in strava_descriptions
        ]
        strava_descriptions_str = "\n".join(desc_lines)
    else:
        strava_descriptions_str = "  None this week."

    # Format check-in data
    if checkin_data:
        checkin_lines = [
            f"  Timestamp:       {checkin_data.get('timestamp', '?')}",
            f"  Interval feel:   {checkin_data.get('interval_feel_label', '?')}",
            f"  Body feel:       {checkin_data.get('body_feel_label', '?')}",
            f"  Modifications:   {checkin_data.get('modifications') or 'None'}",
            f"  External factors:{checkin_data.get('external_factors') or 'None'}",
        ]
        checkin_data_str = "\n".join(checkin_lines)
    else:
        checkin_data_str = "  None this week."

    # Format EF + HR signals
    ef_hr_lines = []
    if ef_data:
        ef_trend_str = (f"{'+' if ef_data['ef_trend_pct'] >= 0 else ''}{ef_data['ef_trend_pct']}%"
                        if ef_data.get("ef_trend_pct") is not None else "—")
        ef_hr_lines.append(
            f"  Efficiency Factor (4wk avg): {ef_data.get('ef_recent','—')} "
            f"| Trend vs prior 4wk: {ef_trend_str} "
            f"({'aerobic adaptation' if (ef_data.get('ef_trend_pct') or 0) > 1 else 'flat/declining'})"
        )
        if ef_data.get("avg_decoupling") is not None:
            dc = ef_data["avg_decoupling"]
            dc_note = "solid aerobic base" if dc < 5 else ("marginal drift" if dc < 10 else "cardiac drift — hold Z2 intensity")
            ef_hr_lines.append(f"  Aerobic decoupling (Z2 rides): {dc}% — {dc_note}")

    if interval_hr_data:
        rec = interval_hr_data.get("recovery_60s_recent")
        rec_delta = interval_hr_data.get("recovery_delta")
        plateau = interval_hr_data.get("hr_plateau_pct")
        if rec:
            delta_str = (f" ({'+' if rec_delta >= 0 else ''}{rec_delta} vs prior 4wk)"
                         if rec_delta is not None else "")
            ef_hr_lines.append(
                f"  Interval HR recovery (60s drop avg): {rec} BPM{delta_str} "
                f"({'improving' if (rec_delta or 0) > 0 else 'stable/declining'})"
            )
        if plateau:
            plateau_note = ("reaching maximal effort" if plateau >= 92
                            else "not reaching max HR — conservative pacing or fatigue")
            ef_hr_lines.append(f"  HR plateau during intervals: {plateau}% of max — {plateau_note}")

    ef_hr_signals_str = "\n".join(ef_hr_lines) if ef_hr_lines else "  No EF/HR stream data yet."

    # Format RPE signals
    rpe_lines = []
    if rpe_signals:
        for r in rpe_signals:
            rpe_vs_pwr = r.get("rpe_vs_power") or "null"
            keywords = ", ".join(r.get("keywords", []))
            rpe_lines.append(
                f"  {r.get('date','?')} {r.get('ride_name','?')}: "
                f"RPE {r.get('inferred_rpe','?')}/5 | {r.get('sentiment','?')} | "
                f"calibration: {rpe_vs_pwr} | signals: {keywords}"
            )
    rpe_signals_str = "\n".join(rpe_lines) if rpe_lines else "  No ride descriptions with RPE signals this week."

    # Format phenotype data
    if phenotype_data and phenotype_data.get("primary") != "unknown":
        p = phenotype_data
        secondary_str = f" / secondary: {p['secondary']}" if p.get("secondary") else ""
        phenotype_str = (
            f"  Primary type: {p['primary']}{secondary_str}\n"
            f"  Best KOM targets: {p['best_kom_label']}\n"
            f"  Rationale: {p['rationale']}"
        )
    else:
        phenotype_str = "  Not computed yet — need ≥3 power curve data points."

    # Format YoY comparison
    if yoy_data:
        yoy_lines = []
        yoy_pmc = yoy_data.get("pmc")
        yoy_curve = yoy_data.get("curve")
        if yoy_pmc:
            ctl_delta = round(pmc_current.get("ctl", 0) - yoy_pmc["ctl"], 1)
            sign = "+" if ctl_delta >= 0 else ""
            yoy_lines.append(
                f"  CTL: {yoy_pmc['ctl']} → {pmc_current.get('ctl', 0)} ({sign}{ctl_delta}) "
                f"[same week {yoy_pmc['date'][:4]}]"
            )
            atl_delta = round(pmc_current.get("atl", 0) - yoy_pmc["atl"], 1)
            sign = "+" if atl_delta >= 0 else ""
            yoy_lines.append(
                f"  ATL: {yoy_pmc['atl']} → {pmc_current.get('atl', 0)} ({sign}{atl_delta})"
            )
        if yoy_curve:
            yoy_lines.append(
                f"  Power curve last year — 1min: {yoy_curve.get('s60', '?')}w | "
                f"5min: {yoy_curve.get('s300', '?')}w | "
                f"10min: {yoy_curve.get('s600', '?')}w | "
                f"20min: {yoy_curve.get('s1200', '?')}w"
            )
        yoy_data_str = "\n".join(yoy_lines) if yoy_lines else "  No prior-year data yet."
    else:
        yoy_data_str = "  No prior-year data yet (first year of tracking)."

    # Format fitness test triggers
    if fitness_test_triggers:
        trigger_lines = []
        stale = fitness_test_triggers.get("stale_durations", [])
        if stale:
            trigger_lines.append(
                f"  STALE POWER DATA: No max effort recorded at [{', '.join(stale)}] "
                f"in the last 6 weeks. CP/FTP model may be anchored on stale data."
            )
        else:
            trigger_lines.append("  Power curve freshness: OK (max effort at all key durations within 6 weeks).")
        drop_pct = fitness_test_triggers.get("atl_drop_pct")
        drop_days = fitness_test_triggers.get("atl_drop_days")
        if drop_pct is not None and drop_days is not None:
            trigger_lines.append(
                f"  ATL DROP: ATL has dropped {drop_pct}% from its 30-day peak "
                f"and has been >30% below peak for {drop_days} consecutive days. "
                f"Possible extended rest, illness, or detraining."
            )
        else:
            trigger_lines.append("  ATL trend: No sustained ATL drop detected.")

        # Case A: no clean window at all (venue problem)
        case_a = fitness_test_triggers.get("case_a_durations", [])
        if case_a:
            trigger_lines.append(
                f"  CASE A — NO CLEAN WINDOW: [{', '.join(case_a)}] — no uninterrupted "
                f"effort found in 6 weeks at these durations. Requires a route with "
                f"sustained climbing (Pantoll / Hawk Hill). Prescribe a dedicated Marin ride."
            )

        # Case B: clean window exists but low likelihood (session structure problem)
        case_b = fitness_test_triggers.get("case_b_durations", [])
        dur_lk = fitness_test_triggers.get("duration_likelihoods", {})
        if case_b:
            for label in case_b:
                # Map label to duration_s key
                dur_map = {"5-min": "300", "8-min": "480", "12-min": "720", "20-min": "1200"}
                lk_key = dur_map.get(label, "")
                lk_val = dur_lk.get(lk_key)
                lk_str = f" (likelihood={lk_val:.2f})" if lk_val is not None else ""
                trigger_lines.append(
                    f"  CASE B — LOW QUALITY: {label}{lk_str} — a clean window exists but "
                    f"came from a fatigued or low-intensity context. Prescribe a FRESH "
                    f"standalone {label} effort at the START of the next Twin Peaks / Bernal "
                    f"ride, before any other work. Do NOT add to existing intensity — replace "
                    f"the lightest planned intensity session, or defer if TSB < 0."
                )

        fitness_test_triggers_str = "\n".join(trigger_lines)
    else:
        fitness_test_triggers_str = "  No trigger data available."

    # Format CP model data
    def _prescribe_anchor(max_dur_s):
        """Return (duration_s, venue) for the next anchor prescription given current best."""
        if max_dur_s < 480:
            return min(int(max_dur_s * 1.5), 480), "Twin Peaks / Bernal"
        elif max_dur_s < 600:
            return 600, "Twin Peaks full loop"
        elif max_dur_s < 900:
            return 900, "Twin Peaks full loop"
        else:
            return 1200, "Pantoll / Hawk Hill (requires planning — 20-min graduation step)"

    if cp_model_data:
        m = cp_model_data
        k_str = f" | k={m['k_constant']}s" if m.get("k_constant") is not None else ""
        est_note = " [EXTRAPOLATED — no anchor ≥10min]" if m.get("anchors_only_short") else ""
        max_dur_min = (m.get("max_duration_used_s") or 0) // 60
        avg_lk = m.get("avg_anchor_likelihood")
        avg_lk_str = f" | avg anchor likelihood={avg_lk:.2f}" if avg_lk is not None else ""
        cp_lines = [
            f"  Model: {m['model_type']} | R²={m['r_squared']} | "
            f"{m['anchor_count']} anchors | confidence={m['confidence']} | "
            f"longest anchor={max_dur_min}min{avg_lk_str}",
            f"  CP={m['cp_watts']}w | W'={m['w_prime_kj']}kJ{k_str}",
            f"  Est. 20-min: {m['est_20min']}w{est_note} | "
            f"Est. 60-min: {m['est_60min']}w{est_note}",
        ]
        if m.get("caveat"):
            cp_lines.append(f"  ⚠ {m['caveat']}")
        if m.get("anchors_only_short"):
            max_dur_s = m.get("max_duration_used_s") or 300
            target_dur_s, venue = _prescribe_anchor(max_dur_s)
            target_dur_min = target_dur_s // 60
            cp_lines.append(
                f"  PRESCRIPTION NEEDED: To improve model accuracy, prescribe a "
                f"{target_dur_min}-min sustained effort at {venue}. Frame as CP anchor "
                f"improvement. Replace lightest planned intensity session; defer if TSB < 0."
            )
        cp_model_str = "\n".join(cp_lines)
    else:
        cp_model_str = "  No CP model yet — insufficient clean anchor data (need ≥3 points spanning ≥3× duration range)."

    user_prompt = WEEKLY_USER_TEMPLATE.format(
        today=today,
        last_session_summary=last_session_summary,
        preferences=prefs,
        ctl=pmc_current.get("ctl", 0),
        atl=pmc_current.get("atl", 0),
        tsb=pmc_current.get("tsb", 0),
        actual_tss=last_week_actual.get("tss", 0),
        planned_tss=last_week_planned_tss,
        distance_km=last_week_actual.get("distance_km", 0),
        elevation_m=last_week_actual.get("elevation_m", 0),
        duration_min=last_week_actual.get("duration_min", 0),
        ride_count=last_week_actual.get("ride_count", 0),
        session_comparison=session_comparison_str,
        hr_data=hr_data_str,
        compliance_history=compliance_history or "  No compliance history yet.",
        segment_progress=_format_segment_progress(segment_data),
        phenotype=phenotype_str,
        yoy_data=yoy_data_str,
        strava_descriptions=strava_descriptions_str,
        checkin_data=checkin_data_str,
        fitness_test_triggers=fitness_test_triggers_str,
        cp_model=cp_model_str,
        ef_hr_signals=ef_hr_signals_str,
        rpe_signals=rpe_signals_str,
        plan_json=json.dumps(
            {**plan, "weeks": [w for w in plan.get("weeks", []) if str(w.get("week_start", "")) >= today]},
            indent=2, default=str
        ) if plan else "No plan yet.",
    )

    print("Generating weekly coaching email with Claude...")
    raw = _call_claude(WEEKLY_SYSTEM, user_prompt)
    result = json.loads(_strip_markdown_json(raw))

    # Save updated plan
    updated_plan = result.get("updated_plan")
    if updated_plan:
        _validate_plan(updated_plan)
        _save_plan(updated_plan)
        print("Training block updated.")

    return result.get("email", {}), updated_plan


# ---------------------------------------------------------------------------
# Mid-week adaptive replan
# ---------------------------------------------------------------------------

REPLAN_SYSTEM = """You are a professional cycling coach. A mid-week replan has been triggered.
Your job is to make the minimum necessary adjustments to the remaining days of this week.

## Core Dampening Rules
- Do NOT rebuild the week from scratch — only change what the data clearly demands
- Prefer reducing reps or adjusting target watts ±5-10% over swapping session types
- Do not reduce remaining-week TSS by more than 15% from what was originally planned
- Do not add a hard session (vo2max/anaerobic) within 48h of another hard session
- Preserve the number of Z2 hours within ±20 min total
- Do not shift any session by more than 1 day
- If sessions were HIT or near-HIT, leave them unchanged — only react to clear MISS/PARTIAL patterns backed by power data
- If today's session is already planned as rest, do not insert training

## Physiological Constraints (enforce strictly)
**W′ budget**: Compound anaerobic blocks: Σ[(target_w − CP) × duration_s] ≤ 18,000 J per block. CP ≈ FTP − 10w.
**Rest ratio**: Efforts >130% FTP require minimum 2:1 rest:work.
**48-hour rule**: No vo2max/anaerobic within 48h of another hard session. TSB is a lagging metric — do not override this with TSB alone.
**Tempo progression**: Do not jump continuous tempo >15 min beyond athlete's established max (~12 min).

Output only valid JSON — no commentary before or after."""

REPLAN_USER_TEMPLATE = """
Today is {today} ({today_day}). Replan the remaining days of this week based on completed session data.

## Athlete Profile
{preferences}

## Current PMC
- CTL: {ctl} | ATL: {atl} | TSB: {tsb}

## Completed Sessions This Week (objective data)
{completed_sessions}

## Remaining Sessions This Week (to potentially adjust)
{remaining_sessions}

## Output Format
Return a JSON object:
{{
  "replan_reason": "1-2 sentences: what the data shows and what you're changing (be specific about watts and reps)",
  "adjusted_sessions": [
    {{
      "day": "wednesday",
      "type": "vo2max|anaerobic|z2|tempo|sweet_spot|rest",
      "duration_min": 60,
      "zone_label": "Z5",
      "target_watts": 355,
      "target_watts_range": [340, 380],
      "environment": "flexible",
      "optional": false,
      "interval_minutes": [10, 20, 10],
      "interval_sets": [{{"count": 4, "duration_s": 240, "target_watts": 355, "rest_s": 240}}],
      "description": "• Warmup: 10min Z1\\n• Main: 4x4min Z5\\n• Cooldown: 10min Z1\\n• Focus: ..."
    }}
  ]
}}

Only include days you are actually changing. Omit unchanged days.
If no changes are warranted, return an empty adjusted_sessions list with replan_reason explaining why.
Enforce all standard session schema rules (interval_sets required for vo2max/anaerobic/tempo/sweet_spot; warmup_min/cooldown_min for z2/z1; interval_minutes must sum to duration_min).
"""


def generate_replan(pmc_current, completed_sessions, remaining_sessions, descriptions=None):
    """
    Generate mid-week plan adjustments based on completed session power actuals + descriptions.
    Returns dict: {replan_reason: str, adjusted_sessions: list of session dicts}.
    """
    prefs = _load_preferences()
    today = date.today()
    today_day = today.strftime("%A")

    # Format completed sessions
    completed_lines = []
    for day in completed_sessions:
        p = day.get("planned") or {}
        a = day.get("actual")
        status = day.get("status", "")
        day_str = day.get("day", "?").capitalize()

        if p.get("type") == "rest" and not a:
            completed_lines.append(f"  {day_str}: REST (planned)")
            continue

        planned_str = (
            f"{p.get('type', '').upper()} {p.get('duration_min', 0)}min "
            f"target {p.get('target_watts', '?')}w"
        ) if p else "—"

        if a:
            istats = a.get("interval_stats") or {}
            efforts = istats.get("efforts_detected", 0)
            avg_w = istats.get("avg_interval_watts")
            target_w = istats.get("target_watts")
            pct = istats.get("pct_delta")
            interval_str = ""
            if efforts and avg_w and target_w:
                sign = "+" if pct and pct >= 0 else ""
                interval_str = (
                    f" | intervals: {efforts} efforts avg {avg_w}w "
                    f"vs target {target_w}w ({sign}{pct}%)"
                )
            completed_lines.append(
                f"  {day_str}: {planned_str} → actual {a.get('duration_min', 0)}min "
                f"{a.get('avg_watts', '?')}w avg TSS={a.get('tss', '?')}{interval_str} [{status.upper()}]"
            )
        else:
            completed_lines.append(f"  {day_str}: {planned_str} → NO RIDE [MISS]")

    # Append ride descriptions to completed sessions context
    if descriptions:
        completed_lines.append("\n  Ride descriptions:")
        for d in descriptions:
            if d.get("description"):
                completed_lines.append(f"    {d['date']} — {d['ride_name']}: {d['description']}")

    # Format remaining sessions
    remaining_lines = []
    for session in remaining_sessions:
        day_str = session.get("day", "?").capitalize()
        s_type = session.get("type", "?")
        if s_type == "rest":
            remaining_lines.append(f"  {day_str}: REST")
            continue
        isets = session.get("interval_sets")
        isets_str = ""
        if isets:
            isets_str = " | sets: " + ", ".join(
                f"{s['count']}x{s['duration_s']}s@{s['target_watts']}w"
                for s in isets
            )
        remaining_lines.append(
            f"  {day_str}: {s_type.upper()} {session.get('duration_min', 0)}min "
            f"target {session.get('target_watts', '?')}w{isets_str}"
        )

    user_prompt = REPLAN_USER_TEMPLATE.format(
        today=today.isoformat(),
        today_day=today_day,
        preferences=prefs,
        ctl=pmc_current.get("ctl", 0),
        atl=pmc_current.get("atl", 0),
        tsb=pmc_current.get("tsb", 0),
        completed_sessions="\n".join(completed_lines) or "  No sessions completed yet this week.",
        remaining_sessions="\n".join(remaining_lines) or "  No remaining sessions this week.",
    )

    print("Generating mid-week replan with Claude...")
    raw = _call_claude(REPLAN_SYSTEM, user_prompt)
    return json.loads(_strip_markdown_json(raw))


# ---------------------------------------------------------------------------
# RPE inference from ride descriptions
# ---------------------------------------------------------------------------

RPE_SYSTEM = """You are a cycling coach analyzing ride descriptions to infer perceived exertion.
Output only valid JSON — no commentary before or after."""

RPE_USER_TEMPLATE = """
Analyze these Strava ride descriptions and infer RPE (1-5 scale) for each ride.
Cross-reference with the session plan and actual power data where available.

RPE scale:
1 = Very easy / recovery
2 = Comfortable / manageable
3 = Moderate / somewhat hard
4 = Hard / pushing limits
5 = Maximal / couldn't do more

Rides and descriptions:
{descriptions}

Session power context (planned vs actual):
{power_context}

For each ride with a meaningful description, return a JSON array:
[
  {{
    "activity_id": 12345678,
    "date": "YYYY-MM-DD",
    "ride_name": "name from description",
    "inferred_rpe": 1-5,
    "sentiment": "positive|negative|neutral",
    "keywords": ["comma", "separated", "effort", "signals"],
    "rpe_vs_power": "calibrated|struggling|underpacing|null"
  }}
]

Use the activity_id exactly as provided in the input (e.g. activity_id=12345678).

rpe_vs_power values:
- "calibrated": RPE matches power delivery (hard effort + hit target, or easy + below target)
- "struggling": High RPE (4-5) but power was at/above target (mental or physical limit)
- "underpacing": Low RPE (1-2) but power well below target (holding back)
- null: no meaningful description or insufficient context

Return [] if no descriptions contain effort signals.
"""


def infer_rpe_from_descriptions(strava_descriptions, session_comparison=None):
    """
    Pre-pass: ask Claude to extract structured RPE from ride descriptions.
    Returns list of dicts or [] if no meaningful descriptions.
    """
    if not strava_descriptions:
        return []

    # Only include rides with non-trivial descriptions
    meaningful = [
        d for d in strava_descriptions
        if d.get("description") and len(d["description"].strip()) > 10
    ]
    if not meaningful:
        return []

    desc_lines = [
        f"  activity_id={d['activity_id']} {d['date']} — {d['ride_name']}: {d['description']}"
        for d in meaningful
    ]

    # Format power context
    power_lines = []
    if session_comparison:
        for day in session_comparison:
            a = day.get("actual")
            p = day.get("planned") or {}
            if a and p.get("type") not in ("rest", None):
                istats = (a or {}).get("interval_stats") or {}
                pct = istats.get("pct_delta")
                pct_str = f"{'+' if pct and pct >= 0 else ''}{pct}% vs target" if pct is not None else ""
                power_lines.append(
                    f"  {day.get('date','?')} {p.get('type','').upper()}: "
                    f"planned {p.get('target_watts','?')}w | "
                    f"actual avg {a.get('avg_watts','?')}w {pct_str} [{day.get('status','')}]"
                )

    user_prompt = RPE_USER_TEMPLATE.format(
        descriptions="\n".join(desc_lines),
        power_context="\n".join(power_lines) if power_lines else "  Not available.",
    )

    try:
        raw = _call_claude(RPE_SYSTEM, user_prompt)
        result = json.loads(_strip_markdown_json(raw))
        if isinstance(result, list):
            print(f"RPE inference: {len(result)} rides with effort signals")
            return result
    except Exception as e:
        print(f"RPE inference failed (non-fatal): {e}")

    return []


# ---------------------------------------------------------------------------
# Block transition — fitness evidence, new block generation, transition email
# ---------------------------------------------------------------------------

def assess_fitness_evidence(curve_data, ftp_outdoor):
    """
    Determine how confidently we can re-estimate FTP from recent max-effort data.

    Uses power_curve_weekly DB to find the athlete's 6-month best at each duration,
    then checks whether the most recent 4 weeks' best efforts are near-maximal
    (within 3% of that 6-month peak) — FTP-agnostic detection.

    CP model: P(t) = CP + W'/t  →  CP ≈ new FTP estimate.
    Requires ≥2 qualifying durations spanning ≥3× (e.g. 120s + 600s).

    Returns:
        {
          "scenario": "A" | "B" | "C",
          "estimated_ftp": int | None,
          "evidence_summary": str,
        }
    """
    import duckdb
    import numpy as np
    from datetime import date, timedelta
    DB_PATH_LOCAL = os.path.join(os.path.dirname(__file__), "data", "cycling_coach.duckdb")

    # Query 6-month best and recent-4-week best for each duration
    DURATIONS = [60, 120, 300, 600, 1200]
    cutoff_recent = date.today() - timedelta(weeks=4)
    cutoff_6mo    = date.today() - timedelta(weeks=26)

    six_mo_best  = {}  # dur → watts
    recent_best  = {}  # dur → watts

    try:
        conn = duckdb.connect(DB_PATH_LOCAL)
        for col, dur in [("s60", 60), ("s300", 300), ("s600", 600), ("s1200", 1200)]:
            row = conn.execute(f"""
                SELECT
                    MAX(CASE WHEN week_end >= ? THEN {col} END) AS recent,
                    MAX(CASE WHEN week_end >= ? THEN {col} END) AS six_mo
                FROM power_curve_weekly
                WHERE week_end >= ?
            """, [cutoff_recent, cutoff_6mo, cutoff_6mo]).fetchone()
            if row:
                recent_best[dur]  = row[0]
                six_mo_best[dur]  = row[1]
        conn.close()
    except Exception as e:
        return {
            "scenario": "C",
            "estimated_ftp": None,
            "evidence_summary": f"DB query failed ({e}) — ramp test recommended.",
        }

    # Identify qualifying durations: recent ≥ 97% of 6-month best
    qualifying = []
    for dur in [300, 600, 1200]:  # skip 60s (too short for CP model)
        r = recent_best.get(dur)
        s = six_mo_best.get(dur)
        if r and s and s > 0 and r >= 0.97 * s:
            qualifying.append((dur, r))

    # Also allow 120s if in current_data but not in DB (use curve_data arg as fallback)
    for dur in [120, 300, 600, 1200]:
        if dur not in [q[0] for q in qualifying]:
            val = (curve_data.get(dur) or {}).get("best")
            s   = six_mo_best.get(dur)
            if val and s and s > 0 and val >= 0.97 * s:
                qualifying.append((dur, val))

    # Remove duplicates, sort by duration
    seen = set()
    unique_q = []
    for dur, w in sorted(qualifying):
        if dur not in seen:
            seen.add(dur)
            unique_q.append((dur, w))

    # Need ≥2 durations spanning ≥3× range for a reliable CP fit
    cp_estimate = None
    if len(unique_q) >= 2:
        min_dur = unique_q[0][0]
        max_dur = unique_q[-1][0]
        if max_dur >= 3 * min_dur:
            # 2-parameter CP model: minimize sum of squared errors
            # P(t) = CP + W'/t → rearranged: P*t = CP*t + W'
            # Linear: if x=t, y=P*t → slope=CP, intercept=W'
            try:
                ts  = np.array([d for d, _ in unique_q], dtype=float)
                ps  = np.array([w for _, w in unique_q], dtype=float)
                ys  = ps * ts
                # least squares: [t | 1] * [CP; W'] = P*t
                A   = np.column_stack([ts, np.ones_like(ts)])
                result = np.linalg.lstsq(A, ys, rcond=None)
                cp_estimate = int(round(result[0][0]))
                # Sanity: CP should be between 200w and 500w
                if not (200 <= cp_estimate <= 500):
                    cp_estimate = None
            except Exception:
                cp_estimate = None

    if cp_estimate is not None:
        dur_strs = " + ".join(f"{d//60}min={w}w" for d, w in unique_q)
        return {
            "scenario": "A",
            "estimated_ftp": cp_estimate,
            "evidence_summary": (
                f"Scenario A: CP model from {len(unique_q)} recent max efforts "
                f"({dur_strs}) → FTP estimate {cp_estimate}w"
            ),
        }

    # Scenario B: some near-max efforts but not enough for reliable CP
    if unique_q or any(
        recent_best.get(d) and six_mo_best.get(d) and recent_best[d] >= 0.97 * six_mo_best[d]
        for d in [60]
    ):
        return {
            "scenario": "B",
            "estimated_ftp": None,
            "evidence_summary": (
                "Scenario B: Only short-duration max efforts in past 4 weeks "
                "(insufficient for CP model). Schedule 5–20 min max effort in week 1."
            ),
        }

    # Scenario C: no outdoor max effort data
    return {
        "scenario": "C",
        "estimated_ftp": None,
        "evidence_summary": (
            "Scenario C: No near-maximal outdoor efforts in past 4 weeks. "
            "Schedule fitness test in week 1."
        ),
    }


NEW_BLOCK_USER_TEMPLATE = """
Generate a new 5-week training block. Today is {today}. This is a BLOCK TRANSITION — not the first run.
Use the prior block data to inform the next block type, TSS ramp, and power targets.

## Athlete Profile
{preferences}

## Current Fitness (PMC)
- CTL (fitness): {ctl}  (was {ctl_block_start} at block start → {ctl_delta:+.1f} over block)
- ATL (fatigue): {atl}
- TSB (form): {tsb}

## Fitness Reassessment
{fitness_assessment}

## Prior Block Summary
{block_summary}

## Athlete Debrief
{debrief_section}

{history_section}

## Block Type Decision Rules
- If compliance ≥ 80% AND TSB > -10: progress to next phase (base→build, build→peak)
- If compliance < 80% (athlete struggled to complete sessions): repeat same phase with same/lower TSS
- If TSB < -20: start with recovery week regardless of compliance
- If motivation = "burned out" (4): insert a transition/rest week before next build
- If goal shifted: re-anchor the block goal to new objective
- If disruptions listed: account for those weeks explicitly (reduce TSS or mark optional)

## Output Format
Return the same JSON schema as always (5-week block).
Also add `kom_effort` and `ftp_test` as valid session types:
- `kom_effort`: outdoor, full-gas effort, `target_watts: null`, `zone_label: "MAX"`,
  description = "• Warmup: 15min Z1\\n• Main: [segment/climb name] all-out effort — this is
  your fitness marker for this block\\n• Cooldown: 10min Z1\\n• Focus: full gas, no pacing"
- `ftp_test` (ramp): indoor, `target_watts: null`, `zone_label: "MAX"`,
  description = "• Warmup: 10min Z1 easy spin\\n• Ramp: start at 200w, add 20w each minute
  until failure\\n• Stop when you can no longer maintain the step\\n• FTP estimate = 0.75 ×
  last completed step × 1.18 (Peloton calibration)\\n• Cool down 10min Z1"
- `ftp_test` (20-min): indoor, `target_watts: null`, `zone_label: "MAX"`,
  description = "• Warmup: 10min Z1\\n• Hard effort: 5min at sweet-spot intensity\\n•
  Recovery: 5min easy\\n• 20-MIN TEST: go as hard as you can sustain for 20 minutes\\n•
  FTP = avg_power × 0.95 × 1.18 (Peloton calibration)\\n• Cooldown: 10min Z1"

Session rules (same as always):
- Plan 5 required sessions per week (Mon/Wed/Thu/Fri/Sun, skip one weekday for rest)
- Tuesday OR Thursday is the rest day — choose based on session sequencing
- Saturday is OPTIONAL — include if week needs volume. Mark optional: true
- Never back-to-back hard sessions
- Week 4 or 5 must be a recovery week (TSS drop ~40%)
- `interval_minutes` MUST sum exactly to `duration_min`
"""


def generate_new_block(pmc_current, ctl_block_start, block_summary_str,
                       debrief, fitness_assessment, block_history_str=""):
    """
    Generate the next 5-week block at a block transition.
    Richer context than generate_block() (init): includes debrief, fitness evidence,
    prior block summary, and CTL delta over the block.
    """
    prefs = _load_preferences()
    today = date.today().isoformat()

    ctl = pmc_current.get("ctl", 0)
    atl = pmc_current.get("atl", 0)
    tsb = pmc_current.get("tsb", 0)
    ctl_delta = ctl - (ctl_block_start or ctl)

    # Format debrief section
    d = debrief or {}
    debrief_lines = [
        f"  Goal achieved:          {d.get('goal_achieved_label', '—')}",
        f"  Biggest limiter:        {d.get('biggest_limiter') or 'None reported'}",
        f"  Motivation:             {d.get('motivation_label', '—')}",
        f"  Disruptions next block: {d.get('disruptions') or 'None'}",
        f"  Goal shifted:           {'Yes — ' + d.get('goal_description','') if d.get('goal_shifted') else 'No'}",
        f"  Availability changed:   {'Yes — ' + d.get('availability_description','') if d.get('availability_changed') else 'No'}",
        f"  Injuries/concerns:      {d.get('injuries') or 'None'}",
        f"  Test preference:        {d.get('test_preference_label', '—')}",
    ]
    debrief_section = "\n".join(debrief_lines)

    history_section = f"## PRIOR BLOCK HISTORY\n{block_history_str}" if block_history_str else ""

    user_prompt = NEW_BLOCK_USER_TEMPLATE.format(
        today=today,
        preferences=prefs,
        ctl=ctl,
        ctl_block_start=ctl_block_start or ctl,
        ctl_delta=ctl_delta,
        atl=atl,
        tsb=tsb,
        fitness_assessment=fitness_assessment.get("evidence_summary", "No evidence data."),
        block_summary=block_summary_str or "  No block summary available.",
        debrief_section=debrief_section,
        history_section=history_section,
    )

    print("Generating new training block with Claude (block transition)...")
    raw = _call_claude(BLOCK_SYSTEM, user_prompt)
    plan = json.loads(_strip_markdown_json(raw))
    _validate_plan(plan)
    _save_plan(plan)
    print(f"New training block saved to {PLAN_PATH}")
    return plan


TRANSITION_EMAIL_SYSTEM = """You are a professional cycling coach modeled on Javier Sola (Tadej Pogačar's coach).
You are data-driven, direct, and precise. No fluff. No generic encouragement.
You are writing a block transition email: a summary of the completed block and an introduction to the next one.
Output only valid JSON — no commentary before or after."""

TRANSITION_EMAIL_TEMPLATE = """
Today is {today}. Write the block transition coaching email.

## Athlete Profile
{preferences}

## Completed Block Summary
{block_summary}

## Fitness Reassessment
{fitness_assessment}

## Athlete Debrief
{debrief_section}

## New Block (just generated)
{new_plan_json}

## Current PMC
- CTL: {ctl} (was {ctl_start} at block start)
- ATL: {atl}
- TSB: {tsb}

## Output Format
Return a JSON object:
{{
  "block_scorecard": {{
    "headline": "short verdict on the completed block",
    "body": "2-3 sentences: what the block achieved, compliance summary, CTL gained.
             Be direct — did the block work?"
  }},
  "power_curve_summary": {{
    "headline": "Power curve: X changes",
    "body": "Specific numbers. E.g. '5-min: 358w → 371w (+3.7%). 20-min: 318w → 323w (+1.6%).
             CP model now estimates FTP at Xw.' If no change, say so."
  }},
  "fitness_assessment": {{
    "headline": "FTP reassessment: [method]",
    "body": "How FTP was determined (or will be tested). What changes in zones if FTP updated."
  }},
  "next_block_overview": {{
    "headline": "Next block: [type] — [one-line goal]",
    "body": "What this block targets physiologically, TSS progression, key sessions.
             Why this type now given the prior block result."
  }},
  "coaches_verdict": {{
    "headline": "short direct coach assessment",
    "body": "1-2 sentences. Honest verdict: is the athlete ready for next phase?
             Any concerns? What to execute well this block."
  }}
}}
"""


def generate_block_transition_email(pmc_current, ctl_block_start,
                                    block_summary_str, debrief,
                                    fitness_assessment, new_plan):
    """
    Generate the block transition email content (separate from weekly email).
    Returns the email content dict.
    """
    prefs = _load_preferences()
    today = date.today().isoformat()

    d = debrief or {}
    debrief_lines = [
        f"  Goal achieved:   {d.get('goal_achieved_label', '—')}",
        f"  Biggest limiter: {d.get('biggest_limiter') or 'None reported'}",
        f"  Motivation:      {d.get('motivation_label', '—')}",
        f"  Injuries:        {d.get('injuries') or 'None'}",
        f"  Disruptions:     {d.get('disruptions') or 'None'}",
    ]

    user_prompt = TRANSITION_EMAIL_TEMPLATE.format(
        today=today,
        preferences=prefs,
        block_summary=block_summary_str or "  No block summary available.",
        fitness_assessment=fitness_assessment.get("evidence_summary", "No evidence data."),
        debrief_section="\n".join(debrief_lines),
        new_plan_json=json.dumps(new_plan.get("block_overview", {}), indent=2, default=str),
        ctl=pmc_current.get("ctl", 0),
        ctl_start=ctl_block_start or pmc_current.get("ctl", 0),
        atl=pmc_current.get("atl", 0),
        tsb=pmc_current.get("tsb", 0),
    )

    print("Generating block transition email with Claude...")
    raw = _call_claude(TRANSITION_EMAIL_SYSTEM, user_prompt)
    return json.loads(_strip_markdown_json(raw))
