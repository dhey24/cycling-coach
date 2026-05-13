# Coaching Reference (`coach.py`)

> **Maintenance:** Update when changing prompt paths, adding new Claude calls, or modifying physiological constraints. The constraints section is the canonical source — `coach.py` prompts must match it.

---

## Claude Prompt Paths

Model: `claude-opus-4-6`, max_tokens: 16,000, 3 retries with 15s backoff.

1. **`generate_block()`** (`--init`): System prompt as Javier Sola persona → outputs 5-week periodized block JSON
2. **`generate_weekly_email()`** (weekly): Current plan + last week actuals + PMC + HR signals + check-in + phenotype + YoY comparison → outputs `{updated_plan, email}` JSON
3. **`generate_new_block()` + `generate_block_transition_email()`** (`--new-block`): Enriched block transition — debrief Q&A + fitness evidence (Scenario A/B/C CP model) + compliance history → next block + transition email JSON
4. **`generate_replan()`** (`replan.py`): Mid-week only — completed session power actuals + descriptions → adjusted remaining days with dampening constraints (max −15% TSS, no session shifts >1 day, prefer watts tweaks)

---

## Coaching Constraints

These physiological rules are encoded in both `BLOCK_SYSTEM` and `WEEKLY_SYSTEM` in `coach.py`. Any change to session design — whether manual edits to `plan.json` or future Claude-generated plans — must respect them.

### W′ Budget for Compound Anaerobic Sets

When efforts at different power targets are joined with no rest (compound block), total kJ above CP within that block must not exceed 18 kJ. CP ≈ FTP − 10w.

- Formula: `Σ[(target_w − CP) × duration_s] ≤ 18,000 J` per block
- Rest between compound blocks must be ≥ 6 min when per-block depletion exceeds 15 kJ
- Example: [2min@385w + 1min@455w + 30sec@530w] = 23.3 kJ → limit to 2 blocks max, not 3

**Why:** W' is finite and non-linear in recovery. Exceeding this budget in a single block pushes into glycolytic debt that doesn't clear between sets, causing quality collapse on later reps.

### Rest Ratio for Anaerobic Repeats

For 1-min efforts above 130% FTP: minimum rest is 2 min (1:2 work:rest). 1:1 ratio is only appropriate up to ~120% FTP (≈388w).

**Why:** At >130% FTP, PCr resynthesis is the limiter. 1:1 rest at that intensity produces residual fatigue that degrades subsequent efforts and increases injury risk.

### 48-Hour Rule Covers VO2max After Anaerobic

The standard "no hard session within 48h of another hard session" applies when the prior session was W′-depleting anaerobic work (efforts >130% FTP). TSB is a lagging metric (42/7-day rolling averages) and will not capture acute neuromuscular fatigue.

**Why:** Do not use TSB thresholds as the sole guard after maximal anaerobic sessions. TSB can read positive the next day while the athlete is still neurologically fatigued from maximal sprints.

### Tempo/Sweet Spot Duration Progression

Continuous tempo (75–90% FTP) duration must not jump more than 15 min beyond the athlete's established maximum.

- Athlete's current max continuous Z3: ~12 min
- Appropriate progression: 20min → 30min → 40min across multiple sessions and blocks
- Continuous tempo >60 min is reserved for CTL 80+ athletes

**Why:** Aerobic system adaptations require progressive overload but connective tissue and mitochondrial density adapt slower than cardiovascular fitness. Large jumps in continuous tempo create injury risk and often result in quality breakdown mid-set.

### Threshold as Co-Primary Session Type (added May 2026)

Training history analysis (May 2026) revealed a significant gap between 5-min power (5.5 W/kg) and 20-min power (4.2 W/kg) — a 24% drop where 15-18% is typical. Threshold intervals (3×10min or 2×15min @ 95-105% FTP) directly target this gap and are now a co-primary hard session type alongside VO2max. Alternate threshold and VO2max as the two weekly hard sessions.

Threshold sessions count as "hard" for 48-hour spacing rules. Session type `threshold` is recognized by the matching system (`metrics.py`, `power_audit.py`, `compare_anaerobic.py`) and requires `interval_sets` in `plan.json`.

### Max Effort Test Placement (added May 2026)

Max effort tests (20-min FTP TT, 5-min anchor, 3-min anchor) are race-like efforts. Their placement is a hard structural constraint — not a scheduling preference.

**Rule: tests go in Week 1 of a new block, after the recovery week that closes the prior block. The recovery week IS the taper.**

- TSB must be ≥ 0 on test day (ideally +5 to +15). A negative-TSB test underestimates fitness by ~3–5% and miscalibrates all subsequent targets for the block.
- No hard sessions in the 4 days before any max test. One rest day is not enough.
- Do not place max effort tests mid-build (weeks 2–5 of an active block). Mid-build TSB is typically −10 to −20.
- Test-week template: prior recovery week ends (Z2 only, no intensity) → Mon rest → Tue optional priming ride (30–45min easy + 2–3 × 30s openers) → Wed test → Thu Z2 recovery → Fri first hard session of new block.
- Go/no-go gate on test morning: if resting HR >5bpm above baseline, defer and substitute threshold intervals (3×10min).
- Exception: ATL drop >30% (illness/break) → ramp test only (more robust to residual fatigue than 20-min TT).

Full freshness protocol in `preferences.md` → *Power Testing Protocol*.
