"""Greedy weekly meal planner.

Reads from settings_kv (rotation_window_days, cook_time_budget_min,
slot_kcal_split, favorites_bypass_mode, default_servings)
and the user_profile + preferences tables. Walks the requested week
slot-by-slot, scoring candidate recipes and picking the best fit.

Pure Python, deterministic, no LLM. Time-bounded — should complete a
7-day plan in <500ms with a few hundred recipes.

Public:
    plan_week(start_date_iso) -> dict with proposed plan + diagnostics
    save_plan(plan) -> None    (writes to meal_plan table)
"""
from __future__ import annotations

import json
import logging
import math
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import db
import prepared
import settings as app_settings
from planner import targets as targets_mod

log = logging.getLogger("king-of-meal-prep.planner")

SLOTS = ("breakfast", "lunch", "dinner", "snack")
DAY_OF_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class Candidate:
    id: int
    name: str
    meal_slot: str
    cuisine: Optional[str]
    total_time_min: Optional[int]
    difficulty: Optional[int]
    equipment: list[str]
    ingredient_keys: list[str]
    yield_servings: float
    kcal: float
    protein_g: float
    last_cooked_at: Optional[str]
    cook_count: int
    rating: Optional[int]
    preference: str


def _load_candidates() -> list[Candidate]:
    rows = db._conn().execute(
        "SELECT r.*, rf.rating, "
        "       COALESCE(rf.preference, 'neutral') AS preference, "
        "       (SELECT GROUP_CONCAT(ingredient_key, '|') "
        "        FROM recipe_ingredients ri WHERE ri.recipe_id = r.id) AS ing_concat "
        "FROM recipes r "
        "LEFT JOIN recipe_feedback rf ON rf.recipe_id = r.id "
        "WHERE r.archived_at IS NULL"
    ).fetchall()
    out = []
    for r in rows:
        out.append(Candidate(
            id=r["id"], name=r["name"],
            meal_slot=r["meal_slot"] or "",
            cuisine=r["cuisine"],
            total_time_min=r["total_time_min"],
            difficulty=r["difficulty"],
            equipment=json.loads(r["equipment_json"] or "[]"),
            ingredient_keys=(r["ing_concat"] or "").split("|"),
            yield_servings=max(float(r["servings"] or 1), 0.1),
            kcal=r["kcal"] or 0,
            protein_g=r["protein_g"] or 0,
            last_cooked_at=r["last_cooked_at"],
            cook_count=r["cook_count"] or 0,
            rating=r["rating"],
            preference=r["preference"] or "neutral",
        ))
    return out


def _eligible(c: Candidate, *, slot: str, dow: str, time_budget_min: int,
              user_equipment: set[str], dislikes: set[str], allergies: set[str],
              cooked_recently: set[int], favorites: set[int],
              planned_this_week: set[int],
              favorites_bypass: str,
              using_prepared: bool = False) -> tuple[bool, str]:
    if c.preference == "avoid":
        return False, "avoided"
    if c.meal_slot and slot not in c.meal_slot and not using_prepared:
        return False, "wrong slot"
    if (
        not using_prepared
        and c.equipment
        and not set(c.equipment).issubset(user_equipment)
    ):
        return False, "needs equipment"
    if (
        not using_prepared
        and c.total_time_min is not None
        and c.total_time_min > time_budget_min
    ):
        return False, "too slow"
    blocked = dislikes | allergies
    if blocked:
        for ingredient_key in c.ingredient_keys:
            normalized = re.sub(
                r"[^a-z0-9]+", " ", ingredient_key.lower()
            ).strip()
            if any(
                re.search(rf"(?:^|\s){re.escape(term)}(?:$|\s)", normalized)
                for term in blocked
                if term
            ):
                return False, "contains disliked/allergen"
    if not using_prepared and c.id in cooked_recently:
        if c.id not in favorites or favorites_bypass == "off":
            return False, "rotation"
    if not using_prepared and c.id in planned_this_week:
        if c.id not in favorites or favorites_bypass != "always":
            return False, "rotation"
    return True, "ok"


