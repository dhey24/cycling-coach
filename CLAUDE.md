# Cycling Coach — Architecture Reference

> **Maintenance:** When you add/rename/remove a module, change the data flow, add a new DB table, or add a new entry point — update the relevant section here. Do not copy live values (FTP, watt targets) — reference the file they live in instead.

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
python calibrate_targets.py         # Recalibrate watt targets (also auto-called by main.py)
python calibrate_peloton.py         # First-run FTP estimation via CP model
python power_audit.py               # Per-ride interval diagnostics
python compare_anaerobic.py         # Multi-week anaerobic fade analysis
python fill_leaderboards.py         # Background leaderboard cache warming (30min LaunchAgent)
python fill_hr_streams.py           # Bulk HR stream backfill
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
| `kom_scout.py` | Segment ranking + KOM opportunity HTML report |
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

---

## DuckDB Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `pmc_daily` | CTL/ATL/TSB history | `date` (PK), `tss`, `ctl`, `atl`, `tsb` |
| `session_compliance` | Planned vs actual per session | `week_start`, `day`, `status` (HIT/PARTIAL/MISS) |
| `power_curve_weekly` | Best efforts per week | `week_end` (PK), `s60`, `s300`, `s600`, `s1200` (watts) |
| `segment_efforts` | KOM segment attempts | `date`, `segment_id`, `avg_watts`, `rank`, `tier` |
| `segment_history` | Full segment ride log | `activity_id`, `segment_id`, `avg_watts` |
| `activity_metrics` | Per-ride analysis | `activity_id` (PK), `ef`, `decoupling`, `inferred_rpe` |
| `interval_hr_stats` | Per-interval HR | `activity_id`, `interval_num`, `peak_hr`, `ramp_rate` |
| `coaching_log` | Weekly plan snapshots + email | `week_start` (PK), `plan_snapshot_before/after`, `tss_planned/actual` |
| `blocks` | Mesocycle-level record across seasons | `block_id` (PK=start_date), `end_date` (NULL=active), `ftp_start/end`, `ctl_start/end`, `compliance_pct`, debrief fields |

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

## Key Algorithms (in `metrics.py`)

- **TSS** = `(duration_s × NP² / (FTP² × 3600)) × 100`
- **CTL** (fitness): 42-day exponential rolling average of TSS
- **ATL** (fatigue): 7-day exponential rolling average of TSS
- **TSB** (form): `CTL - ATL` (positive = fresh, negative = fatigued)
- **EF** (aerobic efficiency): `NP / avg_HR` — rising EF = adaptation
- **Decoupling**: HR drift between first/second half of Z2 rides (>10% = hold intensity)
- **Session matching**: plan sessions matched to actual rides by date + type; intervals sourced from Strava laps (outdoor) or detected via power stream (indoor/fallback); each effort matched to its planned set by duration, compared to per-set target watts
- **Z2 main block**: detected dynamically via 60s rolling average ≥ 90% of Z2 floor, sustained ≥120s — filters warmup spin-ups; warmup/cooldown are excluded from pct_in_zone calculation

---

## Claude Coaching (in `coach.py`)

Three prompt paths:
1. **`generate_block()`** (`--init`): System prompt as Javier Sola persona → outputs 5-week periodized block JSON
2. **`generate_weekly_email()`** (weekly): Current plan + last week actuals + PMC + HR signals + check-in → outputs `{updated_plan, email}` JSON
3. **`generate_new_block()` + `generate_block_transition_email()`** (`--new-block`): Enriched block transition — debrief Q&A + fitness evidence (Scenario A/B/C CP model) + compliance history → next block + transition email JSON

Model: `claude-opus-4-6`, max_tokens: 16,000, 3 retries with 15s backoff.

---

## Coaching Constraints (enforced in `coach.py` prompts)

These physiological rules are encoded in both `BLOCK_SYSTEM` and `WEEKLY_SYSTEM` in `coach.py`. Any change to session design — whether manual edits to `plan.json` or future Claude-generated plans — must respect them.

**W′ budget for compound anaerobic sets**
When efforts at different power targets are joined with no rest (compound block), total kJ above CP within that block must not exceed 18 kJ. CP ≈ FTP − 10w.
- Formula: `Σ[(target_w − CP) × duration_s] ≤ 18,000 J` per block
- Rest between compound blocks must be ≥ 6 min when per-block depletion exceeds 15 kJ
- Example: [2min@385w + 1min@455w + 30sec@530w] = 23.3 kJ → limit to 2 blocks max, not 3

**Rest ratio for anaerobic repeats**
For 1-min efforts above 130% FTP: minimum rest is 2 min (1:2 work:rest). 1:1 ratio is only appropriate up to ~120% FTP (≈388w).

**48-hour rule covers VO2max after anaerobic**
The standard "no hard session within 48h of another hard session" applies when the prior session was W′-depleting anaerobic work (efforts >130% FTP). TSB is a lagging metric (42/7-day rolling averages) and will not capture acute neuromuscular fatigue — do not use TSB thresholds as the sole guard after maximal anaerobic sessions.

**Tempo/sweet spot duration progression**
Continuous tempo (75–90% FTP) duration must not jump more than 15 min beyond the athlete's established maximum. Athlete's current max continuous Z3: ~12 min. Appropriate progression: 20min → 30min → 40min across multiple sessions and blocks. Continuous tempo >60 min is reserved for CTL 80+ athletes.

---

## Adding New Features

- **New metric:** add to `metrics.py`, expose in `main.py` context dict
- **New DB table:** add to `db.py` `init_db()`, add insert/query functions there
- **New email section:** add to `email_builder.py`
- **New athlete preference/target:** add to `preferences.md` (so Claude sees it)
- **All non-critical ops in `main.py`:** wrap in try/except with logged failure

## Plan Session Schema (required fields)

Every non-rest session in `plan.json` must have:
- `interval_sets`: **required** for `vo2max`, `anaerobic`, `sweet_spot`, `tempo` — list of `{count, duration_s, target_watts, rest_s}` per set; enables per-effort target matching
- `warmup_min`, `cooldown_min`: **required** for `z2`, `z1` — used as fallback if dynamic Z2 detection fails
- `interval_minutes`: required for all non-rest sessions — must sum to `duration_min`

Coach.py prompts enforce these. When backfilling past sessions (descriptions overwritten with "ACTUAL:"), source from `screenshots/*.md` or email screenshots.
