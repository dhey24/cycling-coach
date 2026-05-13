# Cycling Coach — Architecture Reference

> **Maintenance:** When you add/rename/remove a module, change the data flow, add a new DB table, or add a new entry point — update the relevant section here. Do not copy live values (FTP, watt targets) — reference the file they live in instead.
>
> Deep-dive references: [Algorithm details](docs/algorithms.md) · [Coaching prompts & constraints](docs/coaching.md) · [KOM scouting & physics model](docs/kom_scouting.md)

---

## System Overview

Automated weekly cycling coaching system. Every Monday at 6:30am, it fetches the athlete's Strava rides, computes training load metrics (TSS/PMC), feeds everything to Claude (Opus 4.6), and sends an HTML email with an adjusted 5-week training plan.

---

## Tech Stack

- **Language:** Python 3.9, virtualenv bootstrapped by `run.sh`
- **Key libs:** `anthropic`, `duckdb`, `scipy`, `numpy`, `requests`, `python-dotenv`
- **Scheduling:** macOS LaunchAgents (`.plist` files)
- **Email:** iCloud SMTP
- **Database:** DuckDB (embedded, `data/cycling_coach.duckdb`)

---

## Entry Points

```bash
python main.py                      # Weekly run (Mon 6:30am via LaunchAgent)
python main.py --init               # First run: generate initial 5-week block
python main.py --new-block          # Block transition: close block, generate next one
python main.py --dry-run            # Generate email, don't send (outputs to /tmp)
python main.py --date=2026-03-30    # Override date for testing

python auth.py                      # One-time Strava OAuth setup
python checkin.py                   # Mid-week athlete questionnaire (manual)
python block_debrief.py             # End-of-block Q&A (run before --new-block)
python kom_scout.py                 # KOM opportunity report
python segment_scout.py             # Discover new segments via Strava Explore API
python segment_similarity.py        # Find segments similar to David's proven top-10 KOM efforts
python segment_similarity.py --calibrate  # Physics model calibration against David's PRs (streams cached)
python calibrate_targets.py         # Recalibrate watt targets (also auto-called by main.py)
python calibrate_peloton.py         # First-run FTP estimation via CP model
python power_audit.py               # Per-ride interval diagnostics
python compare_anaerobic.py         # Multi-week anaerobic fade analysis
python fill_leaderboards.py         # Background leaderboard cache warming (30min LaunchAgent)
python fill_hr_streams.py           # Bulk HR stream backfill
python replan.py                    # Mid-week adaptive replan (run after a bad session)
python replan.py --dry-run          # Preview replan without saving
python match_audit.py               # Planned vs actual session matching audit (last 4 weeks)
python match_audit.py --weeks=8     # Longer lookback
python match_audit.py --label       # Interactive: assign activity IDs to unmatched sessions
python training_history.py          # Multi-year training analysis report (HTML + charts + coach interpretation)
python training_history.py --no-coach  # Skip Claude coaching interpretation
python training_history.py --from=2022 # Override start year
```

---

## Module Map

| File | Purpose |
|------|---------|
| `main.py` | Orchestrator: fetch → compute → coach → email; also `run_new_block()` for transitions |
| `coach.py` | Claude API calls; block generation, weekly adjustments, block transitions |
| `block_debrief.py` | End-of-block athlete Q&A (goals, limiter, disruptions, test preference) |
| `metrics.py` | All math: TSS, CTL/ATL/TSB, power curves, HR analysis, session matching |
| `db.py` | DuckDB schema + all queries |
| `strava_client.py` | Strava API client, OAuth refresh, caching, rate limiting |
| `email_builder.py` | HTML email rendering (PMC, sessions, zones, segments) |
| `email_sender.py` | iCloud SMTP send + error notification emails |
| `calibrate_targets.py` | Analyze recent sessions → update watt targets in preferences.md |
| `fill_leaderboards.py` | Batch leaderboard cache warming (runs every 30min) |
| `replan.py` | Mid-week adaptive replan: reads completed-session power actuals + Strava descriptions → Claude adjusts remaining days with dampening rules |
| `kom_scout.py` | Segment ranking + KOM opportunity HTML report; badges segments by phenotype match |
| `segment_similarity.py` | Nearest-neighbor similarity search: surfaces segments matching David's proven top-10 profile using standardized euclidean distance on [grade, log(duration), log(athletes)] |
| `match_audit.py` | Planned vs actual session matching audit + interactive labeling; loads archived plans from `data/plans/` |
| `training_history.py` | Multi-year training analysis: 8 charts + HTML report + Claude coaching interpretation + markdown summary |
| `preferences.md` | **Source of truth for athlete config** — FTP, zones, HR, goals, home lat/lng |

