"""Three-tier barcode lookup with bounded Open Food Facts fallback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
from typing import Any

import httpx

import db
from nutrition import resolve as nutrition_resolve
from pantry.units import canonical_key

from .gtin import Barcode, parse as parse_barcode
from .off import parse_product


log = logging.getLogger("king-of-meal-prep.barcodes")

OFF_API_ROOT = "https://world.openfoodfacts.org/api/v2/product"
OFF_FIELDS = ",".join((
    "code",
    "product_name",
    "product_name_en",
    "product_name_it",
    "generic_name",
    "generic_name_en",
    "generic_name_it",
    "brands",
    "quantity",
    "product_quantity",
    "product_quantity_unit",
    "nutriments",
))
MAX_RESPONSE_BYTES = 512 * 1024
POSITIVE_CACHE_TTL = timedelta(days=90)
NEGATIVE_CACHE_TTL = timedelta(hours=24)


class OnlineLookupError(RuntimeError):
    """Open Food Facts could not be reached or returned an invalid response."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _proposal(product: dict[str, Any], source: str) -> dict[str, Any]:
    nutrition_available = bool(product.get("nutrition_available"))
    return {
        "ok": True,
        "source": source,
        "proposal": {
            "ingredient_key": product["ingredient_key"],
            "display_name": product["display_name"],
            "quantity": product.get("quantity") or 1,
            "unit": product.get("unit") or "piece",
        },
        "details": {
            "brand": product.get("brand") or "",
            "nutrition_available": nutrition_available,
            "nutrition": {
                "source": product.get("nutrition_source") or "unknown",
                "confidence": (
                    product.get("nutrition_confidence") or "unknown"
                ),
                "basis": product.get("nutrition_basis") or "unknown",
                "available": nutrition_available,
                "kcal_100g": product.get("kcal_100g"),
                "protein_100g": product.get("protein_100g"),
                "carbs_100g": product.get("carbs_100g"),
                "fat_100g": product.get("fat_100g"),
                "fiber_100g": product.get("fiber_100g"),
            },
        },
    }


def _find_user(barcode: Barcode) -> dict[str, Any] | None:
    connection = db._conn()
    for alias in barcode.aliases:
        row = connection.execute(
            "SELECT ean, display_name, quantity, unit "
            "FROM user_barcodes WHERE ean = ?",
            (alias,),
        ).fetchone()
        if not row:
            continue
        connection.execute(
            "UPDATE user_barcodes SET use_count = use_count + 1 WHERE ean = ?",
            (row["ean"],),
        )
        product = {
            "ingredient_key": canonical_key(row["display_name"]),
            "display_name": row["display_name"],
            "quantity": row["quantity"] or 1,
            "unit": row["unit"] or "piece",
            "brand": "",
            "nutrition_available": False,
            "nutrition_source": "user",
            "nutrition_confidence": "unknown",
            "nutrition_basis": "reviewed_identity_without_nutrition",
        }
        cached_row, _ = _cached(barcode)
        profile = (
            _product_from_cache(cached_row)
            if cached_row and cached_row["status"] == "found"
            else _find_bundled(barcode)
        )
        if profile:
            product.update({
                "ingredient_key": profile["ingredient_key"],
                "kcal_100g": profile.get("kcal_100g"),
                "protein_100g": profile.get("protein_100g"),
                "carbs_100g": profile.get("carbs_100g"),
                "fat_100g": profile.get("fat_100g"),
                "fiber_100g": profile.get("fiber_100g"),
                "nutrition_available": profile.get(
                    "nutrition_available", False
                ),
                "nutrition_source": (
                    profile.get("nutrition_source")
                    or profile.get("source")
                    or "unknown"
                ),
                "nutrition_confidence": (
                    profile.get("nutrition_confidence") or "unknown"
                ),
                "nutrition_basis": (
                    "remembered_" +
                    (profile.get("nutrition_basis") or "barcode_profile")
                ),
            })
        return product
    return None


def _find_bundled(barcode: Barcode) -> dict[str, Any] | None:
    for alias in barcode.aliases:
        product = nutrition_resolve.resolve_by_ean(alias)
        if product:
            return {
                **product,
                "quantity": 1,
                "unit": "piece",
                "brand": "",
                "nutrition_available": any(
                    product.get(key) is not None
                    for key in (
                        "kcal_100g",
                        "protein_100g",
                        "carbs_100g",
                        "fat_100g",
                        "fiber_100g",
                    )
                ),
            }
    return None


def _cached(barcode: Barcode) -> tuple[sqlite3.Row | None, bool]:
    row = db._conn().execute(
        "SELECT * FROM barcode_cache WHERE ean = ?",
        (barcode.canonical,),
    ).fetchone()
    if not row:
        return None, False
    try:
        fresh = datetime.fromisoformat(row["expires_at"]) > _now()
    except (TypeError, ValueError):
        fresh = False
    return row, fresh


def _product_from_cache(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ingredient_key": f"off:{row['ean']}",
        "display_name": row["display_name"],
        "brand": row["brand"] or "",
        "quantity": row["package_quantity"] or 1,
        "unit": row["package_unit"] or "piece",
        "kcal_100g": row["kcal_100g"],
        "protein_100g": row["protein_100g"],
        "carbs_100g": row["carbs_100g"],
        "fat_100g": row["fat_100g"],
        "fiber_100g": row["fiber_100g"],
        "nutrition_available": any(
            row[key] is not None
            for key in (
                "kcal_100g",
                "protein_100g",
                "carbs_100g",
                "fat_100g",
                "fiber_100g",
            )
        ),
        "nutrition_source": "off",
        "nutrition_confidence": (
            "medium"
            if sum(
                row[key] is not None
                for key in (
                    "kcal_100g",
                    "protein_100g",
                    "carbs_100g",
                    "fat_100g",
                    "fiber_100g",
                )
            ) >= 4
            else "low"
            if any(
                row[key] is not None
                for key in (
                    "kcal_100g",
                    "protein_100g",
                    "carbs_100g",
                    "fat_100g",
                    "fiber_100g",
                )
            )
            else "unknown"
        ),
        "nutrition_basis": "cached_barcode",
    }


