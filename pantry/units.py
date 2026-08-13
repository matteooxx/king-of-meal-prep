"""Canonical ingredient identities and quantity conversion.

Pantry rows keep the user's display quantity/unit, but comparisons and
movements use a base quantity:

* mass -> grams
* volume -> millilitres
* count -> pieces
* unknown units -> an isolated custom dimension
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    name: str
    dimension: str
    base_unit: str
    factor: float


_UNIT_ALIASES: dict[str, Unit] = {}


def _register(names: tuple[str, ...], dimension: str, base_unit: str, factor: float) -> None:
    unit = Unit(names[0], dimension, base_unit, factor)
    for name in names:
        _UNIT_ALIASES[name] = unit


_register(("g", "gram", "grams"), "mass", "g", 1.0)
_register(("kg", "kilogram", "kilograms", "kilo", "kilos"), "mass", "g", 1000.0)
_register(("mg", "milligram", "milligrams"), "mass", "g", 0.001)
_register(("oz", "ounce", "ounces"), "mass", "g", 28.349523125)
_register(("lb", "lbs", "pound", "pounds"), "mass", "g", 453.59237)

_register(("ml", "millilitre", "millilitres", "milliliter", "milliliters"),
          "volume", "ml", 1.0)
_register(("l", "litre", "litres", "liter", "liters"), "volume", "ml", 1000.0)
_register(("tsp", "teaspoon", "teaspoons"), "volume", "ml", 5.0)
_register(("tbsp", "tablespoon", "tablespoons"), "volume", "ml", 15.0)
_register(("cup", "cups"), "volume", "ml", 240.0)
_register(("fl oz", "floz", "fluid ounce", "fluid ounces"),
          "volume", "ml", 29.5735295625)
_register(("pint", "pints"), "volume", "ml", 473.176473)

_register(("piece", "pieces", "pc", "pcs", "item", "items", "each", "x"),
          "count", "piece", 1.0)


def normalize_unit(value: str | None) -> str:
    unit = re.sub(r"\s+", " ", (value or "piece").strip().lower())
    return unit or "piece"


def unit_info(value: str | None) -> Unit:
    normalized = normalize_unit(value)
    known = _UNIT_ALIASES.get(normalized)
    if known:
        return known
    safe = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")[:30] or "unit"
    return Unit(normalized, f"custom:{safe}", normalized, 1.0)


def to_canonical(quantity: float, unit: str | None) -> tuple[float, str, str]:
    amount = float(quantity)
    if not math.isfinite(amount):
        raise ValueError("quantity must be finite")
    info = unit_info(unit)
    return amount * info.factor, info.base_unit, info.dimension


def from_canonical(quantity: float, unit: str | None) -> float:
    info = unit_info(unit)
    return float(quantity) / info.factor


def compatible(unit_a: str | None, unit_b: str | None) -> bool:
    return unit_info(unit_a).dimension == unit_info(unit_b).dimension


def ingredient_slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name or "").encode(
        "ascii", "ignore"
    ).decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:80] or "unnamed"


def canonical_key(display_name: str, proposed: str | None = None) -> str:
    key = (proposed or "").strip()
    if key and key.lower() != "unknown":
        return key[:160]
    return f"name:{ingredient_slug(display_name)}"
