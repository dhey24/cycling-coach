# Cycling Metrics Guide

## The Core Metrics

### FTP — Functional Threshold Power
The maximum power you can sustain for ~60 minutes. Everything else is measured relative to it.
- **Outdoor FTP: 323w** (CP model from road power data)
- **Indoor FTP: 275w** (Peloton FTP test — Peloton reads ~18% low)

These differ because the Peloton calibration doesn't match your outdoor power meter. Use indoor zones on Peloton, outdoor zones on the road.

### TSS — Training Stress Score
How hard a ride was, expressed as a single number. A 1-hour ride at exactly FTP = 100 TSS.
- Easy Z2 hour: ~40–60 TSS
- Tempo/sweet spot hour: ~70–90 TSS
- Hard VO2max session: ~80–100 TSS
- Long 3-hour Z2 ride: ~120–180 TSS
- A good week: 250–400 TSS

### NP — Normalized Power
A smarter average power that accounts for variability. A punchy ride with surges scores higher NP than a flat steady ride at the same average watts. Used in TSS calculation.

### IF — Intensity Factor
NP / FTP. An IF of 1.0 = riding at FTP. Below 0.75 = easy Z2. Above 1.05 = very hard.

---

## Performance Management Chart (PMC)

### CTL — Chronic Training Load (Fitness)
42-day exponential rolling average of daily TSS. This is your **fitness number**. It builds slowly (weeks/months) and drops slowly if you stop training.
- **Your current CTL**: check the email header
- Below 40: low fitness baseline
- 40–70: solid amateur training
- 70–100: competitive amateur / cat 3-4 level
- 100+: elite / professional range

### ATL — Acute Training Load (Fatigue)
7-day exponential rolling average of daily TSS. This is your **fatigue number**. It spikes quickly after a hard week and recovers within days.

### TSB — Training Stress Balance (Form)
**TSB = CTL − ATL**. Positive means fresh, negative means fatigued.
- **> +10**: Very fresh — good for racing or testing, but fitness may be slightly declining
- **-5 to +5**: Neutral form — normal training state
- **-10 to -20**: Fatigued — expected during a build block, manageable
- **< -20**: Overreaching zone — risk of illness/injury, back off

*Insight for you: After a hard week, your TSB will dip. That's the point — the adaptation happens during recovery. The goal is to manage depth and timing of the dip.*

---

## Power Zones

### Outdoor Zones (FTP = 323w)
| Zone | Name | Watts | % FTP | What It Feels Like |
|------|------|-------|-------|-------------------|
| Z1 | Active Recovery | < 194w | < 60% | Almost embarrassingly easy. Legs are spinning, not working. |
| Z2 | Endurance | 194–259w | 60–80% | Conversational. Could do this for hours. Aerobic engine builder. |
| Z3 | Tempo | 259–291w | 80–90% | Uncomfortable but sustainable for 20–60 min. Moderate HR. |
| Z4 | Lactate Threshold | 291–355w | 90–110% | Hard. This is FTP range. 1 hour at max effort. |
| Z5 | VO2max | 355–388w | 110–120% | Very hard. 3–8 minute efforts. The key zone for KOM hunting. |
| Z6 | Anaerobic Capacity | 388–485w | 120–150% | All-out for 30s–3min. Burns fast. |
| Z7 | Neuromuscular | > 485w | 150%+ | Sprint. 5–15 seconds. Pure power. |

### Indoor Zones (FTP = 275w, Peloton)
| Zone | Name | Watts | % FTP |
|------|------|-------|-------|
| Z1 | Active Recovery | < 165w | < 60% |
| Z2 | Endurance | 165–220w | 60–80% |
| Z3 | Tempo | 220–248w | 80–90% |
| Z4 | Lactate Threshold | 248–302w | 90–110% |
| Z5 | VO2max | 302–330w | 110–120% |
| Z6 | Anaerobic Capacity | 330–413w | 120–150% |
| Z7 | Neuromuscular | > 413w | 150%+ |

---

## HR Signals

### Power:HR Ratio
Average watts divided by average heart rate. E.g., 220w / 130bpm = 1.69 w/bpm.
- **Rising ratio over weeks** = aerobic adaptation (same HR, more power = better fitness)
- **Declining ratio** = accumulated fatigue or fitness loss

### HR Elevation Flag
If your average HR during rides is >5% above your 4-week baseline at similar power output, that's a signal of accumulated fatigue, poor recovery, or early illness. Back off.

### Suffer Score (Strava)
Strava's HR-based effort score. Higher = more cardiovascular stress. Elevated suffer scores without corresponding fitness gains = inefficient training or overreaching.

---

## What Numbers Mean For You Right Now

| Metric | Your Value | Interpretation |
|--------|-----------|----------------|
| FTP Outdoor | 323w | Strong club-level base. VO2max work will push this higher. |
| FTP Indoor | 275w | Peloton-calibrated. Use for all Peloton workouts. |
| Goal | Top 10 KOMs (1–10 min climbs) | Requires Z5–Z6 power at the start of climbs after 30–60 min riding |

**The math for KOMs**: A 5-minute climb at Z5 (355w+) after a hard approach is your target race effort. Everything in training builds toward sustaining that power after fatigue.
