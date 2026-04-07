# Coaching History Persistence

## What was added and why

When a new 5-week training block is generated, Claude had zero memory of prior blocks — what the block goal was, what adaptations were made mid-block, or what recurring athlete patterns emerged (e.g. consistently missing Thursdays, VO2max sessions repeatedly scaled back due to overreach). Each block started cold.

These changes give Claude persistent context across blocks.

---

## 1. Plan archiving + atomic write (`coach.py`)

`_save_plan()` now:
- Copies `data/plan.json` to `data/plans/plan_YYYY-MM-DD.json` before overwriting — every pre-adjustment plan is preserved
- Writes atomically (temp file → `os.replace()`) to prevent corrupt JSON on crash

---

## 2. `coaching_log` table (`db.py`)

New DuckDB table, one row per week:

| Column | What it stores |
|---|---|
| `week_start` | PK — the Monday of the logged week |
| `plan_snapshot_before` | Full plan JSON as it was at start of Monday's run |
| `plan_snapshot_after` | Full plan JSON after Claude's weekly adjustment |
| `email_json` | Complete coaching email content dict |
| `coaches_note` | Extracted `coaches_note.body` for easy querying |
| `tss_planned` / `tss_actual` | Week's load compliance |
| `tsb_at_week_start` | Fatigue state at time of generation |

**Intentionally excluded:** `session_comparison_json` — that data already lives in the `session_compliance` table (normalized, queryable via `compliance_trends()`). No duplication.

---

## 3. `_diff_plan_snapshots()` + `block_history_summary()` (`db.py`)

`_diff_plan_snapshots(before_json, after_json)` compares future weeks (skips week 1, already completed) and returns strings like:
```
Wk3 thursday: vo2max 355w ↓ 320w
Wk4 wednesday: vo2max → z2
```
Only flags session type changes and watt changes ≥10w (ignores noise).

`block_history_summary(blocks=2)` queries `coaching_log` for the last ~8 weeks and returns a formatted string for Claude prompts showing:
- Block goal (from `block_overview.goal` of oldest snapshot in the window)
- Per-week: TSS compliance %, coach note, plan adjustments made

Returns `""` if fewer than 2 rows (graceful no-op on first runs).

**Key distinction from `compliance_trends()`:** `compliance_trends()` covers session-level weekly patterns (VO2max hit 75% of the time, etc.) and is already passed to `generate_weekly_email()`. `block_history_summary()` covers block-level progression — what the training goals were and how Claude adjusted them over multi-week arcs — and is fed only into `generate_block()`.

---

## 4. Wiring (`main.py`)

- `block_history_summary()` called before the init/weekly branch (non-fatal try/except)
- `plan_before_snapshot = current_plan` captured before `generate_weekly_email()` runs
- Both `generate_weekly_email()` call sites updated to unpack tuple return: `email_content, updated_plan = ...`
- `insert_coaching_log()` called after both branches converge, before email rendering (non-fatal)

---

## What this means for block transitions

When `generate_block()` is called (via `--init`), it now receives `block_history` as a prompt section showing what happened in prior blocks. Claude generating the next block can see:

- What the prior block was trying to achieve physiologically
- Whether TSS targets were consistently hit or missed
- Specific sessions that were repeatedly adjusted down (overreach signals)
- Weeks where TSB was very negative (recovery debt)

The `data/plans/` archive also provides a full audit trail of every pre-adjustment plan if you need to reconstruct what was originally prescribed vs what actually ran.

---

## Files changed

- `coach.py` — `_save_plan()`, `generate_block()`, `generate_weekly_email()` return value
- `db.py` — `coaching_log` DDL, `insert_coaching_log()`, `_diff_plan_snapshots()`, `block_history_summary()`
- `main.py` — history fetch, before-snapshot capture, tuple unpack, log write