def _score_details(
    c: Candidate,
    *,
    slot_target_kcal: int,
    slot_target_protein: int,
    pantry_ingredient_keys: set[str],
    days_since_cook: int,
    is_weekday: bool,
    serving_count: float = 1.0,
    using_prepared: bool = False,
) -> dict:
    # Macro fit: lower RMS error is better. Protect against zero macros.
    fit_kcal = abs(
        (c.kcal or 0) * serving_count - slot_target_kcal
    ) / max(1, slot_target_kcal)
    fit_prot = abs(
        (c.protein_g or 0) * serving_count - slot_target_protein
    ) / max(1, slot_target_protein)
    macro_fit = math.sqrt(fit_kcal**2 + fit_prot**2)

    # Pantry overlap: exact canonical-key matches, not display-name substrings.
    pantry_overlap = 0.0
    if c.ingredient_keys:
        hits = sum(
            1 for key in c.ingredient_keys if key in pantry_ingredient_keys
        )
        pantry_overlap = hits / len(c.ingredient_keys)

    # Rotation freshness: +1 the longer it's been since we cooked
    rotation_bonus = min(days_since_cook / 30.0, 1.0)

    # Difficulty: easier = better on weekdays, no preference on weekends
    diff_pen = 0.0
    if is_weekday and c.difficulty:
        diff_pen = (c.difficulty - 1) * 0.08

    # Lower is better
    prepared_bonus = 1.25 if using_prepared else 0.0
    rating_adjustment = (
        (3 - int(c.rating)) * 0.12
        if c.rating is not None else 0.0
    )
    preference_bonus = 0.28 if c.preference == "make_again" else 0.0
    total = (
        macro_fit
        - 0.3 * pantry_overlap
        - 0.2 * rotation_bonus
        - prepared_bonus
        - preference_bonus
        + rating_adjustment
        + diff_pen
    )
    return {
        "total": total,
        "macro_fit": macro_fit,
        "pantry_overlap": pantry_overlap,
        "rotation_bonus": rotation_bonus,
    }


def _score(c: Candidate, *, slot_target_kcal: int, slot_target_protein: int,
           pantry_ingredient_keys: set[str], days_since_cook: int,
           is_weekday: bool, serving_count: float = 1.0,
           using_prepared: bool = False) -> float:
    return float(_score_details(
        c,
        slot_target_kcal=slot_target_kcal,
        slot_target_protein=slot_target_protein,
        pantry_ingredient_keys=pantry_ingredient_keys,
        days_since_cook=days_since_cook,
        is_weekday=is_weekday,
        serving_count=serving_count,
        using_prepared=using_prepared,
    )["total"])


def _selection_reasons(
    candidate: Candidate,
    details: dict,
    *,
    using_prepared: bool,
    favorite: bool,
    days_since_cook: int,
) -> list[str]:
    reasons = []
    if using_prepared:
        reasons.append("Prepared portion ready")
    if candidate.preference == "make_again":
        reasons.append("Marked make again")
    if candidate.rating is not None and candidate.rating >= 4:
        reasons.append(f"Rated {candidate.rating}/5")
    if favorite:
        reasons.append("Favorite")
    if details["pantry_overlap"] >= 0.34:
        reasons.append("Uses pantry stock")
    if details["macro_fit"] <= 0.35:
        reasons.append("Close to nutrition target")
    if candidate.total_time_min is not None and candidate.total_time_min <= 30:
        reasons.append(f"{candidate.total_time_min} min")
    if candidate.cook_count == 0:
        reasons.append("New to your rotation")
    elif days_since_cook >= 21:
        reasons.append("Due for rotation")
    return (reasons or ["Best available fit"])[:4]


