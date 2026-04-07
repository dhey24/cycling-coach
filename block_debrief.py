"""
block_debrief.py — End-of-block athlete Q&A. Run before 'python main.py --new-block'.

Saves answers to data/block_debrief.json for use in new block generation.
"""

import json
import os
from datetime import datetime

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
DEBRIEF_PATH = os.path.join(DATA_DIR, "block_debrief.json")

GOAL_ACHIEVED_OPTIONS = {
    "1": "1 - Yes (hit the primary goal)",
    "2": "2 - Mostly (partial progress toward goal)",
    "3": "3 - No (didn't move the needle)",
}

GOAL_SHIFTED_OPTIONS = {
    "1": "1 - Same (goals unchanged)",
    "2": "2 - Different (goal or priorities have shifted)",
}

AVAILABILITY_OPTIONS = {
    "1": "1 - Same (schedule unchanged for next block)",
    "2": "2 - Changed (different availability next block)",
}

MOTIVATION_OPTIONS = {
    "1": "1 - High (energized, want to train)",
    "2": "2 - Normal (no issues)",
    "3": "3 - Declining (losing motivation, going through the motions)",
    "4": "4 - Burned out (need a break)",
}

TEST_PREFERENCE_OPTIONS = {
    "1": "1 - Ramp test (Peloton: 1-min steps to failure, safer if fatigued)",
    "2": "2 - 20-min TT (better FTP signal if rested)",
    "3": "3 - Max climb effort (outdoor all-out on a 5–20 min climb)",
}


def _prompt_choice(question, options):
    print(f"\n{question}")
    for key, label in options.items():
        print(f"  {label}")
    valid = list(options.keys())
    while True:
        val = input(f"Your answer ({'/'.join(valid)}): ").strip()
        if val in options:
            return int(val), options[val]
        print(f"  Please enter {', '.join(valid[:-1])} or {valid[-1]}.")


def _prompt_text(question):
    print(f"\n{question}")
    return input("Your answer (press Enter to skip): ").strip() or None


def main():
    print("=" * 60)
    print("  End-of-Block Debrief — Cycling Coach")
    print("=" * 60)
    print("Takes ~2 minutes. Feeds into your next training block.\n")

    goal_achieved, goal_achieved_label = _prompt_choice(
        "1. Did you achieve the primary goal of this block?",
        GOAL_ACHIEVED_OPTIONS,
    )
    biggest_limiter = _prompt_text(
        "2. What was the biggest limiter this block?\n"
        "   (e.g. 'missed Fri sessions most weeks', 'travel in week 3', 'legs heavy in week 4')"
    )
    disruptions = _prompt_text(
        "3. Any upcoming disruptions in the next block?\n"
        "   (trips, races, vacation, work crunch — include approximate dates)"
    )
    goal_shifted, goal_shifted_label = _prompt_choice(
        "4. Has your primary goal shifted?",
        GOAL_SHIFTED_OPTIONS,
    )
    goal_description = None
    if goal_shifted == 2:
        goal_description = _prompt_text(
            "   Describe the new goal or priority shift:"
        )
    availability_changed, availability_label = _prompt_choice(
        "5. Training availability — same as before or changed?",
        AVAILABILITY_OPTIONS,
    )
    availability_description = None
    if availability_changed == 2:
        availability_description = _prompt_text(
            "   Describe what's changed (days, duration limits, indoor vs outdoor):"
        )
    injuries = _prompt_text(
        "6. Any injuries or physical concerns heading into the next block?"
    )
    motivation, motivation_label = _prompt_choice(
        "7. Motivation across this block overall?",
        MOTIVATION_OPTIONS,
    )
    test_preference, test_preference_label = _prompt_choice(
        "8. If a fitness test is needed next block, which format do you prefer?\n"
        "   (Used only if data doesn't have enough max-effort evidence to estimate FTP)",
        TEST_PREFERENCE_OPTIONS,
    )

    debrief = {
        "timestamp":               datetime.now().isoformat(timespec="seconds"),
        "goal_achieved":           ["yes", "mostly", "no"][goal_achieved - 1],
        "goal_achieved_label":     goal_achieved_label,
        "biggest_limiter":         biggest_limiter,
        "disruptions":             disruptions,
        "goal_shifted":            goal_shifted == 2,
        "goal_shifted_label":      goal_shifted_label,
        "goal_description":        goal_description,
        "availability_changed":    availability_changed == 2,
        "availability_label":      availability_label,
        "availability_description": availability_description,
        "injuries":                injuries,
        "motivation":              motivation,
        "motivation_label":        motivation_label,
        "test_preference":         ["ramp_test", "20min_tt", "max_climb"][test_preference - 1],
        "test_preference_label":   test_preference_label,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DEBRIEF_PATH, "w") as f:
        json.dump(debrief, f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"  Debrief saved to {DEBRIEF_PATH}")
    print(f"{'=' * 60}")
    print(f"  Goal achieved:  {goal_achieved_label}")
    print(f"  Motivation:     {motivation_label}")
    if biggest_limiter:
        print(f"  Limiter:        {biggest_limiter}")
    if disruptions:
        print(f"  Disruptions:    {disruptions}")
    if injuries:
        print(f"  Injuries:       {injuries}")
    print(f"\nNext step: python main.py --new-block [--dry-run]")


if __name__ == "__main__":
    main()
