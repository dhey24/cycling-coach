# Cycling Coach Preferences

## Goals
- Increase FTP (primary)
- VO2max development as the key lever
- Target: top 10 KOMs on local 1–10 minute climb segments
- Segments are short, punchy efforts — training should reflect this intensity profile

## Power Targets
- FTP_OUTDOOR: YOUR_FTP_OUTDOOR  (e.g. CP model estimate from outdoor power data)
- FTP_INDOOR: YOUR_FTP_INDOOR    (e.g. from trainer/Peloton FTP test)
- Peloton calibration factor: YOUR_CALIBRATION_FACTOR  (e.g. 1.18x if Peloton reads ~18% low)
- MAX_HR: YOUR_MAX_HR  (athlete confirmed)

### Power Zones — Outdoor (FTP YOUR_FTP_OUTDOOR)
| Zone | Description          | Watts                          |
|------|----------------------|--------------------------------|
| Z1   | Active Recovery      | < 60% FTP                      |
| Z2   | Endurance            | 60–80% FTP                     |
| Z3   | Tempo                | 80–90% FTP                     |
| Z4   | Lactate Threshold    | 90–110% FTP                    |
| Z5   | VO2max               | 110–120% FTP                   |
| Z6   | Anaerobic Capacity   | 120–150% FTP                   |
| Z7   | Neuromuscular        | > 150% FTP                     |

### Power Zones — Indoor / Peloton (FTP YOUR_FTP_INDOOR)
| Zone | Description          | Watts                          |
|------|----------------------|--------------------------------|
| Z1   | Active Recovery      | < 60% FTP                      |
| Z2   | Endurance            | 60–80% FTP                     |
| Z3   | Tempo                | 80–90% FTP                     |
| Z4   | Lactate Threshold    | 90–110% FTP                    |
| Z5   | VO2max               | 110–120% FTP                   |
| Z6   | Anaerobic Capacity   | 120–150% FTP                   |
| Z7   | Neuromuscular        | > 150% FTP                     |

## Training Availability
- Riding days: 5 minimum, 6 maximum per week
- Off days: max 2 per week (1 weekday + Saturday), min 1 (either a weekday OR Saturday)
- Weekday rest: 1 day, typically Tuesday or Thursday (coach chooses based on load)
- Saturday: rest or ride — flexible week to week. Mark optional sessions with "optional": true
- Sunday: always available for riding (long Z2 or key session)
- Max duration: ~65 min/day on weekdays, up to 90 min on weekends

## Coaching Style
- Data-heavy and direct — no fluff
- Specific power targets for every workout (watts, not just RPE)
- Modeled on Javier Sola (Pogačar's coach): precise, periodized, performance-first
- Call out underperformance directly; explain the physiological reason
- Flag TSB < -20 as overreaching risk
- Adjust block if weekly volume drops significantly

## Home Location (for segment discovery)
HOME_LAT: YOUR_HOME_LAT
HOME_LNG: YOUR_HOME_LNG

## Training Philosophy
- VO2max intervals are the primary FTP driver: 4–6x4min @ 110–120% FTP, or 8–12x1min @ 130%+
- Support with Z2 aerobic base (55–75% FTP) to build engine
- Short KOM efforts (1–10 min) = anaerobic/VO2 blend — train both systems
- Avoid junk miles: every session has a purpose
- Recovery weeks every 4th week (drop TSS ~40%)

## Tempo/Sweet Spot Baseline
- Max continuous Z3/tempo duration: YOUR_MAX_TEMPO_MIN min
- Current sweet spot prescription: YOUR_SWEET_SPOT_PRESCRIPTION
- Progression path: 2×20 → 2×25 → 2×30 → 3×20 → 1×45 → 1×60 (across multiple blocks)

## Physical Stats
RIDER_WEIGHT_KG: YOUR_WEIGHT_KG

## Outdoor Power Target Calibration
# Add calibrated targets here after running calibrate_targets.py
# Example format:
#
# ### 1-min anaerobic repeats
# Calibrated target: Xw  |  basis: 87% of best 60s (power curve DB)
#
# ### 4-min vo2max repeats
# Calibrated target: Xw  |  basis: 117% of FTP (115-120% VO2max zone)
