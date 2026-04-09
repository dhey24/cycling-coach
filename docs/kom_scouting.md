# KOM Scouting Reference

> **Maintenance:** Update the Physics Model and Calibration sections whenever constants change or a new calibration run produces different results. The calibration history is the most important thing here — it's unrecoverable from code alone.

---

## Goal

Identify segments where David has a realistic shot at KOM or top-10. Two complementary tools:

- **`kom_scout.py`** — ranks segments David has already ridden by power gap to KOM/10th
- **`segment_similarity.py`** — finds unridden (or poorly-ranked) segments that match the profile of segments where David already succeeds

---

## Workflow

Run order matters — `segment_similarity.py` reads the `kom_scout` report.

```bash
python kom_scout.py                  # generates data/reports/kom_scout_YYYY-MM-DD.json + HTML
python segment_scout.py              # (optional) discover new segments → data/segment_candidates.json
python segment_similarity.py         # reads latest kom_scout report → similar_scout_YYYY-MM-DD.html
python segment_similarity.py --calibrate  # validate physics model against David's PRs (run after weight changes)
```

Reports open automatically in browser and are saved to `data/reports/`. Designed to be reviewed on iPhone.

---

## Physics Model

`segment_similarity.py` estimates the power required to match KOM and 10th-place times.
Two model variants, used in order of preference:

1. **Profile-based** (`_implied_power_profile`): Uses the segment's actual altitude stream from Strava API. Binary-searches for a constant power P such that the simulated ride time over the real elevation profile equals the target time. Cached permanently in `.cache/seg_alt_{id}.json`.

2. **Avg-grade fallback** (`_implied_power`): Used when no altitude stream is available. Less accurate on variable-grade segments.

Both models output a **range**:
- `lower_w` — gravity + rolling only (best case: tailwind, smooth surface)
- `point_w` — gravity + rolling + aero in still air (honest estimate)
- `upper_w` — gravity + rolling + 1.5× aero (light ~10 km/h headwind)

### Constants

```python
RIDER_KG       = 77      # David's weight, 170 lbs current (update if weight changes >5 lbs)
BIKE_KG        = 10      # Specialized Secteur Elite + bottle (~9.1–10 kg)
WEIGHT_KG      = 87      # total system mass (rider + bike)
CRR            = 0.005   # rolling resistance — SF streets rougher than smooth tarmac (0.004)
CDA            = 0.35    # drag area (m²), road cyclist on hoods
RHO            = 1.2     # air density kg/m³
DRIVETRAIN_EFF = 0.97    # crank-to-wheel efficiency; power meter measures crank power
```

**If David's weight changes significantly:** update `RIDER_KG`, then run `--calibrate` to check mean error.

### Grade Bias Correction

A +7% multiplier is applied to all power estimates when `avg_grade >= 12%`.

**Why:** On SF's steepest streets (15–18% grades), standing climbing and gear inefficiency increase effective resistance beyond what CRR=0.005 captures in the model. Confirmed by calibration.

---

## Calibration History

`python segment_similarity.py --calibrate` validates the physics model against David's actual PRs. Filters to local segments (≤15 km), grade ≥3%, elapsed 60–900s, with a leaderboard rank (`pr_rank` set = genuine max effort, not a default KOM from easy riding through).

### 2026-04-08 (n=63 grade≥5%, WEIGHT_KG=87, CRR=0.005)

```
Mean error: +0.2%   MAE: 7.4%   p25–p75: −5.8% to +4.8%

By grade:
  5–8%  : n=37   mean=+1.2%
  8–12% : n=21   mean=−0.0%
  12%+  : n=5    mean=−6.8%  ← triggers +7% correction

By duration:
  1–2 min   : n=24   mean=+0.7%
  2–5 min   : n=27   mean=+1.3%
  5–10 min  : n=10   mean=−2.6%
  10–15 min : n=2    mean=−6.8%
```

**Notes:**
- Segments at 3–5% grade show large positive errors (+30–88%) — these are non-maximal default KOMs from easy rides and are excluded from the calibration summary above
- All 116 local climbing segments now have altitude streams cached; `--calibrate` runs instantly
- The −6.8% at 12%+ grade was the original motivation for adding the grade correction

---

## Leaderboard Scraping

The OAuth API requires Strava Partner access for leaderboard data (returns 403). Instead, the tool scrapes the leaderboard XHR endpoint using a browser session cookie.

**Setup:** Get `_strava4_session` cookie from browser DevTools → Application → Cookies → www.strava.com. Set in `.env` as `STRAVA_WEB_COOKIE`. Expires when Strava invalidates the session (days to weeks).

**Known behavior:** Strava appends the authenticated user's own leaderboard row at the bottom of the response when they're not in the top 10. The code strips this entry by matching against David's PR time. This means `pr_rank` values in the detail cache may be stale (Strava doesn't update them) — the strip works by time match, not rank.

**Leaderboard suspect flag** (`⚠ lb?` in report): fires when spread >40% (implausible for a real competitive segment) or fewer than 8 entries (too thin to trust).

**Cache:** Leaderboard times cached 7 days in `.cache/seg_top10_{id}.json`. Altitude streams cached indefinitely (segment geometry never changes).

---

## Proven Segment Detection

Authoritative source: David's Strava leader pages, scraped on each run:
- `https://www.strava.com/athletes/11887293/segments/leader` — KOMs
- `https://www.strava.com/athletes/11887293/segments/leader?top_tens=true` — top-10s

Falls back to kom_scout report ranks if the cookie is expired.

Proven segments are filtered to local climbing segments (≤15 km, grade ≥3%, elapsed ≤900s) to exclude endurance routes and non-SF segments (Mallorca, Marin, marina loops).

---

## Report Interpretation

| Column | What it shows |
|--------|--------------|
| **KOM Time** | Fastest time on the segment (competitive benchmark) |
| **David's PR** | David's best effort: watts, time, rank, model validation error % |
| **KOM Power Needed** | `point [lower–upper]w @ KOM time [zone] / David: Xw → gap` |
| **10th Power Needed** | Same format; `(est)` if 10th-place time wasn't scraped |

**Zone badges:** Anaerobic (<2 min, ±10–15% day-to-day), VO2max (2–8 min, ~5–8% variability), Threshold (>8 min, stable — gap requires training weeks to close).

**Gap colors:** green = David already beats it; amber = within 8%; grey = harder gap.

**Similarity score:** euclidean distance in standardized [grade, log(duration), log(athletes)] space. <1.0 = very close match; <2.0 = similar; higher = diverges.
