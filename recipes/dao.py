"""Recipes + recipe_ingredients data access. Thin SQLite wrapper.

Schema is defined in db.py; here we just provide the typed CRUD helpers
the API endpoints call. All quantities + per-serving macros are computed
from the ingredients on save (see compute_totals).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import db
from pantry.units import canonical_key

FEEDBACK_PREFERENCES = {"neutral", "make_again", "avoid"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- create / update -------------------------------------------------------

def create(*, name: str, name_it: Optional[str], source: str,
           source_url: Optional[str], servings: int, total_time_min: Optional[int],
           active_time_min: Optional[int], difficulty: Optional[int],
           cuisine: Optional[str], meal_slot: Optional[str], equipment: list[str],
           steps: list[str], notes: Optional[str], ingredients: list[dict]) -> int:
    """ingredients: each dict has keys ingredient_key, display_name,
    display_name_it (opt), quantity, unit, optional, kcal, protein_g,
    carbs_g, fat_g, fiber_g, and nutrition provenance."""
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO recipes "
            "(name, name_it, source, source_url, servings, total_time_min, active_time_min, "
            " difficulty, cuisine, meal_slot, equipment_json, steps_json, notes, "
            " kcal, protein_g, carbs_g, fat_g, fiber_g, created_at, cook_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,0,0,0,0, ?, 0)",
            (name, name_it, source, source_url, servings, total_time_min, active_time_min,
             difficulty, cuisine, meal_slot,
             json.dumps(equipment or []), json.dumps(steps or []), notes,
             _now()),
        )
        rid = cur.lastrowid
        for pos, ing in enumerate(ingredients):
            c.execute(
                "INSERT INTO recipe_ingredients "
                "(recipe_id, position, ingredient_key, display_name, display_name_it, "
                " quantity, unit, optional, kcal, protein_g, carbs_g, fat_g, fiber_g, "
                " nutrition_source, nutrition_confidence, nutrition_basis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, pos, canonical_key(
                    ing["display_name"], ing.get("ingredient_key")
                ),
                 ing["display_name"], ing.get("display_name_it"),
                 ing.get("quantity"), ing.get("unit"),
                 1 if ing.get("optional") else 0,
                 ing.get("kcal"), ing.get("protein_g"), ing.get("carbs_g"),
                 ing.get("fat_g"), ing.get("fiber_g"),
                 ing.get("nutrition_source") or "unknown",
                 ing.get("nutrition_confidence") or "unknown",
                 ing.get("nutrition_basis")),
            )
        # Compute per-serving totals from ingredient macros
        compute_totals(rid, ingredients, servings)
        return rid


# Internal allowlist for recipes_dao.update — defense in depth so a future
# caller that forwards a hostile dict can't inject arbitrary column names.
_UPDATE_ALLOWED = {
    "name", "name_it", "source", "source_url", "servings", "total_time_min",
    "active_time_min", "difficulty", "cuisine", "meal_slot", "notes",
    "equipment", "steps",
}


def update(rid: int, *, fields: dict, ingredients: Optional[list[dict]] = None) -> None:
    if not fields and ingredients is None:
        return
    cols = []
    vals = []
    for k, v in fields.items():
        if k not in _UPDATE_ALLOWED:
            raise ValueError(f"recipes.update: column {k!r} is not updatable")
        if k in ("equipment", "steps"):
            cols.append(f"{k}_json = ?")
            vals.append(json.dumps(v or []))
        else:
            cols.append(f"{k} = ?")
            vals.append(v)
    with db.tx() as c:
        before = c.execute(
            "SELECT servings, kcal, protein_g, carbs_g, fat_g, fiber_g "
            "FROM recipes WHERE id = ?",
            (rid,),
        ).fetchone()
        if cols:
            vals.append(rid)
            c.execute(f"UPDATE recipes SET {', '.join(cols)} WHERE id = ?", vals)
        if ingredients is not None:
            c.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (rid,))
            for pos, ing in enumerate(ingredients):
                c.execute(
                    "INSERT INTO recipe_ingredients "
                    "(recipe_id, position, ingredient_key, display_name, display_name_it, "
                    " quantity, unit, optional, kcal, protein_g, carbs_g, fat_g, fiber_g, "
                    " nutrition_source, nutrition_confidence, nutrition_basis) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, pos, canonical_key(
                        ing["display_name"], ing.get("ingredient_key")
                    ),
                     ing["display_name"], ing.get("display_name_it"),
                     ing.get("quantity"), ing.get("unit"),
                     1 if ing.get("optional") else 0,
                     ing.get("kcal"), ing.get("protein_g"), ing.get("carbs_g"),
                     ing.get("fat_g"), ing.get("fiber_g"),
                     ing.get("nutrition_source") or "unknown",
                     ing.get("nutrition_confidence") or "unknown",
                     ing.get("nutrition_basis")),
                )
            r = c.execute("SELECT servings FROM recipes WHERE id = ?", (rid,)).fetchone()
            servings = (r["servings"] if r else 1) or 1
            compute_totals(rid, ingredients, servings)
        elif before and "servings" in fields:
            old_servings = max(1, int(before["servings"] or 1))
            new_servings = max(1, int(fields["servings"] or 1))
            if old_servings != new_servings:
                values = []
                for key in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g"):
                    value = before[key]
                    values.append(
                        None if value is None
                        else round(float(value) * old_servings / new_servings, 1)
                    )
                c.execute(
                    "UPDATE recipes SET kcal = ?, protein_g = ?, carbs_g = ?, "
                    "fat_g = ?, fiber_g = ? WHERE id = ?",
                    (*values, rid),
                )


def compute_totals(rid: int, ingredients: list[dict], servings: int) -> None:
    """Sum known ingredient macros and divide by servings.

    Unknown nutrition stays NULL instead of becoming a misleading zero. A
    partially counted recipe still exposes its known subtotal while the API's
    completeness summary makes the missing ingredients visible.
    """
    totals = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    counts = {key: 0 for key in totals}
    for ing in ingredients:
        for k in totals:
            v = ing.get(k)
            if v is not None:
                try:
                    totals[k] += float(v)
                    counts[k] += 1
                except (TypeError, ValueError):
                    pass
    s = max(1, int(servings or 1))
    values = {
        key: round(totals[key] / s, 1) if counts[key] else None
        for key in totals
    }
    db._conn().execute(
        "UPDATE recipes SET kcal = ?, protein_g = ?, carbs_g = ?, fat_g = ?, fiber_g = ? "
        "WHERE id = ?",
        (
            values["kcal"],
            values["protein_g"],
            values["carbs_g"],
            values["fat_g"],
            values["fiber_g"],
            rid,
        ),
    )


# ---- read ------------------------------------------------------------------

def get(rid: int, *, include_archived: bool = False) -> Optional[dict]:
    sql = "SELECT * FROM recipes WHERE id = ?"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    r = db._conn().execute(sql, (rid,)).fetchone()
    if not r:
        return None
    out = dict(r)
    out["equipment"] = json.loads(out.pop("equipment_json") or "[]")
    out["steps"] = json.loads(out.pop("steps_json") or "[]")
    rows = db._conn().execute(
        "SELECT * FROM recipe_ingredients WHERE recipe_id = ? ORDER BY position",
        (rid,),
    ).fetchall()
    out["ingredients"] = [dict(x) for x in rows]
    out["feedback"] = get_feedback(rid)
    return out


def list_(*, search: str = "", cuisine: str = "", meal_slot: str = "",
          favorites_only: bool = False, limit: int = 200) -> list[dict]:
    sql = "SELECT r.id, r.name, r.name_it, r.cuisine, r.meal_slot, " \
          "r.total_time_min, r.kcal, r.protein_g, r.cook_count, " \
          "r.last_cooked_at, rf.rating, " \
          "COALESCE(rf.preference, 'neutral') AS preference, " \
          "(SELECT COUNT(*) FROM recipe_ingredients ri " \
          " WHERE ri.recipe_id = r.id) AS ingredient_count, " \
          "(SELECT COUNT(*) FROM recipe_ingredients ri " \
          " WHERE ri.recipe_id = r.id AND (" \
          "   ri.kcal IS NOT NULL OR ri.protein_g IS NOT NULL OR " \
          "   ri.carbs_g IS NOT NULL OR ri.fat_g IS NOT NULL OR " \
          "   ri.fiber_g IS NOT NULL" \
          " )) AS nutrition_count " \
          "FROM recipes r LEFT JOIN recipe_feedback rf ON rf.recipe_id = r.id " \
          "WHERE r.archived_at IS NULL"
    params: list = []
    if search:
        sql += " AND (r.name LIKE ? OR r.name_it LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if cuisine:
        sql += " AND r.cuisine = ?"
        params.append(cuisine)
    if meal_slot:
        sql += " AND r.meal_slot LIKE ?"
        params.append(f"%{meal_slot}%")
    sql += " ORDER BY COALESCE(r.last_cooked_at, r.created_at) DESC LIMIT ?"
    params.append(limit)
    rows = db._conn().execute(sql, params).fetchall()
    items = [dict(x) for x in rows]
    if favorites_only:
        favs = set(_favorites())
        items = [x for x in items if x["id"] in favs]
    return items


def delete(rid: int) -> bool:
    cur = db._conn().execute(
        "UPDATE recipes SET archived_at = ? "
        "WHERE id = ? AND archived_at IS NULL",
        (_now(), rid),
    )
    return cur.rowcount > 0


def mark_cooked(rid: int) -> None:
    db._conn().execute(
        "UPDATE recipes SET cook_count = cook_count + 1, last_cooked_at = ? WHERE id = ?",
        (_now(), rid),
    )


# ---- planning feedback ----------------------------------------------------

def get_feedback(rid: int) -> dict:
    row = db._conn().execute(
        "SELECT rating, preference, updated_at "
        "FROM recipe_feedback WHERE recipe_id = ?",
        (rid,),
    ).fetchone()
    if not row:
        return {
            "rating": None,
            "preference": "neutral",
            "updated_at": None,
        }
    return dict(row)


def set_feedback(
    rid: int,
    *,
    rating: Optional[int],
    preference: str,
) -> dict:
    if rating is not None and rating not in range(1, 6):
        raise ValueError("rating must be between 1 and 5")
    if preference not in FEEDBACK_PREFERENCES:
        raise ValueError("invalid feedback preference")
    now = _now()
    with db.tx() as c:
        recipe = c.execute(
            "SELECT 1 FROM recipes WHERE id = ? AND archived_at IS NULL",
            (rid,),
        ).fetchone()
        if not recipe:
            raise KeyError("recipe not found")
        c.execute(
            "INSERT INTO recipe_feedback "
            "(recipe_id, rating, preference, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(recipe_id) DO UPDATE SET "
            "rating = excluded.rating, preference = excluded.preference, "
            "updated_at = excluded.updated_at",
            (rid, rating, preference, now),
        )
    return get_feedback(rid)


# ---- favorites (stored on preferences.favorites_json) ---------------------

def _favorites() -> list[int]:
    return db.get_preferences().get("favorites") or []


def is_favorite(rid: int) -> bool:
    return rid in _favorites()


def toggle_favorite(rid: int) -> bool:
    """Atomic toggle. Read-modify-write of the favorites JSON blob is racy
    under concurrent threads; wrap in tx() so the read + update are serialized
    against other writers."""
    with db.tx():
        favs = _favorites()
        if rid in favs:
            favs = [x for x in favs if x != rid]
            new_state = False
        else:
            favs.append(rid)
            new_state = True
        db.update_preferences(favorites=favs)
    return new_state