---

## Data Flow

**Weekly (automated):**
```
LaunchAgent (Mon 6:30am)
  └─ main.py
       ├─ strava_client.py   fetch 8-week activities + power/HR streams (cached)
       ├─ metrics.py         TSS, CTL/ATL/TSB, power curves, HR analysis, session matching
       ├─ db.py              persist PMC, compliance, activity metrics, segments
       ├─ calibrate_targets.py  (optional) update watt targets in preferences.md
       ├─ coach.py           Claude: load plan.json + metrics → adjusted plan + email JSON
       ├─ email_builder.py   render HTML email
       └─ email_sender.py    send via iCloud SMTP → davidhey@me.com
```

**Block transition (manual, every ~5 weeks):**
```
block_debrief.py             # interactive Q&A → data/block_debrief.json
main.py --new-block
  ├─ [same data collection as weekly]
  ├─ coach.assess_fitness_evidence()   Scenario A/B/C: CP model or schedule test
  ├─ db.close_block()                  close active block row with end metrics + debrief
  ├─ coach.generate_new_block()        Claude: debrief + compliance + power curve → new plan
  ├─ db.open_block()                   open new block row
  ├─ coach.generate_block_transition_email()
  ├─ email_builder.build_transition_email()
  └─ email_sender.send_email()
```

Non-critical steps (DB writes, HR analysis, power curves, segments) are wrapped in try/except — failures are logged but don't block the email send.

---

## Key Data Files

| Path | Purpose | Updated by |
|------|---------|------------|
| `data/plan.json` | Current 5-week training block | `coach.py` each week |
| `screenshots/*.md` | Structured session data extracted from past email screenshots | Manual (used to backfill `interval_sets` for weeks where descriptions were overwritten) |
| `data/plans/plan_YYYY-MM-DD.json` | Archive of all plans | `coach.py` |
| `data/activities.json` | Cached Strava activities (8-week window) | `strava_client.py` |
| `data/checkin.json` | Mid-week athlete feedback | `checkin.py` (manual) |
| `data/block_debrief.json` | End-of-block Q&A (consumed by `--new-block`, then archived) | `block_debrief.py` (manual) |
| `data/block_debrief_YYYY-MM-DD.json` | Archived debriefs | `main.py --new-block` |
| `data/plans/plan_transition_YYYY-MM-DD.json` | Pre-transition plan archives | `main.py --new-block` |
| `data/cycling_coach.duckdb` | Time-series metrics DB | `db.py` |
| `.cache/stream_{id}.json` | Per-activity power streams | `strava_client.py` (never expire) |
| `.cache/hr_{id}.json` | Per-activity HR streams | `strava_client.py` (never expire) |
| `.cache/detail_{id}.json` | Activity detail: `description`, `segment_efforts`, `laps` | `strava_client.py` |
| `.cache/seg_stats_{id}.json` | Segment leaderboard data | `fill_leaderboards.py` |
| `preferences.md` | **Athlete config** (FTP, zones, goals, home lat/lng) | Human + `calibrate_targets.py` |
| `data/match_overrides.json` | Manual session→ride pairings that bypass the matching algorithm | `match_audit.py --label` or hand-edited |

---