def _save_found(barcode: Barcode, product: dict[str, Any]) -> None:
    now = _now()
    db._conn().execute(
        "INSERT INTO barcode_cache "
        "(ean, status, display_name, brand, package_quantity, package_unit, "
        "kcal_100g, protein_100g, carbs_100g, fat_100g, fiber_100g, "
        "fetched_at, expires_at) "
        "VALUES (?, 'found', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ean) DO UPDATE SET "
        "status = excluded.status, display_name = excluded.display_name, "
        "brand = excluded.brand, package_quantity = excluded.package_quantity, "
        "package_unit = excluded.package_unit, kcal_100g = excluded.kcal_100g, "
        "protein_100g = excluded.protein_100g, carbs_100g = excluded.carbs_100g, "
        "fat_100g = excluded.fat_100g, fiber_100g = excluded.fiber_100g, "
        "fetched_at = excluded.fetched_at, expires_at = excluded.expires_at",
        (
            barcode.canonical,
            product["display_name"],
            product.get("brand") or "",
            product.get("quantity") or 1,
            product.get("unit") or "piece",
            product.get("kcal_100g"),
            product.get("protein_100g"),
            product.get("carbs_100g"),
            product.get("fat_100g"),
            product.get("fiber_100g"),
            now.isoformat(),
            (now + POSITIVE_CACHE_TTL).isoformat(),
        ),
    )


def _save_not_found(barcode: Barcode) -> None:
    now = _now()
    db._conn().execute(
        "INSERT INTO barcode_cache "
        "(ean, status, fetched_at, expires_at) "
        "VALUES (?, 'not_found', ?, ?) "
        "ON CONFLICT(ean) DO UPDATE SET "
        "status = excluded.status, display_name = NULL, brand = NULL, "
        "package_quantity = NULL, package_unit = NULL, kcal_100g = NULL, "
        "protein_100g = NULL, carbs_100g = NULL, fat_100g = NULL, "
        "fiber_100g = NULL, fetched_at = excluded.fetched_at, "
        "expires_at = excluded.expires_at",
        (
            barcode.canonical,
            now.isoformat(),
            (now + NEGATIVE_CACHE_TTL).isoformat(),
        ),
    )


def _fetch_code(code: str, requested: Barcode) -> dict[str, Any] | None:
    url = httpx.URL(
        f"{OFF_API_ROOT}/{code}.json",
        params={"fields": OFF_FIELDS},
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "KingOfMealPrep/2.1 (self-hosted meal planner)",
    }
    try:
        with httpx.Client(
            timeout=6.0,
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    return None
                if response.status_code != 200:
                    raise OnlineLookupError(
                        f"Open Food Facts returned HTTP {response.status_code}"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise OnlineLookupError(
                            "Open Food Facts response exceeded the size limit"
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise OnlineLookupError("Open Food Facts request failed") from exc

    try:
        payload = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OnlineLookupError("Open Food Facts returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OnlineLookupError(
            "Open Food Facts returned an invalid response"
        )
    if payload.get("status") not in (1, "1"):
        return None
    return parse_product(payload.get("product"), requested)


def _fetch_online(barcode: Barcode) -> dict[str, Any] | None:
    for code in barcode.aliases:
        product = _fetch_code(code, barcode)
        if product:
            return product
    return None


def lookup(value: str, *, online: bool) -> dict[str, Any] | None:
    barcode = parse_barcode(value)

    user_product = _find_user(barcode)
    if user_product:
        return _proposal(user_product, "local")

    bundled_product = _find_bundled(barcode)
    if bundled_product:
        return _proposal(bundled_product, "off_index")

    cache_row, cache_fresh = _cached(barcode)
    stale_product = None
    if cache_row and cache_row["status"] == "found":
        stale_product = _product_from_cache(cache_row)
        if cache_fresh:
            return _proposal(stale_product, "off_cache")
    elif cache_row and cache_row["status"] == "not_found" and cache_fresh:
        return None

    if not online:
        return _proposal(stale_product, "off_cache") if stale_product else None

    try:
        product = _fetch_online(barcode)
    except OnlineLookupError:
        if stale_product:
            return _proposal(stale_product, "off_cache")
        raise
    if product:
        _save_found(barcode, product)
        return _proposal(product, "off_online")
    if stale_product:
        _save_not_found(barcode)
        return None
    _save_not_found(barcode)
    return None


def local_nutrition_profile(barcode: Barcode) -> dict[str, Any] | None:
    """Return the best already-local per-100 g profile without network I/O."""
    row, _ = _cached(barcode)
    if row and row["status"] == "found":
        return _product_from_cache(row)
    return _find_bundled(barcode)


def remember_user_barcode(
    connection: sqlite3.Connection,
    barcode: Barcode,
    *,
    display_name: str,
    quantity: float,
    unit: str,
) -> None:
    connection.execute(
        "INSERT INTO user_barcodes "
        "(ean, display_name, quantity, unit, added_at, use_count) "
        "VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(ean) DO UPDATE SET "
        "display_name = excluded.display_name, quantity = excluded.quantity, "
        "unit = excluded.unit, use_count = use_count + 1",
        (
            barcode.canonical,
            display_name,
            quantity,
            unit,
            _now().isoformat(),
        ),
    )
