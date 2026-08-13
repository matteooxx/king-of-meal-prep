"""Mifflin-St Jeor BMR + activity multiplier + goal modifier + protein floor.

Inputs come from db.user_profile. Outputs are stamped back into the same row
(rest_kcal_target / rest_protein_g / rest_carbs_g / rest_fat_g) so the
planner can read a single source of truth on every plan run. Training-day
deltas live in the same row; per-meal-slot training adjustments live in
settings_kv.training_delta_per_slot if the user opts in.

References:
  Mifflin MD et al., "A new predictive equation for resting energy expenditure
  in healthy individuals", Am J Clin Nutr 1990 — the modern default.

  Helms ER et al., "Evidence-based recommendations for natural bodybuilding
  contest preparation: nutrition and supplementation", J Int Soc Sports Nutr
  2014 — protein floor at 1.6-2.2 g/kg for active adults.
"""
from __future__ import annotations

from typing import TypedDict


ACTIVITY_MULTIPLIERS = {
    "sedentary":   1.20,   # desk job, no training
    "light":       1.375,  # 1-3 sessions/week
    "moderate":    1.55,   # 3-5 sessions/week
    "active":      1.725,  # 6-7 sessions/week
    "very_active": 1.90,   # twice-a-day or hard physical labor
}

GOAL_KCAL_DELTA = {
    "cut":      -400,   # ~0.4 kg/week deficit
    "maintain":    0,
    "bulk":     +300,   # gentle surplus, lean gain
}

# Protein floor in g/kg of body weight per goal. Cutting → higher (preserves
# lean mass under deficit); bulking → lower bound is fine, the surplus
# already feeds growth.
PROTEIN_PER_KG = {
    "cut":      2.2,
    "maintain": 1.8,
    "bulk":     1.7,
}

# Fat floor as % of total kcal to keep hormonal function healthy on a cut.
FAT_PCT_FLOOR = 0.25  # 25% of total kcal


class Targets(TypedDict):
    bmr: int
    tdee: int
    rest_kcal: int
    rest_protein_g: int
    rest_carbs_g: int
    rest_fat_g: int


def mifflin_st_jeor(weight_kg: float, height_cm: float, age_years: int, sex: str) -> float:
    """BMR in kcal/day. Sex must be 'm' or 'f'."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age_years
    return bmr + (5 if sex == "m" else -161)


def compute(profile: dict) -> Targets:
    """Compute resting-day targets from a user_profile row.

    Caller is responsible for handling missing fields (we raise KeyError if
    profile doesn't have the required stats; the wizard guarantees they're
    set before this is called).
    """
    weight = float(profile["weight_kg"])
    height = float(profile["height_cm"])
    age = int(profile["age_years"])
    sex = profile["sex"]
    activity = profile.get("activity_level", "moderate")
    goal = profile.get("goal", "maintain")

    bmr = mifflin_st_jeor(weight, height, age, sex)
    tdee = bmr * ACTIVITY_MULTIPLIERS.get(activity, 1.55)
    rest_kcal = round(tdee + GOAL_KCAL_DELTA.get(goal, 0))

    protein_g = round(weight * PROTEIN_PER_KG.get(goal, 1.8))
    fat_kcal_floor = rest_kcal * FAT_PCT_FLOOR
    fat_g = round(fat_kcal_floor / 9)
    # Carbs fill what's left. 4 kcal/g for both protein and carbs, 9 for fat.
    used_kcal = protein_g * 4 + fat_g * 9
    carbs_g = max(0, round((rest_kcal - used_kcal) / 4))

    return Targets(
        bmr=round(bmr),
        tdee=round(tdee),
        rest_kcal=rest_kcal,
        rest_protein_g=protein_g,
        rest_carbs_g=carbs_g,
        rest_fat_g=fat_g,
    )


def slot_targets(rest_targets: Targets, slot_split: dict, *, training: bool,
                 training_delta: dict | None = None) -> dict:
    """Derive per-slot targets from the day's rest target + slot kcal split,
    applying training-day uplift either flat (training_delta is None or has
    no per-slot key) or per-slot (training_delta_per_slot is configured)."""
    out = {}
    for slot, frac in slot_split.items():
        kcal = rest_targets["rest_kcal"] * frac
        prot = rest_targets["rest_protein_g"] * frac
        if training and training_delta:
            kcal += (training_delta.get(slot, {}) or {}).get("kcal", 0)
            prot += (training_delta.get(slot, {}) or {}).get("protein_g", 0)
        out[slot] = {"kcal": round(kcal), "protein_g": round(prot)}
    return out
