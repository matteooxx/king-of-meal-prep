"""Durable review queue for unknown barcodes, product photos, and OCR lines."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any
from urllib.parse import quote

import db
from barcodes.gtin import Barcode, BarcodeError, parse as parse_barcode
from nutrition import resolve as nutrition_resolve
from pantry import dao as pantry_dao
from pantry.units import canonical_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _off_url(barcode: str | None) -> str | None:
    if not barcode:
        return None
    try:
        parsed = parse_barcode(barcode)
    except BarcodeError:
        return None
    if not parsed.is_gtin:
        return None
    display = min(parsed.aliases, key=len)
    return (
        "https://world.openfoodfacts.org/product/"
        + quote(display, safe="")
    )


def _serialize(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item.pop("image_jpeg", None)
    item["has_image"] = bool(item.get("image_sha256"))
    item["image_url"] = (
        f"/api/recognition-inbox/{item['id']}/image"
        if item["has_image"]
        else None
    )
    item["off_url"] = _off_url(item.get("barcode_display") or item.get("barcode"))
    return item


def get(item_id: int) -> dict[str, Any] | None:
    row = db._conn().execute(
        "SELECT ri.*, r.id AS receipt_id "
        "FROM recognition_inbox ri "
        "LEFT JOIN receipt_items i ON i.id = ri.receipt_item_id "
        "LEFT JOIN receipt_imports r ON r.id = i.receipt_id "
        "WHERE ri.id = ?",
        (item_id,),
    ).fetchone()
    return _serialize(row) if row else None


def list_items(*, status: str = "open", limit: int = 100) -> list[dict]:
    rows = db._conn().execute(
        "SELECT ri.*, r.id AS receipt_id "
        "FROM recognition_inbox ri "
        "LEFT JOIN receipt_items i ON i.id = ri.receipt_item_id "
        "LEFT JOIN receipt_imports r ON r.id = i.receipt_id "
        "WHERE ri.status = ? "
        "ORDER BY ri.updated_at DESC LIMIT ?",
        (status, max(1, min(limit, 200))),
    ).fetchall()
    return [_serialize(row) for row in rows]


def record_barcode_miss(
    barcode: Barcode,
    *,
    reason: str,
) -> dict[str, Any]:
    now = _now()
    with db.tx() as connection:
        row = connection.execute(
            "SELECT id FROM recognition_inbox "
            "WHERE kind = 'barcode' AND status = 'open' AND barcode = ?",
            (barcode.canonical,),
        ).fetchone()
        if row:
            item_id = int(row["id"])
            connection.execute(
                "UPDATE recognition_inbox SET "
                "barcode_display = ?, raw_text = ?, "
                "attempt_count = attempt_count + 1, updated_at = ? "
                "WHERE id = ?",
                (barcode.raw, reason[:500], now, item_id),
            )
        else:
            cur = connection.execute(
                "INSERT INTO recognition_inbox "
                "(kind, status, barcode, barcode_display, raw_text, quantity, "
                " unit, created_at, updated_at) "
                "VALUES ('barcode', 'open', ?, ?, ?, 1, 'piece', ?, ?)",
                (
                    barcode.canonical,
                    barcode.raw,
                    reason[:500],
                    now,
                    now,
                ),
            )
            item_id = int(cur.lastrowid)
    return get(item_id)


def create_product_photo(
    *,
    image_jpeg: bytes,
    image_sha256: str,
    note: str | None = None,
    suggested_name: str | None = None,
) -> dict[str, Any]:
    now = _now()
    cur = db._conn().execute(
        "INSERT INTO recognition_inbox "
        "(kind, status, raw_text, suggested_name, quantity, unit, "
        " image_jpeg, image_sha256, created_at, updated_at) "
        "VALUES ('product_photo', 'open', ?, ?, 1, 'piece', ?, ?, ?, ?)",
        (
            (note or "")[:500] or None,
            (suggested_name or "")[:200] or None,
            image_jpeg,
            image_sha256,
            now,
            now,
        ),
    )
    return get(int(cur.lastrowid))


def attach_photo(
    item_id: int,
    *,
    image_jpeg: bytes,
    image_sha256: str,
) -> dict[str, Any] | None:
    cur = db._conn().execute(
        "UPDATE recognition_inbox SET image_jpeg = ?, image_sha256 = ?, "
        "updated_at = ? WHERE id = ? AND status = 'open'",
        (image_jpeg, image_sha256, _now(), item_id),
    )
    return get(item_id) if cur.rowcount else None


def image(item_id: int) -> bytes | None:
    row = db._conn().execute(
        "SELECT image_jpeg FROM recognition_inbox WHERE id = ?",
        (item_id,),
    ).fetchone()
    return bytes(row["image_jpeg"]) if row and row["image_jpeg"] else None


def record_receipt_line(
    connection: sqlite3.Connection,
    *,
    receipt_item_id: int,
    raw_text: str,
    suggested_name: str,
    suggested_key: str,
    quantity: float,
    unit: str,
    nutrition_source: str,
    nutrition_confidence: str,
    nutrition_basis: str | None,
) -> int:
    now = _now()
    cur = connection.execute(
        "INSERT INTO recognition_inbox "
        "(kind, status, raw_text, suggested_name, suggested_key, quantity, "
        " unit, nutrition_source, nutrition_confidence, nutrition_basis, "
        " receipt_item_id, created_at, updated_at) "
        "VALUES ('receipt_line', 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            raw_text[:500],
            suggested_name[:200],
            suggested_key[:160],
            quantity,
            unit,
            nutrition_source,
            nutrition_confidence,
            nutrition_basis,
            receipt_item_id,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def suggestions(item_id: int, query: str) -> list[dict]:
    row = db._conn().execute(
        "SELECT id FROM recognition_inbox WHERE id = ? AND status = 'open'",
        (item_id,),
    ).fetchone()
    if not row:
        return []
    return nutrition_resolve.search(query, limit=6)


def resolve_item(
    item_id: int,
    *,
    display_name: str,
    ingredient_key: str | None,
    quantity: float,
    unit: str,
    expires_on: str | None,
    add_to_pantry: bool,
) -> dict[str, Any] | None:
    now = _now()
    pantry_item_id = None
    receipt_id = None
    with db.tx() as connection:
        row = connection.execute(
            "SELECT * FROM recognition_inbox "
            "WHERE id = ? AND status = 'open'",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        proposed_key = ingredient_key or row["suggested_key"]
        selected = nutrition_resolve.by_key(proposed_key)
        key = canonical_key(
            display_name,
            selected["ingredient_key"] if selected else proposed_key,
        )
        nutrition_source = (
            selected["nutrition_source"] if selected else "user"
        )
        nutrition_confidence = (
            selected["nutrition_confidence"] if selected else "low"
        )
        nutrition_basis = (
            selected["nutrition_basis"]
            if selected
            else "user_reviewed_identity"
        )

        if row["kind"] == "receipt_line" and row["receipt_item_id"]:
            receipt_item = connection.execute(
                "SELECT receipt_id FROM receipt_items WHERE id = ?",
                (row["receipt_item_id"],),
            ).fetchone()
            if receipt_item:
                receipt_id = int(receipt_item["receipt_id"])
                pantry_match = connection.execute(
                    "SELECT id FROM pantry_items "
                    "WHERE exhausted_at IS NULL AND "
                    "(ingredient_key = ? OR LOWER(display_name) = LOWER(?)) "
                    "ORDER BY (ingredient_key = ?) DESC, "
                    "(expires_on IS NULL), expires_on, id LIMIT 1",
                    (key, display_name, key),
                ).fetchone()
                connection.execute(
                    "UPDATE receipt_items SET display_name = ?, quantity = ?, "
                    "unit = ?, ingredient_key = ?, nutrition_source = ?, "
                    "nutrition_confidence = ?, nutrition_basis = ?, "
                    "matched_pantry_item_id = ?, "
                    "action = CASE WHEN duplicate_of_id IS NOT NULL THEN 'skip' "
                    "WHEN ? IS NOT NULL THEN 'merge' ELSE 'add' END "
                    "WHERE id = ?",
                    (
                        display_name,
                        quantity,
                        unit,
                        key,
                        nutrition_source,
                        nutrition_confidence,
                        nutrition_basis,
                        pantry_match["id"] if pantry_match else None,
                        pantry_match["id"] if pantry_match else None,
                        row["receipt_item_id"],
                    ),
                )
        elif add_to_pantry:
            pantry_item_id = pantry_dao.add(
                ingredient_key=key,
                display_name=display_name,
                quantity=quantity,
                unit=unit,
                expires_on=expires_on,
                source="barcode" if row["barcode"] else "manual",
                ean=row["barcode"],
                nutrition_profile=selected,
            )

        if row["barcode"]:
            from barcodes import service as barcode_service

            barcode_service.remember_user_barcode(
                connection,
                parse_barcode(row["barcode"]),
                display_name=display_name,
                quantity=quantity,
                unit=unit,
            )

        connection.execute(
            "UPDATE recognition_inbox SET status = 'resolved', "
            "suggested_name = ?, suggested_key = ?, quantity = ?, unit = ?, "
            "nutrition_source = ?, nutrition_confidence = ?, "
            "nutrition_basis = ?, updated_at = ?, resolved_at = ? "
            "WHERE id = ?",
            (
                display_name,
                key,
                quantity,
                unit,
                nutrition_source,
                nutrition_confidence,
                nutrition_basis,
                now,
                now,
                item_id,
            ),
        )
    return {
        "item": get(item_id),
        "pantry_item_id": pantry_item_id,
        "receipt_id": receipt_id,
    }


def dismiss(item_id: int) -> bool:
    now = _now()
    cur = db._conn().execute(
        "UPDATE recognition_inbox SET status = 'dismissed', updated_at = ?, "
        "resolved_at = ? WHERE id = ? AND status = 'open'",
        (now, now, item_id),
    )
    return cur.rowcount > 0


def resolve_receipt_queue(
    connection: sqlite3.Connection,
    receipt_id: int,
) -> None:
    now = _now()
    connection.execute(
        "UPDATE recognition_inbox SET status = 'resolved', "
        "updated_at = ?, resolved_at = ? "
        "WHERE status = 'open' AND receipt_item_id IN "
        "(SELECT id FROM receipt_items WHERE receipt_id = ?)",
        (now, now, receipt_id),
    )


def resolve_barcode_queue(
    connection: sqlite3.Connection,
    barcode: Barcode,
    *,
    display_name: str,
    ingredient_key: str,
    quantity: float,
    unit: str,
) -> None:
    now = _now()
    connection.execute(
        "UPDATE recognition_inbox SET status = 'resolved', "
        "suggested_name = ?, suggested_key = ?, quantity = ?, unit = ?, "
        "nutrition_source = 'user', nutrition_confidence = 'unknown', "
        "nutrition_basis = 'reviewed_identity_without_nutrition', "
        "updated_at = ?, resolved_at = ? "
        "WHERE kind = 'barcode' AND status = 'open' AND barcode = ?",
        (
            display_name,
            ingredient_key,
            quantity,
            unit,
            now,
            now,
            barcode.canonical,
        ),
    )