def plan_week(start_iso: str, *, preserve_manual: bool = True) -> dict:
    """Generate proposals for 7×4 = 28 slots starting at start_iso (Monday).

    Returns:
      {"plan": {"YYYY-MM-DD": {"breakfast": {"recipe_id": ..., "name": ..., "kcal": ...}, ...}, ...},
       "skipped": [{"date": ..., "slot": ..., "reason": ...}, ...],
       "candidates_total": N}
    """
    start = date.fromisoformat(start_iso)

    # Read all the settings up front
    profile = db.get_user_profile()
    if not profile.get("rest_kcal_target"):
        # Profile incomplete; can't plan macros — use a default
        rest_targets = {"rest_kcal": 2000, "rest_protein_g": 130,
                        "rest_carbs_g": 250, "rest_fat_g": 70}
    else:
        rest_targets = {
            "rest_kcal":      profile["rest_kcal_target"],
            "rest_protein_g": profile["rest_protein_g"],
            "rest_carbs_g":   profile["rest_carbs_g"],
            "rest_fat_g":     profile["rest_fat_g"],
        }
    slot_split = app_settings.kv_get("slot_kcal_split") or {
        "breakfast": 0.20, "lunch": 0.30, "dinner": 0.35, "snack": 0.15
    }
    cook_budget = app_settings.kv_get("cook_time_budget_min") or {
        d: 30 for d in DAY_OF_WEEK
    }
    rotation_days = int(app_settings.kv_get("rotation_window_days") or 14)
    fav_bypass = app_settings.kv_get("favorites_bypass_mode") or "always"
    weekday_set = set(app_settings.kv_get("weekday_set") or
                       ["mon","tue","wed","thu","fri"])
    default_servings = float(app_settings.kv_get("default_servings") or 1)
    training_slot_delta = app_settings.kv_get("training_delta_per_slot")
    leftover_behavior = app_settings.kv_get("leftover_behavior") or "next_day_lunch"

    prefs = db.get_preferences()
    user_equipment = set(prefs.get("equipment") or [])
    dislikes = {x.lower() for x in (prefs.get("dislikes") or [])}
    allergies = {x.lower() for x in (prefs.get("allergies") or [])}
    favorites = set(prefs.get("favorites") or [])

    # Recently-cooked set. Use timezone-aware UTC throughout so we can compare
    # against last_cooked_at (also stored UTC).
    from datetime import timezone as _tz
    cutoff = (datetime.now(_tz.utc) - timedelta(days=rotation_days)).isoformat()
    recently = {r["id"] for r in db._conn().execute(
        "SELECT id FROM recipes WHERE last_cooked_at >= ?", (cutoff,)
    ).fetchall()}

    # Pantry ingredient keys (so the solver prefers what we already have)
    pantry_keys = {r["ingredient_key"] for r in db._conn().execute(
        "SELECT DISTINCT ingredient_key FROM pantry_items WHERE exhausted_at IS NULL"
    ).fetchall()}

    candidates = _load_candidates()
    virtual_prepared = {
        candidate.id: prepared.available(candidate.id)
        for candidate in candidates
    }
    end_iso = (start + timedelta(days=7)).isoformat()
    existing_rows = db._conn().execute(
        "SELECT * FROM meal_plan WHERE date >= ? AND date < ?",
        (start_iso, end_iso),
    ).fetchall()
    existing = {(r["date"], r["slot"]): dict(r) for r in existing_rows}
    training_dates = {
        r["date"] for r in existing_rows if r["is_training_day"]
    }
    plan: dict = {}
    skipped: list[dict] = []
    conflicts: list[dict] = []
    used_today: dict[str, set[int]] = {}   # avoid same recipe twice in one day
    # `recently` tracks recipes we should NOT pick again. We start it from
    # the last_cooked_at history (cooked within rotation_window_days), and
    # GROW it as we plan — once we've planned recipe X for Monday, X is
    # in `recently` so it won't be picked again Tuesday/Wednesday/etc.
    # This is the missing-feature side of the rotation rule. (#4)
    planned_so_far: set[int] = set()

    for day_offset in range(7):
        d = start + timedelta(days=day_offset)
        diso = d.isoformat()
        dow = DAY_OF_WEEK[d.weekday()]
        is_weekday = dow in weekday_set
        time_budget = int(cook_budget.get(dow) or 30)
        used_today[diso] = set()
        plan[diso] = {}

        for slot in SLOTS:
            current = existing.get((diso, slot))
            preserve_reason = None
            if current:
                if current["status"] == "cooked":
                    preserve_reason = "cooked"
                elif current["locked"]:
                    preserve_reason = "locked"
                elif (
                    preserve_manual
                    and current["origin"] == "manual"
                    and current["recipe_id"] is not None
                ):
                    preserve_reason = "manual"
            if preserve_reason:
                recipe = next(
                    (candidate for candidate in candidates
                     if candidate.id == current["recipe_id"]),
                    None,
                )
                plan[diso][slot] = {
                    "recipe_id": current["recipe_id"],
                    "name": recipe.name if recipe else None,
                    "kcal": (
                        int(
                            (recipe.kcal or 0)
                            * float(current["servings"] or 1)
                        )
                        if recipe else 0
                    ),
                    "protein_g": (
                        int(
                            (recipe.protein_g or 0)
                            * float(current["servings"] or 1)
                        )
                        if recipe else 0
                    ),
                    "servings": float(current["servings"] or 1),
                    "is_training_day": diso in training_dates,
                    "preserved": True,
                    "preserve_reason": preserve_reason,
                }
                conflicts.append({
                    "date": diso,
                    "slot": slot,
                    "reason": preserve_reason,
                })
                if current["recipe_id"]:
                    used_today[diso].add(current["recipe_id"])
                    planned_so_far.add(current["recipe_id"])
                continue

            # Per-slot kcal/protein target
            day_kcal = rest_targets["rest_kcal"]
            day_protein = rest_targets["rest_protein_g"]
            if diso in training_dates:
                day_kcal += int(profile.get("training_kcal_delta") or 0)
                day_protein += int(profile.get("training_protein_delta") or 0)
            slot_kcal = round(day_kcal * slot_split.get(slot, 0.25))
            slot_prot = round(day_protein * slot_split.get(slot, 0.25))
            if isinstance(training_slot_delta, dict) and diso in training_dates:
                delta = training_slot_delta.get(slot) or {}
                if isinstance(delta, dict):
                    slot_kcal += int(delta.get("kcal") or 0)
                    slot_prot += int(delta.get("protein_g") or 0)

            best: Optional[Candidate] = None
            best_score = float("inf")
            best_details: Optional[dict] = None
            best_days_since = 0
            for c in candidates:
                if c.id in used_today[diso]:
                    continue
                prepared_available = virtual_prepared.get(c.id, 0.0)
                using_prepared = (
                    prepared_available + prepared.EPSILON >= default_servings
                    and (
                        not c.meal_slot
                        or slot in c.meal_slot
                        or (
                            leftover_behavior == "next_day_lunch"
                            and slot == "lunch"
                        )
                        or leftover_behavior == "same_day"
                    )
                )
                # Combine cooked-recently with planned-so-far so the rotation
                # rule applies inside this very planning run (#4).
                ok, reason = _eligible(
                    c, slot=slot, dow=dow, time_budget_min=time_budget,
                    user_equipment=user_equipment, dislikes=dislikes,
                    allergies=allergies,
                    cooked_recently=recently | planned_so_far,
                    favorites=favorites, favorites_bypass=fav_bypass,
                    planned_this_week=planned_so_far,
                    using_prepared=using_prepared,
                )
                if not ok:
                    continue
                if c.last_cooked_at:
                    try:
                        # last_cooked_at is stored UTC ISO; today's date in UTC
                        # avoids a TZ-induced ±1 day error on the days-since math.
                        last_d = datetime.fromisoformat(c.last_cooked_at).date()
                        days_since = (datetime.now(_tz.utc).date() - last_d).days
                    except ValueError:
                        days_since = rotation_days * 2
                else:
                    days_since = 9999
                details = _score_details(
                    c,
                    slot_target_kcal=slot_kcal, slot_target_protein=slot_prot,
                    pantry_ingredient_keys=pantry_keys,
                    days_since_cook=days_since, is_weekday=is_weekday,
                    serving_count=default_servings,
                    using_prepared=using_prepared,
                )
                s = float(details["total"])
                if s < best_score:
                    best_score = s
                    best = c
                    best_details = details
                    best_days_since = days_since

            if best is None:
                plan[diso][slot] = None
                skipped.append({"date": diso, "slot": slot, "reason": "no eligible recipe"})
            else:
                if best_details is None:
                    raise RuntimeError("selected planner candidate has no score")
                uses_prepared = (
                    virtual_prepared.get(best.id, 0.0)
                    + prepared.EPSILON >= default_servings
                    and (
                        not best.meal_slot
                        or slot in best.meal_slot
                        or (
                            leftover_behavior == "next_day_lunch"
                            and slot == "lunch"
                        )
                        or leftover_behavior == "same_day"
                    )
                )
                plan[diso][slot] = {
                    "recipe_id": best.id, "name": best.name,
                    "kcal": int((best.kcal or 0) * default_servings),
                    "protein_g": int(
                        (best.protein_g or 0) * default_servings
                    ),
                    "servings": default_servings,
                    "uses_prepared": uses_prepared,
                    "reasons": _selection_reasons(
                        best,
                        best_details,
                        using_prepared=uses_prepared,
                        favorite=best.id in favorites,
                        days_since_cook=best_days_since,
                    ),
                    "is_training_day": diso in training_dates,
                    "preserved": False,
                    "score": round(best_score, 3),
                }
                used_today[diso].add(best.id)
                if plan[diso][slot]["uses_prepared"]:
                    virtual_prepared[best.id] = max(
                        0.0,
                        virtual_prepared.get(best.id, 0.0) - default_servings,
                    )
                else:
                    virtual_prepared[best.id] = (
                        virtual_prepared.get(best.id, 0.0)
                        + max(0.0, best.yield_servings - default_servings)
                    )
                # Lock this recipe out of further days within the rotation
                # window unless it's a favorite and bypass mode allows it.
                if (
                    not plan[diso][slot]["uses_prepared"]
                    and not (best.id in favorites and fav_bypass == "always")
                ):
                    planned_so_far.add(best.id)

    return {
        "plan": plan,
        "skipped": skipped,
        "conflicts": conflicts,
        "candidates_total": len(candidates),
        "preserve_manual": preserve_manual,
        "training_dates": sorted(training_dates),
    }


