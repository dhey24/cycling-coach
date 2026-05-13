# Algorithms Reference (`metrics.py`)

> **Maintenance:** Update this file when changing any algorithm, threshold, or tunable parameter. Include *why* the value was chosen and any known shortcomings — that context is more valuable than the formula alone.

---

## Basic Training Load Metrics

- **TSS** = `(duration_s × NP² / (FTP² × 3600)) × 100`
- **CTL** (fitness): 42-day exponential rolling average of TSS
- **ATL** (fatigue): 7-day exponential rolling average of TSS
- **TSB** (form): `CTL - ATL` (positive = fresh, negative = fatigued)
- **EF** (aerobic efficiency): `NP / avg_HR` — rising EF = adaptation
- **Decoupling**: HR drift between first/second half of Z2 rides (>10% = hold intensity)

---

## Session Matching

Plan sessions matched to actual rides by date + type. Intervals sourced from Strava laps (outdoor) or detected via power stream (indoor/fallback). Each effort matched to its planned set by duration, compared to per-set target watts.

---

## Z2 Main Block Detection

Detected dynamically via 60s rolling average ≥ 90% of Z2 floor, sustained ≥120s. Filters warmup spin-ups. Warmup/cooldown are excluded from `pct_in_zone` calculation.

---

## Power Phenotype (`compute_power_phenotype()`)

Classifies athlete type from two ratios:
- **P60/P300** (anaerobic index): >1.25 = anaerobic
- **P1200/P600** (threshold flatness): >0.94 = threshold, <0.92 = VO2max

Also estimates CP and W' from the CP model using 5-min and 20-min anchors. Used to badge KOM segments by duration match in `kom_scout.py` and inform coaching prompts.

**Neuromuscular contamination (Apr 2026):** David has a powerlifting background producing elevated PCr/neuromuscular power at short durations — his 60s/FTP ratio is ~161% vs the typical 130–145% for endurance athletes. Including sub-2-min efforts in the CP fit inflates W' and deflates CP. **Fit CP model from 2-min+ anchors only.** The 60s best is reported separately as a neuromuscular characterization but excluded from the curve fit. If it sits >15% above the fitted curve, that is expected, not an error.

---

## Clean Window Detection (`_is_clean_window()`)

Two-layer filter to reject non-maximal power windows:

- **Layer 1:** Rejects windows where >5% of seconds are below 25% FTP (hard stops, red lights)
- **Layer 2:** Rejects windows where any 15s sub-window drops below 70% of window mean (sprint-then-collapse)

Parameters are tunable without schema changes.

---

## Max Effort Likelihood (`_max_effort_likelihood()`)

0–1 score layered on top of the clean flag. The clean flag is a necessary condition; likelihood is a quality modifier.

Combines two signals:
1. **`_hr_effort_score()`**: fraction of last 40% of window above duration-specific HR threshold:
   - 2–8min: 88% max HR
   - >8min: 85% max HR
   - Returns `None` for windows <150s (too short to be meaningful)
2. **`_preceding_tss()` penalty multiplier**: discounts efforts with high prior fatigue:
   - <30 TSS → 1.0
   - 30–60 TSS → 0.85
   - 60–90 TSS → 0.65
   - >90 TSS → 0.45

**Known shortcoming:** Likelihood threshold for CP model inclusion (Case B fires when `s{d}_likelihood < 0.4`) needs calibration against known efforts post-launch. The 0.4 cutoff is a starting estimate.

---

## Extended Power Curve (`power_curve_extended()`)

Scans outdoor rides for clean windows at 9 durations (60–1200s). Tracks two candidates per duration:
- **`best_display`**: highest raw clean watts — used for email/DB
- **`best_model`**: highest watts×likelihood — used for CP fitting

Accepts `fetch_hr_fn` and `max_hr` parameters.

**Why two candidates?** A record power may have come from a partially-fatigued effort or with tail-wind assist. The model candidate prioritizes quality for curve fitting, while display keeps the true best for reporting.

---

## CP Model Fitting (`fit_cp_model()`)

Fits 3-parameter model: `P(t) = CP + W'/(t+k)` via `scipy.optimize.curve_fit` with likelihood-weighted residuals (`sigma=1/sqrt(likelihood)`).

- Falls back to weighted 2-parameter linear model if 3-param fails
- Requires ≥3 anchors spanning ≥3× duration ratio
- Returns: confidence (high/moderate/low), R², `avg_anchor_likelihood`, `anchors_only_short` flag, estimated 20-min/60-min power

**Confidence levels:** high = R²≥0.95 + avg_likelihood≥0.6; moderate = R²≥0.85; low = otherwise.

**Known shortcoming:** The `k` constant in the 3-param model can overfit when anchors cluster in a narrow duration range. `anchors_only_short` flag is set when all anchors are <5min — treat such fits as indicative only.

---

## Segment Similarity (`segment_similarity.py`)

Nearest-neighbor similarity search surfacing segments matching David's proven top-10 KOM profile. Uses standardized Euclidean distance on `[grade, log(duration), log(athletes)]`.

Physics model estimates KOM/10th-place power:
- Profile-based (preferred) + avg-grade fallback
- Constants: `WEIGHT_KG=87` (77 rider + 10 bike), `CRR=0.005`, +7% grade correction at ≥12%
- `--calibrate` runs model validation against all local PRs with cached streams
