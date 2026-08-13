"""Pantry items data access.

Active items = exhausted_at IS NULL. Consumption either reduces quantity or
sets exhausted_at when fully used. Expiry buckets are computed in the
endpoint based on settings_kv.expiry_days_by_category for items without
an explicit expires_on date.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import math
from typing import Optional

import db
import settings as app_settings
from nutrition import resolve as nutrition_resolve
from pantry.units import (
    canonical_key,
    compatible,
    from_canonical,
    normalize_unit,
    to_canonical,
)

_UNSET = object()


class PortionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> datetime:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# Heuristic: classify a free-text ingredient into one of the expiry buckets.
# Used when the user adds a pantry item without giving us an expires_on date.
# Categories must match keys in settings_kv.expiry_days_by_category.
CATEGORY_HINTS = {
    "produce":  ["spinach","kale","lettuce","tomato","cucumber","pepper","onion",
                 "garlic","carrot","celery","potato","apple","banana","lemon",
                 "lime","orange","mushroom","courgette","zucchini","aubergine",
                 "eggplant","broccoli","cauliflower","cabbage","leek","fennel",
                 "asparagus","artichoke","avocado","strawberr","blueberr","raspberr",
                 "pear","peach","grape","mango","pineapple"],
    "dairy":    ["milk","yogurt","cheese","cream","butter","ricotta","mozzarella",
                 "parmesan","parmigiano","mascarpone","feta","cheddar"],
    "meat":     ["chicken","beef","pork","lamb","turkey","sausage","mince","bacon",
                 "rashers","pancetta","prosciutto","ham","duck","veal"],
    "fish":     ["salmon","cod","tuna","fish","trout","prawn","shrimp","squid",
                 "octopus","mussel","clam","sardine","anchov"],
    "frozen":   ["frozen","ice cream","ice-cream"],
    "dry":      ["pasta","rice","flour","sugar","salt","pepper","oil","vinegar",
                 "lentil","bean","chickpea","oat","cereal","tea","coffee","spice",
                 "honey","baking","cocoa","chocolate","biscuit","cracker"],
}

DEFAULT_EXPIRY_DAYS = {"produce": 5, "dairy": 14, "dry": 365, "frozen": 90,
                       "meat": 3, "fish": 2, "other": 7}


def categorize(name: str) -> str:
    n = (name or "").lower()
    for cat, words in CATEGORY_HINTS.items():
        if any(w in n for w in words):
            return cat
    return "other"


def estimate_expiry(name: str) -> Optional[str]:
    """Return ISO date string `today + N days` for the category. None means
    we don't auto-expire (caller can leave NULL)."""
    days_by = app_settings.kv_get("expiry_days_by_category") or DEFAULT_EXPIRY_DAYS
    cat = categorize(name)
    days = days_by.get(cat) or days_by.get("other")
    if not days:
        return None
    return (_today() + timedelta(days=days)).date().isoformat()


def _portion_size(canonical_quantity: float, portions) -> float | None:
    if portions is None:
        return None
    count = float(portions)
    if not math.isfinite(count) or count <= 0:
        raise PortionError("portions must be a positive finite number")
    return canonical_quantity / count


def _nutrition_profile(item: dict) -> dict:
    return {
        "ingredient_key": item.get("ingredient_key"),
        "kcal_100g": item.get("kcal_100g"),
        "protein_100g": item.get("protein_100g"),
        "carbs_100g": item.get("carbs_100g"),
        "fat_100g": item.get("fat_100g"),
        "fiber_100g": item.get("fiber_100g"),
        "nutrition_source": item.get("nutrition_source") or "unknown",
        "nutrition_confidence": (
            item.get("nutrition_confidence") or "unknown"
        ),
        "nutrition_basis": item.get("nutrition_basis"),
    }


def nutrition_for_amount(
    item: dict,
    *,
    quantity: float,
    unit: str,
) -> dict:
    nutrition = nutrition_resolve.scale_profile(
        _nutrition_profile(item),
        quantity=quantity,
        unit=unit,
        display_name=item["display_name"],
    )
    if (
        nutrition["nutrition_status"] == "no_nutrition"
        and nutrition["nutrition_source"] == "unknown"
    ):
        nutrition["nutrition_status"] = "no_match"
    return nutrition