def plan_version(start_iso: str) -> int:
    row = db._conn().execute(
        "SELECT version FROM plan_weeks WHERE start_date = ?", (start_iso,)
    ).fetchone()
    return int(row["version"]) if row else 0


def create_proposal(start_iso: str, *, preserve_manual: bool | None = None) -> dict:
    if preserve_manual is None:
        preserve_manual = bool(app_settings.kv_get("planner_preserve_manual"))
    version_before = plan_version(start_iso)
    proposal = plan_week(start_iso, preserve_manual=preserve_manual)
    version_after = plan_version(start_iso)
    if version_after != version_before:
        raise RuntimeError("week changed while the proposal was being generated")
    proposal_id = secrets.token_urlsafe(24)
    expected_version = version_after
    now = datetime.now(timezone.utc)
    with db.tx() as c:
        version_row = c.execute(
            "SELECT version FROM plan_weeks WHERE start_date = ?", (start_iso,)
        ).fetchone()
        locked_version = int(version_row["version"]) if version_row else 0
        if locked_version != expected_version:
            raise RuntimeError(
                "week changed while the proposal was being generated"
            )
        c.execute(
            "DELETE FROM plan_proposals WHERE expires_at < ?", (now.isoformat(),)
        )
        c.execute(
            "INSERT INTO plan_proposals "
            "(id, start_date, expected_version, payload_json, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                proposal_id,
                start_iso,
                expected_version,
                json.dumps(proposal),
                now.isoformat(),
                (now + timedelta(minutes=15)).isoformat(),
            ),
        )
    return {
        **proposal,
        "proposal_id": proposal_id,
        "expected_version": expected_version,
        "start": start_iso,
    }


