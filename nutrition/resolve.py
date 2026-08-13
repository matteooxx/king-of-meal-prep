"""Resolve "<qty> <unit> <name>" → macros.

The user types ingredient lines in plain language. We:
  1. Parse the line into qty + unit + name.
  2. Look up the name in nutrition.db (USDA preferred, OFF fallback).
  3. Convert the per-100g macros to per-quantity, accounting for unit
     (g/ml/tbsp/cup/piece). Volume → mass uses density approximations from
     a small table; pieces use a typical-piece-mass table.

If the lookup misses, returns None for macros and the caller (the recipe
save endpoint) falls back to manual entry.

Public surface:
  parse_line("400 g chicken thigh") -> ParsedIngredient
  resolve(ParsedIngredient) -> dict with macros + ingredient_key (or no macros if miss)
  resolve_by_ean(ean) -> dict | None
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

import config

log = logging.getLogger("king-of-meal-prep.nutrition")


# ---- unit normalization ----------------------------------------------------

# Mass conversions to grams
MASS_UNITS = {
    "g": 1, "gram": 1, "grams": 1,
    "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
    "kg": 1000, "kilo": 1000, "kilos": 1000, "kilogram": 1000, "kilograms": 1000,
    "oz": 28.35, "ounce": 28.35, "ounces": 28.35,
    "lb": 453.6, "lbs": 453.6, "pound": 453.6, "pounds": 453.6,
}

# Volume conversions to ml
VOLUME_UNITS = {
    "ml": 1, "milliliter": 1, "milliliters": 1, "millilitre": 1, "millilitres": 1,
    "l": 1000, "liter": 1000, "liters": 1000, "litre": 1000, "litres": 1000,
    "tsp": 5, "teaspoon": 5, "teaspoons": 5,
    "tbsp": 15, "tablespoon": 15, "tablespoons": 15,
    "cup": 240, "cups": 240,
    "fl oz": 29.57, "floz": 29.57,
    "pint": 473, "pints": 473,
    "quart": 946, "quarts": 946,
    "gallon": 3785,
}

COUNT_UNITS = {
    "piece", "pieces", "pc", "pcs", "item", "items", "each", "x",
}

PROFILE_MACROS = (
    ("kcal_100g", "kcal"),
    ("protein_100g", "protein_g"),
    ("carbs_100g", "carbs_g"),
    ("fat_100g", "fat_g"),
    ("fiber_100g", "fiber_g"),
)

# Approximate density (g/ml) for volume → mass conversion. Default 1.0 (water).
# Only the most common cooking ingredients; unknown lookups fall through to 1.0.
DENSITY_G_PER_ML = {
    "olive oil": 0.92, "vegetable oil": 0.92, "oil": 0.92, "butter": 0.95,
    "honey": 1.42, "milk": 1.03, "cream": 0.99, "yogurt": 1.04,
    "flour": 0.59, "sugar": 0.85, "rice": 0.85, "pasta": 0.40,
    "salt": 1.20, "pepper": 0.50,
}

# Typical mass per "piece" for items often sold/measured by count.
# Extended via the resolver as we encounter new produce.
TYPICAL_PIECE_G = {
    "egg": 50, "eggs": 50,
    "garlic clove": 4, "garlic cloves": 4,
    "clove garlic": 4, "cloves garlic": 4, "clove": 4, "cloves": 4,
    "onion": 150, "onions": 150,
    "tomato": 120, "tomatoes": 120,
    "potato": 175, "potatoes": 175,
    "lemon": 60, "lemons": 60, "lime": 50, "limes": 50,
    "carrot": 60, "carrots": 60,
    "apple": 180, "apples": 180,
    "banana": 120, "bananas": 120,
    "orange": 130, "oranges": 130,
    "bell pepper": 120, "pepper": 120, "peppers": 120,
}

PIECE_NAME_MODIFIERS = {
    "small", "medium", "large", "extra", "red", "green", "yellow", "white",
    "brown", "sweet", "ripe", "peeled",
}


@dataclass
class ParsedIngredient:
    raw: str
    quantity: Optional[float]
    unit: Optional[str]
    name: str
    name_normalized: str = field(default="")


# Regex: optional "1/2" or "1.5" or "1" qty, optional unit, then the name.
# Supports "1 1/2 cups flour" via the alt branch.
_QTY = r"(?P<qty>\d+(?:\s+\d+/\d+)?|\d*\.\d+|\d+/\d+|\d+)"
_UNITS_ALT = "|".join(
    re.escape(u)
    for u in sorted(
        set(MASS_UNITS) | set(VOLUME_UNITS) | COUNT_UNITS,
        key=len,
        reverse=True,
    )
)
_LINE_RE = re.compile(
    rf"^\s*{_QTY}\s*(?P<unit>{_UNITS_ALT})?\s+(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


def _parse_qty(s: str) -> Optional[float]:
    s = s.strip()
    if " " in s:
        whole, frac = s.split()
        if "/" in frac:
            n, d = frac.split("/")
            return float(whole) + float(n) / float(d)
    if "/" in s:
        n, d = s.split("/")
        return float(n) / float(d)
    try:
        return float(s)
    except ValueError:
        return None


def parse_line(line: str) -> ParsedIngredient:
    """Best-effort parse of a single ingredient line. Always returns a result;
    falls back to {qty: None, unit: None, name: <full line>} on no match."""
    m = _LINE_RE.match(line)
    if not m:
        name = line.strip()
        return ParsedIngredient(raw=line, quantity=None, unit=None,
                                name=name, name_normalized=_normalize_name(name))
    qty = _parse_qty(m.group("qty")) if m.group("qty") else None
    unit = (m.group("unit") or "").lower() or None
    name = m.group("name").strip()
    return ParsedIngredient(
        raw=line, quantity=qty, unit=unit, name=name,
        name_normalized=_normalize_name(name),
    )


def _normalize_name(name: str) -> str:
    """Lowercase + strip leading articles / parentheticals so 'Chicken Thigh
    (boneless)' and 'chicken thigh' both lookup the same."""
    s = name.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\b(of|the|a|an|some|fresh|chopped|diced|sliced|minced|ground|whole|raw)\b", " ", s)
    s = re.sub(r"[,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---- DB lookup -------------------------------------------------------------

_local = None

def _conn() -> sqlite3.Connection:
    """Per-thread connection to the nutrition.db file. Reuse across calls so
    a recipe-save with N ingredients doesn't open N connections."""
    global _local
    import threading
    if _local is None:
        _local = threading.local()
    c = getattr(_local, "conn", None)
    try:
        stat = os.stat(config.NUTRITION_DB)
        signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        raise sqlite3.OperationalError(str(exc)) from exc
    if c is not None and getattr(_local, "signature", None) != signature:
        c.close()
        c = None
    if c is None:
        uri = f"file:{config.NUTRITION_DB}?mode=ro&immutable=1"
        c = sqlite3.connect(
            uri, uri=True, timeout=5.0, check_same_thread=False
        )
        c.row_factory = sqlite3.Row
        _local.conn = c
        _local.signature = signature
    return c


def close_thread_conn() -> None:
    global _local
    if _local is None:
        return
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None
    _local.signature = None


def _like_pattern(value: str) -> str:
    """Escape user-controlled text for a LIKE expression with ESCAPE '\\'."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _lookup_name(name: str) -> Optional[tuple[sqlite3.Row, str]]:
    """Best-match by name, USDA preferred over OFF, exact > prefix > all-words.

    USDA ingredient names are 'Chicken, broilers or fryers, dark meat, thigh,
    meat only, raw'. Searching for 'chicken thigh' must match those rows even
    though the words aren't adjacent. So the all-words branch ANDs LIKE clauses
    per token, then orders by name length (shortest = most generic = best
    match for a casual query like 'chicken thigh').
    """
    if not name:
        return None
    try:
        c = _conn()
    except sqlite3.OperationalError:
        log.warning("nutrition.db unavailable at %s", config.NUTRITION_DB)
        return None
    try:
        # Exact match (case-insensitive), USDA first
        for src in ("usda", "off"):
            r = c.execute(
                "SELECT * FROM ingredients WHERE LOWER(name) = ? AND source = ? LIMIT 1",
                (name, src),
            ).fetchone()
            if r:
                return r, "exact_name"
        # Prefix match (USDA only — OFF prefixes are too noisy)
        r = c.execute(
            "SELECT * FROM ingredients "
            "WHERE LOWER(name) LIKE ? ESCAPE '\\' AND source='usda' "
            "ORDER BY LENGTH(name) LIMIT 1",
            (_like_pattern(name) + "%",),
        ).fetchone()
        if r:
            return r, "name_prefix"
        # All-words match (each token must appear, in any order). USDA first;
        # tokens like 'raw' / 'cooked' / 'meat only' are the differentiator
        # but we don't require them — order by shortest name.
        tokens = [t for t in name.split() if len(t) > 2]
        if tokens:
            where = " AND ".join(
                "LOWER(name) LIKE ? ESCAPE '\\'" for _ in tokens
            )
            params = [f"%{_like_pattern(t.lower())}%" for t in tokens]
            # Match the user's casual intent ("400 g chicken thigh" means raw
            # meat, not e.g. skin or feet). Order by:
            #   1. USDA before OFF
            #   2. Penalize cuts the user almost certainly didn't mean (skin,
            #      fat, feet, neck, gizzard, liver, heart) — these can be at
            #      the right name length but very different macros.
            #   3. Prefer 'raw' since macros change a lot when cooked.
            #   4. Shortest name (most generic match).
            # SQLite has no REGEXP by default; chain LIKEs into one CASE expr.
            noise_words = ["skin", "fat", "feet", "neck", "gizzard", "liver",
                           "heart", "gravy", "broth", "spread"]
            noise_clause = " OR ".join(
                f"LOWER(name) LIKE '%{w}%'" for w in noise_words
            )
            sql = (
                f"SELECT * FROM ingredients WHERE {where} "
                f"ORDER BY (source = 'usda') DESC, "
                f"  CASE WHEN ({noise_clause}) THEN 1 ELSE 0 END, "
                f"  CASE WHEN LOWER(name) LIKE '%raw%' THEN 0 ELSE 1 END, "
                f"  LENGTH(name) "
                f"LIMIT 1"
            )
            r = c.execute(sql, params).fetchone()
            if r:
                return r, "all_words"
        # Final substring fallback (single word search like 'flour')
        r = c.execute(
            "SELECT * FROM ingredients WHERE LOWER(name) LIKE ? ESCAPE '\\' "
            "ORDER BY (source = 'usda') DESC, LENGTH(name) LIMIT 1",
            ("%" + _like_pattern(name) + "%",),
        ).fetchone()
        return (r, "name_substring") if r else None
    finally:
        # Do NOT close — connection is thread-local + reused across calls.
        pass


def _typical_piece_grams(name_norm: str) -> Optional[float]:
    """Return a count estimate only when the name describes the whole food."""
    name_tokens = name_norm.split()
    for name, grams in sorted(
        TYPICAL_PIECE_G.items(),
        key=lambda item: len(item[0].split()),
        reverse=True,
    ):
        food_tokens = name.split()
        for start in range(len(name_tokens) - len(food_tokens) + 1):
            if name_tokens[start:start + len(food_tokens)] != food_tokens:
                continue
            extras = (
                name_tokens[:start]
                + name_tokens[start + len(food_tokens):]
            )
            if all(token in PIECE_NAME_MODIFIERS for token in extras):
                return float(grams)
    return None


def _to_grams(qty: float, unit: Optional[str], name_norm: str) -> Optional[float]:
    """Convert (qty, unit) pair to grams. Returns None if the unit is
    something we don't know how to convert (e.g. 'pinch', 'splash')."""
    if not unit:
        # Bare number = piece count for known per-piece foods
        piece_grams = _typical_piece_grams(name_norm)
        return qty * piece_grams if piece_grams is not None else None
    u = unit.lower()
    if u in MASS_UNITS:
        return qty * MASS_UNITS[u]
    if u in VOLUME_UNITS:
        ml = qty * VOLUME_UNITS[u]
        density = 1.0
        for k, d in DENSITY_G_PER_ML.items():
            if k in name_norm:
                density = d
                break
        return ml * density
    if u in COUNT_UNITS:
        piece_grams = _typical_piece_grams(name_norm)
        return qty * piece_grams if piece_grams is not None else None
    return None


def _nutrition_metadata(
    row: sqlite3.Row,
    basis: str,
    *,
    amount_known: bool,
) -> dict[str, str]:
    source = row["source"] if row["source"] in {"usda", "off"} else "unknown"
    macro_count = sum(
        row[key] is not None
        for key in (
            "kcal_100g",
            "protein_100g",
            "carbs_100g",
            "fat_100g",
            "fiber_100g",
        )
    )
    if not macro_count or not amount_known:
        confidence = "unknown"
    elif source == "usda" and basis in {
        "exact_name", "barcode_exact", "user_selected",
    }:
        confidence = "high" if macro_count >= 4 else "medium"
    elif source == "usda" and basis in {"name_prefix", "all_words"}:
        confidence = "medium"
    elif source == "off" and basis in {
        "exact_name", "barcode_exact", "user_selected",
    }:
        confidence = "medium" if macro_count >= 4 else "low"
    else:
        confidence = "low"
    return {
        "nutrition_source": source,
        "nutrition_confidence": confidence,
        "nutrition_basis": basis if amount_known else "amount_not_convertible",
    }


def resolve(line_or_parsed) -> dict:
    """Resolve to a dict with as much info as we have:
        {
          ingredient_key: str,
          display_name: str,
          quantity: float|None, unit: str|None,
          grams: float|None,
          kcal: float|None, protein_g: float|None,
          carbs_g: float|None, fat_g: float|None, fiber_g: float|None,
          source: 'usda'|'off'|'manual',
          resolved: bool   # True iff macros were filled from the DB
        }
    """
    p = line_or_parsed if isinstance(line_or_parsed, ParsedIngredient) else parse_line(line_or_parsed)
    out = {
        "ingredient_key": _slugify(p.name),
        "display_name": p.name,
        "quantity": p.quantity,
        "unit": p.unit,
        "grams": None,
        "kcal": None, "protein_g": None, "carbs_g": None, "fat_g": None, "fiber_g": None,
        "source": "manual",
        "resolved": False,
        "nutrition_source": "manual",
        "nutrition_confidence": "unknown",
        "nutrition_basis": "no_dataset_match",
        "nutrition_status": (
            "missing_amount"
            if p.quantity is None
            else "unknown_unit"
            if _to_grams(p.quantity, p.unit, p.name_normalized) is None
            else "no_match"
        ),
    }
    match = _lookup_name(p.name_normalized) or _lookup_name(p.name.lower())
    if not match:
        return out
    row, basis = match
    profile = {
        "ingredient_key": row["key"],
        "source": row["source"],
        **{key: row[key] for key, _ in PROFILE_MACROS},
        **_nutrition_metadata(row, basis, amount_known=True),
    }
    return scale_profile(
        profile,
        quantity=p.quantity,
        unit=p.unit,
        display_name=p.name,
        basis=basis,
    )


def scale_profile(
    profile: dict,
    *,
    quantity: float | None,
    unit: str | None,
    display_name: str,
    basis: str | None = None,
) -> dict:
    """Scale a per-100 g profile to an entered amount.

    This is shared by recipe review, pantry logging, and explicit dataset
    selections so each path uses the same conversion and completeness rules.
    """
    normalized_name = _normalize_name(display_name)
    grams = (
        _to_grams(float(quantity), unit, normalized_name)
        if quantity is not None
        else None
    )
    has_profile = any(profile.get(key) is not None for key, _ in PROFILE_MACROS)
    if quantity is None:
        status = "missing_amount"
    elif grams is None:
        status = "unknown_unit"
    elif not has_profile:
        status = "no_nutrition"
    else:
        status = "counted"

    source = (
        profile.get("nutrition_source")
        or profile.get("source")
        or "unknown"
    )
    confidence = profile.get("nutrition_confidence") or "unknown"
    match_basis = basis or profile.get("nutrition_basis") or "user_selected"
    if status == "missing_amount":
        confidence = "unknown"
        nutrition_basis = "amount_missing"
    elif status == "unknown_unit":
        confidence = "unknown"
        nutrition_basis = "unit_not_convertible"
    else:
        nutrition_basis = match_basis

    output = {
        "ingredient_key": (
            profile.get("ingredient_key")
            or profile.get("key")
            or _slugify(display_name)
        ),
        "display_name": display_name,
        "quantity": quantity,
        "unit": unit,
        "grams": grams,
        "kcal": None,
        "protein_g": None,
        "carbs_g": None,
        "fat_g": None,
        "fiber_g": None,
        "source": source,
        "resolved": status == "counted",
        "nutrition_source": source,
        "nutrition_confidence": confidence,
        "nutrition_basis": nutrition_basis,
        "nutrition_status": status,
    }
    factor = grams / 100.0 if grams is not None else None
    for profile_key, output_key in PROFILE_MACROS:
        value = profile.get(profile_key)
        if value is not None and factor is not None:
            output[output_key] = round(float(value) * factor, 1)
    return output


def resolve_fields(
    *,
    display_name: str,
    quantity: float | None,
    unit: str | None,
    ingredient_key: str | None = None,
) -> dict:
    """Resolve structured form fields without flattening them back to text."""
    selected = by_key(ingredient_key)
    if selected:
        return scale_profile(
            selected,
            quantity=quantity,
            unit=unit,
            display_name=display_name,
            basis="user_selected",
        )
    parsed = ParsedIngredient(
        raw=display_name,
        quantity=quantity,
        unit=unit,
        name=display_name,
        name_normalized=_normalize_name(display_name),
    )
    return resolve(parsed)


def resolve_by_ean(ean: str) -> Optional[dict]:
    if not ean:
        return None
    try:
        c = _conn()
    except sqlite3.OperationalError:
        return None
    try:
        r = c.execute(
            "SELECT i.* FROM barcodes b JOIN ingredients i ON i.key = b.ingredient_key "
            "WHERE b.ean = ?",
            (ean,),
        ).fetchone()
        if not r:
            return None
        return {
            "ingredient_key": r["key"],
            "display_name": r["name"],
            "kcal_100g": r["kcal_100g"],
            "protein_100g": r["protein_100g"],
            "carbs_100g": r["carbs_100g"],
            "fat_100g": r["fat_100g"],
            "fiber_100g": r["fiber_100g"],
            "source": r["source"],
            "nutrition_status": (
                "counted"
                if any(r[key] is not None for key, _ in PROFILE_MACROS)
                else "no_nutrition"
            ),
            **_nutrition_metadata(
                r,
                "barcode_exact",
                amount_known=True,
            ),
        }
    finally:
        # Do NOT close — connection is thread-local + reused across calls.
        pass


def search(query: str, *, limit: int = 6) -> list[dict]:
    """Return bounded review suggestions without claiming an exact match."""
    normalized = _normalize_name(query)[:120]
    if not normalized:
        return []
    limit = max(1, min(int(limit), 12))
    try:
        connection = _conn()
    except sqlite3.OperationalError:
        return []

    rows: list[tuple[sqlite3.Row, str]] = []
    seen: set[str] = set()

    def add(found, basis: str) -> None:
        for row in found:
            if row["key"] in seen:
                continue
            seen.add(row["key"])
            rows.append((row, basis))
            if len(rows) >= limit:
                break

    add(
        connection.execute(
            "SELECT * FROM ingredients "
            "WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "ORDER BY (source = 'usda') DESC, LENGTH(name) LIMIT ?",
            (_like_pattern(normalized) + "%", limit),
        ).fetchall(),
        "name_prefix",
    )
    if len(rows) < limit:
        add(
            connection.execute(
                "SELECT * FROM ingredients "
                "WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "ORDER BY (source = 'usda') DESC, LENGTH(name) LIMIT ?",
                ("%" + _like_pattern(normalized) + "%", limit * 2),
            ).fetchall(),
            "name_substring",
        )

    output = []
    for row, basis in rows:
        output.append({
            "ingredient_key": row["key"],
            "display_name": row["name"],
            "kcal_100g": row["kcal_100g"],
            "protein_100g": row["protein_100g"],
            "carbs_100g": row["carbs_100g"],
            "fat_100g": row["fat_100g"],
            "fiber_100g": row["fiber_100g"],
            "source": row["source"],
            "nutrition_status": (
                "counted"
                if any(row[key] is not None for key, _ in PROFILE_MACROS)
                else "no_nutrition"
            ),
            **_nutrition_metadata(row, basis, amount_known=True),
        })
    return output


def by_key(ingredient_key: str | None) -> dict | None:
    """Resolve an explicitly selected dataset identity."""
    key = (ingredient_key or "").strip()
    if not key or len(key) > 160:
        return None
    try:
        connection = _conn()
    except sqlite3.OperationalError:
        return None
    row = connection.execute(
        "SELECT * FROM ingredients WHERE key = ? LIMIT 1",
        (key,),
    ).fetchone()
    if not row:
        return None
    return {
        "ingredient_key": row["key"],
        "display_name": row["name"],
        "kcal_100g": row["kcal_100g"],
        "protein_100g": row["protein_100g"],
        "carbs_100g": row["carbs_100g"],
        "fat_100g": row["fat_100g"],
        "fiber_100g": row["fiber_100g"],
        "source": row["source"],
        "nutrition_status": (
            "counted"
            if any(row[key] is not None for key, _ in PROFILE_MACROS)
            else "no_nutrition"
        ),
        **_nutrition_metadata(row, "user_selected", amount_known=True),
    }


def _slugify(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", name.lower()).strip("_")
    return s[:80] or "unknown"