def _item_dict(row) -> dict:
    item = dict(row)
    size = item.get("portion_size_canonical")
    total = float(item.get("canonical_quantity") or 0)
    if size is not None and float(size) > 0:
        size = float(size)
        item["portion_quantity"] = from_canonical(size, item["unit"])
        item["portions_remaining"] = total / size
    else:
        item["portion_quantity"] = None
        item["portions_remaining"] = None
    nutrition = nutrition_for_amount(
        item,
        quantity=float(item["quantity"]),
        unit=item["unit"],
    )
    item["nutrition"] = nutrition
    item["nutrition_available"] = any(
        item.get(key) is not None
        for key in (
            "kcal_100g",
            "protein_100g",
            "carbs_100g",
            "fat_100g",
            "fiber_100g",
        )
    )
    item["nutrition_amount_available"] = (
        nutrition["nutrition_status"] == "counted"
    )
    if item["portion_quantity"] is not None:
        item["portion_nutrition"] = nutrition_for_amount(
            item,
            quantity=float(item["portion_quantity"]),
            unit=item["unit"],
        )
    else:
        item["portion_nutrition"] = None
    return item


def add(*, ingredient_key: str, display_name: str, quantity: float, unit: str,
        expires_on: Optional[str] = None, source: str = "manual",
        portions: float | None = None, ean: str | None = None,
        nutrition_profile: dict | None = None) -> int:
    if not expires_on:
        expires_on = estimate_expiry(display_name)
    unit = normalize_unit(unit)
    canonical_quantity, canonical_unit, dimension = to_canonical(quantity, unit)
    portion_size = _portion_size(canonical_quantity, portions)
    profile = (
        nutrition_profile
        or nutrition_resolve.by_key(ingredient_key)
        or {}
    )
    nutrition_source = (
        profile.get("nutrition_source")
        or profile.get("source")
        or "unknown"
    )
    nutrition_confidence = (
        profile.get("nutrition_confidence") or "unknown"
    )
    nutrition_basis = profile.get("nutrition_basis")
    cur = db._conn().execute(
        "INSERT INTO pantry_items "
        "(ingredient_key, display_name, quantity, unit, expires_on, source, added_at, "
        " canonical_quantity, canonical_unit, dimension, portion_size_canonical, "
        " ean, kcal_100g, protein_100g, carbs_100g, fat_100g, fiber_100g, "
        " nutrition_source, nutrition_confidence, nutrition_basis) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            canonical_key(display_name, ingredient_key),
            display_name,
            quantity,
            unit,
            expires_on,
            source,
            _now(),
            canonical_quantity,
            canonical_unit,
            dimension,
            portion_size,
            ean,
            profile.get("kcal_100g"),
            profile.get("protein_100g"),
            profile.get("carbs_100g"),
            profile.get("fat_100g"),
            profile.get("fiber_100g"),
            nutrition_source,
            nutrition_confidence,
            nutrition_basis,
        ),
    )
    return cur.lastrowid


def update(pid: int, *, quantity=_UNSET, unit=_UNSET, expires_on=_UNSET,
           display_name=_UNSET, ingredient_key=_UNSET, portions=_UNSET) -> bool:
    row = db._conn().execute(
        "SELECT * FROM pantry_items WHERE id = ?", (pid,)
    ).fetchone()
    if not row:
        return False
    name = row["display_name"] if display_name is _UNSET else display_name
    new_unit = row["unit"] if unit is _UNSET else normalize_unit(unit)
    new_quantity = row["quantity"] if quantity is _UNSET else float(quantity)
    canonical_quantity, canonical_unit, dimension = to_canonical(
        new_quantity, new_unit
    )
    if ingredient_key is _UNSET:
        proposed_key = row["ingredient_key"] if display_name is _UNSET else None
    else:
        proposed_key = ingredient_key
    key = canonical_key(name, proposed_key)
    profile = None
    if key != row["ingredient_key"] and not row["ean"]:
        profile = nutrition_resolve.by_key(key) or {}
    nutrition_values = {
        column: (
            profile.get(column)
            if profile is not None
            else row[column]
        )
        for column in (
            "kcal_100g",
            "protein_100g",
            "carbs_100g",
            "fat_100g",
            "fiber_100g",
        )
    }
    nutrition_source = (
        (
            profile.get("nutrition_source")
            or profile.get("source")
            or "unknown"
        )
        if profile is not None
        else row["nutrition_source"]
    )
    nutrition_confidence = (
        (profile.get("nutrition_confidence") or "unknown")
        if profile is not None
        else row["nutrition_confidence"]
    )
    nutrition_basis = (
        profile.get("nutrition_basis")
        if profile is not None
        else row["nutrition_basis"]
    )
    new_expiry = row["expires_on"] if expires_on is _UNSET else expires_on
    portion_size = (
        row["portion_size_canonical"]
        if portions is _UNSET
        else _portion_size(canonical_quantity, portions)
    )
    db._conn().execute(
        "UPDATE pantry_items SET ingredient_key = ?, display_name = ?, quantity = ?, "
        "unit = ?, expires_on = ?, canonical_quantity = ?, canonical_unit = ?, "
        "dimension = ?, portion_size_canonical = ?, "
        "kcal_100g = ?, protein_100g = ?, carbs_100g = ?, fat_100g = ?, "
        "fiber_100g = ?, nutrition_source = ?, nutrition_confidence = ?, "
        "nutrition_basis = ?, "
        "exhausted_at = CASE WHEN ? > 0 THEN NULL ELSE exhausted_at END, "
        "version = version + 1 WHERE id = ?",
        (
            key,
            name,
            new_quantity,
            new_unit,
            new_expiry,
            canonical_quantity,
            canonical_unit,
            dimension,
            portion_size,
            nutrition_values["kcal_100g"],
            nutrition_values["protein_100g"],
            nutrition_values["carbs_100g"],
            nutrition_values["fat_100g"],
            nutrition_values["fiber_100g"],
            nutrition_source,
            nutrition_confidence,
            nutrition_basis,
            canonical_quantity,
            pid,
        ),
    )
    return True