def commit_proposal(proposal_id: str, expected_version: int) -> dict:
    """Commit one unexpired proposal if the saved week has not changed."""
    now = datetime.now(timezone.utc).isoformat()
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM plan_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if not row or row["expires_at"] < now:
            raise ValueError("proposal expired; generate a new one")
        if int(row["expected_version"]) != int(expected_version):
            raise ValueError("proposal version does not match")
        version_row = c.execute(
            "SELECT version FROM plan_weeks WHERE start_date = ?",
            (row["start_date"],),
        ).fetchone()
        current_version = int(version_row["version"]) if version_row else 0
        if current_version != int(expected_version):
            raise RuntimeError("week changed since this proposal was generated")

        proposal = json.loads(row["payload_json"])
        preserve_manual = bool(proposal.get("preserve_manual"))
        training_dates = set(proposal.get("training_dates") or [])
        written = 0
        cleared = 0
        for day, slots in proposal["plan"].items():
            for slot, item in slots.items():
                if item and item.get("preserved"):
                    continue
                existing = c.execute(
                    "SELECT * FROM meal_plan WHERE date = ? AND slot = ?",
                    (day, slot),
                ).fetchone()
                replaceable = (
                    not existing
                    or (
                        existing["status"] != "cooked"
                        and not existing["locked"]
                        and (
                            existing["origin"] == "planner"
                            or not preserve_manual
                            or existing["recipe_id"] is None
                        )
                    )
                )
                if not replaceable:
                    raise RuntimeError(f"conflict at {day} {slot}")
                if not item:
                    if existing:
                        c.execute(
                            "DELETE FROM meal_plan WHERE date = ? AND slot = ?",
                            (day, slot),
                        )
                        cleared += 1
                    continue
                training = 1 if day in training_dates else 0
                c.execute(
                    "INSERT INTO meal_plan "
                    "(date, slot, recipe_id, servings, status, cooked_at, "
                    " is_training_day, origin, locked, version) "
                    "VALUES (?, ?, ?, ?, 'planned', NULL, ?, 'planner', 0, 1) "
                    "ON CONFLICT(date, slot) DO UPDATE SET "
                    "recipe_id = excluded.recipe_id, servings = excluded.servings, "
                    "status = 'planned', cooked_at = NULL, "
                    "is_training_day = excluded.is_training_day, origin = 'planner', "
                    "locked = 0, version = meal_plan.version + 1",
                    (
                        day,
                        slot,
                        item["recipe_id"],
                        item.get("servings") or 1,
                        training,
                    ),
                )
                written += 1

        # A training flag is a property of the date, not whichever slot was
        # edited first. Keep every surviving row in sync and retain one
        # replaceable placeholder only when a training date has no meals.
        for day in proposal["plan"]:
            training = 1 if day in training_dates else 0
            c.execute(
                "UPDATE meal_plan SET is_training_day = ? WHERE date = ?",
                (training, day),
            )
            if training and not c.execute(
                "SELECT 1 FROM meal_plan WHERE date = ? LIMIT 1", (day,)
            ).fetchone():
                c.execute(
                    "INSERT INTO meal_plan "
                    "(date, slot, recipe_id, servings, status, is_training_day, "
                    " origin, locked, version) "
                    "VALUES (?, 'snack', NULL, 1, 'planned', 1, 'planner', 0, 1)",
                    (day,),
                )

        new_version = current_version + 1
        c.execute(
            "INSERT INTO plan_weeks (start_date, version, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(start_date) DO UPDATE SET "
            "version = excluded.version, updated_at = excluded.updated_at",
            (row["start_date"], new_version, now),
        )
        c.execute("DELETE FROM plan_proposals WHERE id = ?", (proposal_id,))
    return {
        "saved": written,
        "cleared": cleared,
        "version": new_version,
        "start": row["start_date"],
        "skipped": proposal.get("skipped", []),
    }


