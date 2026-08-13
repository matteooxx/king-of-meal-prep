"""Normalize Open Food Facts products into the app's barcode model."""
from __future__ import annotations

import math
import re
from typing import Any

from .gtin import Barcode, BarcodeError, parse as parse_barcode


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0 or result > 1_000_000:
        return None
    return result


def _package_quantity(product: dict[str, Any]) -> tuple[float, str]:
    unit_aliases = {
        "g": (1.0, "g"),
        "kg": (1.0, "kg"),
        "ml": (1.0, "ml"),
        "cl": (10.0, "ml"),
        "dl": (100.0, "ml"),
        "l": (1.0, "l"),
        "oz": (1.0, "oz"),
        "lb": (1.0, "lb"),
    }
    quantity = _number(product.get("product_quantity"))
    unit = _text(product.get("product_quantity_unit"), 20).lower()
    if quantity and unit in unit_aliases:
        factor, normalized = unit_aliases[unit]
        return quantity * factor, normalized

    quantity_text = _text(product.get("quantity"), 80).lower().replace(",", ".")
    multipack = re.search(
        r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*"
        r"(kg|g|ml|cl|dl|l|oz|lb)\b",
        quantity_text,
    )
    if multipack:
        count = _number(multipack.group(1))
        amount = _number(multipack.group(2))
        factor, normalized = unit_aliases[multipack.group(3)]
        if count and amount:
            return count * amount * factor, normalized

    single = re.search(
        r"(\d+(?:\.\d+)?)\s*(kg|g|ml|cl|dl|l|oz|lb)\b",
        quantity_text,
    )
    if single:
        amount = _number(single.group(1))
        factor, normalized = unit_aliases[single.group(2)]
        if amount:
            return amount * factor, normalized
    return 1.0, "piece"


def _barcode_for_product(product: dict[str, Any], requested: Barcode) -> Barcode:
    code = _text(product.get("code"), 32)
    if code:
        try:
            return parse_barcode(code)
        except BarcodeError:
            pass
    return requested


def parse_product(
    product: dict[str, Any] | None,
    requested: Barcode,
) -> dict[str, Any] | None:
    if not isinstance(product, dict):
        return None
    name = next((
        candidate
        for candidate in (
            _text(product.get("product_name_en"), 200),
            _text(product.get("product_name"), 200),
            _text(product.get("product_name_it"), 200),
            _text(product.get("generic_name_en"), 200),
            _text(product.get("generic_name"), 200),
            _text(product.get("generic_name_it"), 200),
        )
        if candidate
    ), "")
    if not name:
        return None

    brand = _text(product.get("brands"), 160).split(",", 1)[0].strip()
    display_name = name
    if brand and brand.casefold() not in name.casefold():
        display_name = f"{brand} {name}"[:200]

    barcode = _barcode_for_product(product, requested)
    nutriments = product.get("nutriments")
    if not isinstance(nutriments, dict):
        nutriments = {}
    kcal = _number(nutriments.get("energy-kcal_100g"))
    if kcal is None:
        energy_kj = _number(
            nutriments.get("energy-kj_100g")
            or nutriments.get("energy_100g")
        )
        kcal = energy_kj / 4.184 if energy_kj is not None else None
    quantity, unit = _package_quantity(product)
    macros = {
        "kcal_100g": kcal,
        "protein_100g": _number(nutriments.get("proteins_100g")),
        "carbs_100g": _number(nutriments.get("carbohydrates_100g")),
        "fat_100g": _number(nutriments.get("fat_100g")),
        "fiber_100g": _number(nutriments.get("fiber_100g")),
    }
    macro_count = sum(value is not None for value in macros.values())
    return {
        "ean": barcode.canonical,
        "ingredient_key": f"off:{barcode.canonical}",
        "display_name": display_name,
        "brand": brand,
        "quantity": quantity,
        "unit": unit,
        **macros,
        "nutrition_available": macro_count > 0,
        "nutrition_source": "off",
        "nutrition_confidence": (
            "medium" if macro_count >= 4 else "low" if macro_count else "unknown"
        ),
        "nutrition_basis": "barcode_exact",
    }