def remove(pid: int) -> bool:
    cur = db._conn().execute(
        "UPDATE pantry_items SET exhausted_at = ? WHERE id = ? AND exhausted_at IS NULL",
        (_now(), pid),
    )
    return cur.rowcount > 0


def hard_delete(pid: int) -> bool:
    cur = db._conn().execute("DELETE FROM pantry_items WHERE id = ?", (pid,))
    return cur.rowcount > 0


def list_active() -> list[dict]:
    rows = db._conn().execute(
        "SELECT * FROM pantry_items WHERE exhausted_at IS NULL "
        "ORDER BY (expires_on IS NULL), expires_on, display_name COLLATE NOCASE"
    ).fetchall()
    return [_item_dict(r) for r in rows]


def get(pid: int, *, active_only: bool = True) -> dict | None:
    sql = "SELECT * FROM pantry_items WHERE id = ?"
    if active_only:
        sql += " AND exhausted_at IS NULL"
    row = db._conn().execute(sql, (pid,)).fetchone()
    return _item_dict(row) if row else None


def consume_portion(pid: int) -> dict | None:
    """Consume one planned portion, or the smaller final remainder."""
    with db.tx() as connection:
        row = connection.execute(
            "SELECT * FROM pantry_items "
            "WHERE id = ? AND exhausted_at IS NULL",
            (pid,),
        ).fetchone()
        if not row:
            return None
        portion_size = row["portion_size_canonical"]
        if portion_size is None or float(portion_size) <= 0:
            raise PortionError("item is not split into portions")
        available = float(row["canonical_quantity"] or 0)
        consumed = min(float(portion_size), available)
        remaining = max(0.0, available - consumed)
        connection.execute(
            "UPDATE pantry_items SET quantity = ?, canonical_quantity = ?, "
            "exhausted_at = ?, version = version + 1 WHERE id = ?",
            (
                from_canonical(remaining, row["unit"]),
                remaining,
                _now() if remaining <= 0.000001 else None,
                pid,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM pantry_items WHERE id = ?",
            (pid,),
        ).fetchone()
    result = _item_dict(updated)
    result["consumed_quantity"] = from_canonical(consumed, row["unit"])
    return result


def consume_for_recipe(recipe_id: int, servings: float = 1.0, *,
                       conn=None, cook_event_id: int | None = None) -> dict:
    """Subtract raw ingredients for ``servings`` portions of a recipe.

    Ingredient quantities describe one full recipe batch, while
    ``recipes.servings`` is that batch's yield. A four-portion recipe cooked
    as four portions therefore consumes each ingredient exactly once.
    """
    recipe = db._conn().execute(
        "SELECT servings FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    recipe_yield = max(float(recipe["servings"] or 1), 0.1) if recipe else 1.0
    batch_scale = float(servings) / recipe_yield
    rows = db._conn().execute(
        "SELECT ingredient_key, display_name, quantity, unit "
        "FROM recipe_ingredients WHERE recipe_id = ? AND optional = 0",
        (recipe_id,),
    ).fetchall()
    needs: dict[tuple, dict] = {}
    for ing in rows:
        key = canonical_key(ing["display_name"], ing["ingredient_key"])
        unit = normalize_unit(ing["unit"])
        qty = float(ing["quantity"] or 0) * batch_scale
        if qty <= 0:
            continue
        canonical_qty, canonical_unit, dimension = to_canonical(qty, unit)
        need = needs.setdefault((key, dimension), {
            "key": key,
            "unit": canonical_unit,
            "dimension": dimension,
            "name": ing["display_name"],
            "qty": 0.0,
        })
        need["qty"] += canonical_qty

    consumed = []
    missing = []
    context = nullcontext(conn) if conn is not None else db.tx()
    with context as c:
        for (key, dimension), need in needs.items():
            items = c.execute(
                "SELECT * FROM pantry_items WHERE ingredient_key = ? AND dimension = ? "
                "AND exhausted_at IS NULL "
                "ORDER BY (expires_on IS NULL), expires_on",
                (key, dimension),
            ).fetchall()
            remaining = need["qty"]
            for item in items:
                if remaining <= 0.000001:
                    break
                available = float(item["canonical_quantity"] or 0)
                take = min(available, remaining)
                if take <= 0:
                    continue
                new_amount = max(0.0, available - take)
                new_display = from_canonical(new_amount, item["unit"])
                c.execute(
                    "UPDATE pantry_items SET quantity = ?, canonical_quantity = ?, "
                    "exhausted_at = ?, version = version + 1 WHERE id = ?",
                    (
                        new_display,
                        new_amount,
                        _now() if new_amount <= 0.000001 else None,
                        item["id"],
                    ),
                )
                if cook_event_id is not None:
                    c.execute(
                        "INSERT INTO pantry_movements "
                        "(cook_event_id, pantry_item_id, ingredient_key, delta_canonical, "
                        " canonical_unit, display_name, display_unit, kind, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'consume', ?)",
                        (
                            cook_event_id,
                            item["id"],
                            key,
                            -take,
                            need["unit"],
                            item["display_name"],
                            item["unit"],
                            _now(),
                        ),
                    )
                consumed.append({
                    "id": item["id"],
                    "qty": from_canonical(take, item["unit"]),
                    "unit": item["unit"],
                    "name": item["display_name"],
                })
                remaining -= take
            if remaining > 0.000001:
                missing.append({
                    "key": key,
                    "name": need["name"],
                    "qty": remaining,
                    "unit": need["unit"],
                })
    return {"consumed": consumed, "missing": missing}


def restore_for_event(c, cook_event_id: int) -> list[dict]:
    """Compensate a cook while retaining unrelated later pantry edits."""
    existing = c.execute(
        "SELECT 1 FROM pantry_movements "
        "WHERE cook_event_id = ? AND kind = 'restore' LIMIT 1",
        (cook_event_id,),
    ).fetchone()
    if existing:
        return []
    rows = c.execute(
        "SELECT * FROM pantry_movements "
        "WHERE cook_event_id = ? AND kind = 'consume' ORDER BY id",
        (cook_event_id,),
    ).fetchall()
    restored = []
    for movement in rows:
        amount = -float(movement["delta_canonical"])
        item = None
        if movement["pantry_item_id"] is not None:
            item = c.execute(
                "SELECT * FROM pantry_items WHERE id = ?",
                (movement["pantry_item_id"],),
            ).fetchone()
        if item and (
            item["ingredient_key"] != movement["ingredient_key"]
            or not compatible(item["unit"], movement["canonical_unit"])
        ):
            # The original row was repurposed after cooking. Restoring into it
            # would mix ingredients or dimensions (for example grams into
            # pieces), so create a replacement row for the original movement.
            item = None
        if item:
            updated = float(item["canonical_quantity"] or 0) + amount
            display_quantity = from_canonical(updated, item["unit"])
            c.execute(
                "UPDATE pantry_items SET quantity = ?, canonical_quantity = ?, "
                "exhausted_at = NULL, version = version + 1 WHERE id = ?",
                (display_quantity, updated, item["id"]),
            )
            pantry_item_id = item["id"]
            display_unit = item["unit"]
            display_name = item["display_name"]
        else:
            display_unit = movement["display_unit"]
            display_name = movement["display_name"]
            display_quantity = from_canonical(amount, display_unit)
            dimension = to_canonical(1, movement["canonical_unit"])[2]
            cur = c.execute(
                "INSERT INTO pantry_items "
                "(ingredient_key, display_name, quantity, unit, source, added_at, "
                " canonical_quantity, canonical_unit, dimension) "
                "VALUES (?, ?, ?, ?, 'recipe_undo', ?, ?, ?, ?)",
                (
                    movement["ingredient_key"],
                    display_name,
                    display_quantity,
                    display_unit,
                    _now(),
                    amount,
                    movement["canonical_unit"],
                    dimension,
                ),
            )
            pantry_item_id = cur.lastrowid
        c.execute(
            "INSERT INTO pantry_movements "
            "(cook_event_id, pantry_item_id, ingredient_key, delta_canonical, "
            " canonical_unit, display_name, display_unit, kind, "
            " reverses_movement_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'restore', ?, ?)",
            (
                cook_event_id,
                pantry_item_id,
                movement["ingredient_key"],
                amount,
                movement["canonical_unit"],
                display_name,
                display_unit,
                movement["id"],
                _now(),
            ),
        )
        restored.append({
            "id": pantry_item_id,
            "qty": display_quantity,
            "unit": display_unit,
            "name": display_name,
        })
    return restored