## DuckDB Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `pmc_daily` | CTL/ATL/TSB history (full history — not rolling window) | `date` (PK), `tss`, `ctl`, `atl`, `tsb` |
| `session_compliance` | Planned vs actual per session | `week_start`, `day`, `status` (HIT/PARTIAL/MISS), `activity_id` (Strava ID of matched ride) |
| `power_curve_weekly` | Best efforts per week | `week_end` (PK), `s60`, `s300`, `s600`, `s1200` (watts) |
| `segment_efforts` | KOM segment attempts | `date`, `segment_id`, `avg_watts`, `rank`, `tier` |
| `segment_history` | Full segment ride log | `activity_id`, `segment_id`, `avg_watts` |
| `activity_metrics` | Per-ride analysis | `activity_id` (PK), `ef`, `decoupling`, `inferred_rpe` |
| `interval_hr_stats` | Per-interval HR | `activity_id`, `interval_num`, `peak_hr`, `ramp_rate` |
| `coaching_log` | Weekly plan snapshots + email | `week_start` (PK), `plan_snapshot_before/after`, `tss_planned/actual` |
| `blocks` | Mesocycle-level record across seasons | `block_id` (PK=start_date), `end_date` (NULL=active), `ftp_start/end`, `ctl_start/end`, `compliance_pct`, debrief fields |
| `power_curve_extended` | Best *clean* efforts at 9 durations (1–20min) | `week_end` (PK), `s60`–`s1200` (watts), `s60_likelihood`–`s1200_likelihood` (0–1 max effort quality) |
| `cp_model_weekly` | Fitted Critical Power model per week | `week_end` (PK), `cp_watts`, `w_prime_kj`, `k_constant`, `model_type`, `r_squared`, `confidence`, `avg_anchor_likelihood` |

---

## Configuration

**Athlete config (FTP, zones, HR, targets, home location):** `preferences.md`
- FTP values updated by: `calibrate_targets.py` (weekly targets), `metrics.update_ftp()` (block transition Scenario A CP model estimate)
- Never hardcode these values elsewhere

**Secrets (API keys, SMTP credentials):** `.env`
- Template: `.env.example`

**Claude model + token limits:** constants at top of `coach.py`

**Strava rate limiting:** `strava_client.py` (200 req/15min, 0.5s delay)

---

## Scheduling (LaunchAgents)

| Plist | Schedule | Command |
|-------|---------|---------|
| `com.davidhey.cycling-coach.plist` | Monday 6:30am | `run.sh` → `main.py` |
| `com.davidhey.leaderboard-fill.plist` | Every 30 minutes | `fill_leaderboards.py` |

Logs: `~/Library/Logs/cycling-coach/`

---

## Adding New Features

- **New metric:** add to `metrics.py`, expose in `main.py` context dict
- **New DB table:** add to `db.py` `init_db()`, add insert/query functions there
- **New email section:** add to `email_builder.py`
- **New athlete preference/target:** add to `preferences.md` (so Claude sees it)
- **All non-critical ops in `main.py`:** wrap in try/except with logged failure

---

## Block Planning Rules — Testing

Max effort tests (FTP TT, 5-min anchor, 3-min anchor) have hard placement constraints. The coach prompt in `coach.py` must enforce these when generating or modifying plans:

1. **Tests go in Week 1 of a new block, not mid-build.** The recovery week that closes the prior block is the taper. Placing a test mid-build (e.g., Week 3–4 of an active block) produces a fatigued number that miscalibrates targets for the rest of the block.
2. **TSB must be ≥ 0 on test day, ideally +5 to +15.** If the computed TSB at the scheduled test day is negative, move the test or extend the recovery week.
3. **No hard sessions in the 4 days before any max test.** One rest day is insufficient. This is a structural rule, not an athlete-feel heuristic.
4. **The recovery week IS the taper.** Design the prior block's final week(s) so that TSB is positive by the time the test arrives — not by adding rest days mid-build.

Full freshness requirements and the test-week structure template are in `preferences.md` → *Power Testing Protocol*.

---

## Plan Session Schema (required fields)

Every non-rest session in `plan.json` must have:
- `interval_sets`: **required** for `vo2max`, `anaerobic`, `sweet_spot`, `tempo`, `threshold` — list of `{count, duration_s, target_watts, rest_s}` per set; enables per-effort target matching
- `warmup_min`, `cooldown_min`: **required** for `z2`, `z1` — used as fallback if dynamic Z2 detection fails
- `interval_minutes`: required for all non-rest sessions — must sum to `duration_min`

Coach.py prompts enforce these. When backfilling past sessions (descriptions overwritten with "ACTUAL:"), source from `screenshots/*.md` or email screenshots.
