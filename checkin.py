"""
checkin.py — Mid-week check-in script. Run manually before Monday's main.py.

Saves answers to data/checkin.json for Claude to read during weekly analysis.
"""

import json
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CHECKIN_PATH = os.path.join(DATA_DIR, "checkin.json")

INTERVAL_FEEL_OPTIONS = {
    "1": "1 - Too easy (more in the tank, could have gone harder)",
    "2": "2 - About right (appropriately challenging)",
    "3": "3 - Hard but doable (pushed through, completed all sets)",
    "4": "4 - Couldn't complete (cut reps, bailed early)",
}

BODY_FEEL_OPTIONS = {
    "1": "1 - Fresh (legs feel good, motivated)",
    "2": "2 - Normal (nothing notable)",
    "3": "3 - Tired (heavy legs, lower motivation)",
    "4": "4 - Wrecked (seriously fatigued, struggling)",
}


def _prompt_choice(question, options):
    print(f"\n{question}")
    for key, label in options.items():
        print(f"  {label}")
    while True:
        val = input("Your answer (1-4): ").strip()
        if val in options:
            return int(val), options[val]
        print("  Please enter 1, 2, 3, or 4.")


def _prompt_text(question):
    print(f"\n{question}")
    return input("Your answer (press Enter to skip): ").strip() or None


def main():
    print("=" * 55)
    print("  Weekly Check-In — Cycling Coach")
    print("=" * 55)
    print("This takes ~60 seconds. Answers feed into Monday's coaching email.\n")

    interval_feel, interval_feel_label = _prompt_choice(
        "1. How did your hard/interval sessions feel this week?",
        INTERVAL_FEEL_OPTIONS,
    )
    body_feel, body_feel_label = _prompt_choice(
        "2. How are your legs/body feeling right now?",
        BODY_FEEL_OPTIONS,
    )
    modifications = _prompt_text(
        "3. Did you modify or skip any sessions? (e.g. 'cut last 2 intervals Thu')"
    )
    external_factors = _prompt_text(
        "4. Any external recovery factors? (sleep, illness, stress, travel)"
    )

    checkin = {
        "timestamp":           datetime.now().isoformat(timespec="seconds"),
        "interval_feel":       interval_feel,
        "interval_feel_label": interval_feel_label,
        "body_feel":           body_feel,
        "body_feel_label":     body_feel_label,
        "modifications":       modifications,
        "external_factors":    external_factors,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHECKIN_PATH, "w") as f:
        json.dump(checkin, f, indent=2)

    print(f"\nCheck-in saved to {CHECKIN_PATH}")
    print(f"  Interval feel:  {interval_feel_label}")
    print(f"  Body feel:      {body_feel_label}")
    if modifications:
        print(f"  Modifications:  {modifications}")
    if external_factors:
        print(f"  External:       {external_factors}")


if __name__ == "__main__":
    main()