def list_week(start_iso: str) -> dict:
    """Return the saved plan for the 7 days from start_iso."""
    start = date.fromisoformat(start_iso)
    end = (start + timedelta(days=7)).isoformat()
    rows = db._conn().execute(
        "SELECT mp.*, r.name AS recipe_name, r.kcal AS recipe_kcal, "
        "r.servings AS recipe_servings "
        "FROM meal_plan mp LEFT JOIN recipes r ON r.id = mp.recipe_id "
        "WHERE mp.date >= ? AND mp.date < ?",
        (start.isoformat(), end),
    ).fetchall()
    plan: dict = {(start + timedelta(days=i)).isoformat(): {} for i in range(7)}
    for r in rows:
        plan[r["date"]][r["slot"]] = {
            "recipe_id": r["recipe_id"], "name": r["recipe_name"],
            "kcal": int(
                float(r["recipe_kcal"] or 0) * float(r["servings"] or 1)
            ),
            "status": r["status"], "is_training_day": bool(r["is_training_day"]),
            "servings": float(r["servings"] or 1),
            "recipe_servings": float(r["recipe_servings"] or 1),
            "prepared_portions": (
                prepared.available(int(r["recipe_id"]))
                if r["recipe_id"] else 0
            ),
            "locked": bool(r["locked"]),
            "origin": r["origin"],
            "version": int(r["version"]),
        }
    return plan
