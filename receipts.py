"""Persistent receipt review, pantry reconciliation, and price history."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import sqlite3
from typing import Any

import db
from nutrition import resolve as nutrition_resolve
from pantry import dao as pantry_dao
from pantry.units import (
    canonical_key,
    from_canonical,
    normalize_unit,
    to_canonical,
)


class ReceiptConflictError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_PRICE_RE = re.compile(
    r"(?:^|\s)(?:EUR|€|\$|£)?\s*(\d{1,6}[.,]\d{2})\s*$",
    re.IGNORECASE,
)
_EXPLICIT_QTY_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(.+?)\s*$"
)
_ALPHA_RE = re.compile(r"[A-Za-zÀ-ÿ]{2,}")
_NON_ITEM_RE = re.compile(
    r"^(?:"
    r"sub\s*total|grand\s+total|total\s+(?:due|eur|gbp|usd)|amount\s+due|"
    r"balance|cash|change|card|visa|mastercard|debit|credit|payment|tender|"
    r"vat|tax|rounding|savings?|discount|loyalty|"
    r"receipt|invoice|transaction|cashier|operator|"
    r"date|time|tel(?:ephone)?|phone|www\.|thank\s+you"
    r")\b",
    re.IGNORECASE,
)
_PLAIN_TOTAL_RE = re.compile(
    r"^total\s*[-:€£$]?\s*\d{1,6}[.,]\d{2}\s*$",
    re.IGNORECASE,
)


def parse_ocr_text(raw_text: str, *, limit: int = 60) -> list[dict]:
    """Extract review candidates while retaining price and original text."""
    candidates: list[dict] = []
    for raw_line in raw_text.splitlines():
        raw = re.sub(r"\s+", " ", raw_line).strip()
        if not raw or len(raw) > 160 or not _ALPHA_RE.search(raw):
            continue
        if _NON_ITEM_RE.search(raw) or _PLAIN_TOTAL_RE.search(raw):
            continue

        line_total = None
        price_match = _PRICE_RE.search(raw)
        without_price = raw
        if price_match:
            line_total = float(price_match.group(1).replace(",", "."))
            without_price = raw[:price_match.start()].strip(" -:|")

        quantity = 1.0
        name = without_price
        confidence = "medium" if line_total is not None else "low"
        quantity_match = _EXPLICIT_QTY_RE.match(without_price)
        if quantity_match:
            quantity = float(quantity_match.group(1).replace(",", "."))
            name = quantity_match.group(2).strip(" -:|")
            confidence = "high" if line_total is not None else "medium"

        name = re.sub(r"\s+", " ", name).strip()
        if len(name) < 2 or len(name) > 100 or not _ALPHA_RE.search(name):
            continue
        candidates.append({
            "raw_line": raw,
            "display_name": name,
            "quantity": max(quantity, 0.000001),
            "unit": "piece",
            "line_total": line_total,
            "ocr_confidence": confidence,
        })
        if len(candidates) >= limit:
            break
    return candidates


def _nutrition_match(name: str) -> dict[str, Any]:
    suggestions = nutrition_resolve.search(name, limit=1)
    if suggestions:
        return suggestions[0]
    return {
        "ingredient_key": canonical_key(name),
        "display_name": name,
        "nutrition_source": "unknown",
        "nutrition_confidence": "unknown",
        "nutrition_basis": "no_dataset_match",
    }


def _active_pantry_match(
    connection: sqlite3.Connection,
    ingredient_key: str,
    display_name: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM pantry_items WHERE ingredient_key = ? "
        "AND exhausted_at IS NULL "
        "ORDER BY (expires_on IS NULL), expires_on, id LIMIT 1",
        (ingredient_key,),
    ).fetchone()
    if row:
        return row
    return connection.execute(
        "SELECT * FROM pantry_items WHERE LOWER(display_name) = LOWER(?) "
        "AND exhausted_at IS NULL "
        "ORDER BY (expires_on IS NULL), expires_on, id LIMIT 1",
        (display_name,),
    ).fetchone()


def create(
    *,
    raw_text: str,
    image_jpeg: bytes | None,
    image_sha256: str | None,
    merchant: str | None,
    purchased_on: str | None,
    currency: str,
) -> dict[str, Any]:
    lines = parse_ocr_text(raw_text)
    now = _now()
    with db.tx() as connection:
        cur = connection.execute(
            "INSERT INTO receipt_imports "
            "(status, merchant, purchased_on, currency, raw_text, image_jpeg, "
            " image_sha256, created_at) "
            "VALUES ('review', ?, ?, ?, ?, ?, ?, ?)",
            (
                merchant,
                purchased_on,
                currency,
                raw_text[:20_000],
                image_jpeg,
                image_sha256,
                now,
            ),
        )
        receipt_id = int(cur.lastrowid)
        first_by_key: dict[str, int] = {}

        for position, candidate in enumerate(lines):
            nutrition = _nutrition_match(candidate["display_name"])
            key = canonical_key(
                candidate["display_name"],
                nutrition.get("ingredient_key"),
            )
            pantry_match = _active_pantry_match(
                connection,
                key,
                candidate["display_name"],
            )
            duplicate_of_id = first_by_key.get(key)
            action = (
                "skip"
                if duplicate_of_id is not None
                else "merge" if pantry_match else "add"
            )
            item_cur = connection.execute(
                "INSERT INTO receipt_items "
                "(receipt_id, position, raw_line, display_name, quantity, unit, "
                " line_total, ingredient_key, nutrition_source, "
                " nutrition_confidence, nutrition_basis, ocr_confidence, "
                " matched_pantry_item_id, duplicate_of_id, action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    position,
                    candidate["raw_line"],
                    candidate["display_name"],
                    candidate["quantity"],
                    candidate["unit"],
                    candidate["line_total"],
                    key,
                    nutrition.get("nutrition_source") or "unknown",
                    nutrition.get("nutrition_confidence") or "unknown",
                    nutrition.get("nutrition_basis"),
                    candidate["ocr_confidence"],
                    pantry_match["id"] if pantry_match else None,
                    duplicate_of_id,
                    action,
                    now,
                ),
            )
            item_id = int(item_cur.lastrowid)
            first_by_key.setdefault(key, item_id)
            if (
                candidate["ocr_confidence"] == "low"
                or nutrition.get("nutrition_confidence") == "unknown"
            ):
                from recognition import record_receipt_line

                record_receipt_line(
                    connection,
                    receipt_item_id=item_id,
                    raw_text=candidate["raw_line"],
                    suggested_name=candidate["display_name"],
                    suggested_key=key,
                    quantity=candidate["quantity"],
                    unit=candidate["unit"],
                    nutrition_source=(
                        nutrition.get("nutrition_source") or "unknown"
                    ),
                    nutrition_confidence=(
                        nutrition.get("nutrition_confidence") or "unknown"
                    ),
                    nutrition_basis=nutrition.get("nutrition_basis"),
                )
    return get(receipt_id)


def _previous_price(
    connection: sqlite3.Connection,
    ingredient_key: str,
    *,
    exclude_receipt_id: int,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT line_total, unit_price, quantity, unit, currency, merchant, "
        "purchased_on "
        "FROM price_history WHERE ingredient_key = ? AND "
        "(receipt_id IS NULL OR receipt_id != ?) "
        "ORDER BY COALESCE(purchased_on, created_at) DESC, id DESC LIMIT 1",
        (ingredient_key, exclude_receipt_id),
    ).fetchone()
    return dict(row) if row else None


def get(receipt_id: int) -> dict[str, Any] | None:
    connection = db._conn()
    receipt = connection.execute(
        "SELECT id, status, merchant, purchased_on, currency, raw_text, "
        "image_sha256, created_at, committed_at "
        "FROM receipt_imports WHERE id = ?",
        (receipt_id,),
    ).fetchone()
    if not receipt:
        return None
    result = dict(receipt)
    result["has_image"] = bool(result.pop("image_sha256"))
    result["image_url"] = (
        f"/api/receipts/{receipt_id}/image"
        if result["has_image"]
        else None
    )
    rows = connection.execute(
        "SELECT i.*, p.display_name AS matched_pantry_name, "
        "p.quantity AS matched_pantry_quantity, p.unit AS matched_pantry_unit "
        "FROM receipt_items i "
        "LEFT JOIN pantry_items p ON p.id = i.matched_pantry_item_id "
        "WHERE i.receipt_id = ? ORDER BY i.position",
        (receipt_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["duplicate"] = item["duplicate_of_id"] is not None
        item["previous_price"] = _previous_price(
            connection,
            item["ingredient_key"],
            exclude_receipt_id=receipt_id,
        )
        items.append(item)
    result["items"] = items
    result["item_count"] = len(items)
    result["priced_count"] = sum(
        item["line_total"] is not None for item in items
    )
    return result


def list_receipts(*, limit: int = 20) -> list[dict]:
    rows = db._conn().execute(
        "SELECT r.id, r.status, r.merchant, r.purchased_on, r.currency, "
        "r.created_at, r.committed_at, COUNT(i.id) AS item_count, "
        "COALESCE(SUM(i.line_total), 0) AS total "
        "FROM receipt_imports r "
        "LEFT JOIN receipt_items i ON i.receipt_id = r.id "
        "GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def image(receipt_id: int) -> bytes | None:
    row = db._conn().execute(
        "SELECT image_jpeg FROM receipt_imports WHERE id = ?",
        (receipt_id,),
    ).fetchone()
    return bytes(row["image_jpeg"]) if row and row["image_jpeg"] else None


def _merge_into_pantry(
    connection: sqlite3.Connection,
    *,
    pantry_item_id: int,
    quantity: float,
    unit: str,
    ingredient_key: str,
    nutrition_profile: dict[str, Any] | None = None,
) -> int:
    row = connection.execute(
        "SELECT * FROM pantry_items WHERE id = ? AND exhausted_at IS NULL",
        (pantry_item_id,),
    ).fetchone()
    if not row or row["ingredient_key"] != ingredient_key:
        raise ReceiptConflictError(
            "the matched pantry item changed; review this receipt again"
        )
    incoming, incoming_unit, incoming_dimension = to_canonical(quantity, unit)
    if incoming_dimension != row["dimension"]:
        raise ReceiptConflictError(
            "the receipt quantity is incompatible with the pantry unit"
        )
    updated = float(row["canonical_quantity"] or 0) + incoming
    display_quantity = from_canonical(updated, row["unit"])
    profile = nutrition_profile or {}
    profile_source = (
        profile.get("nutrition_source")
        or profile.get("source")
        or "unknown"
    )
    profile_confidence = (
        profile.get("nutrition_confidence") or "unknown"
    )
    connection.execute(
        "UPDATE pantry_items SET quantity = ?, canonical_quantity = ?, "
        "canonical_unit = ?, exhausted_at = NULL, "
        "kcal_100g = COALESCE(kcal_100g, ?), "
        "protein_100g = COALESCE(protein_100g, ?), "
        "carbs_100g = COALESCE(carbs_100g, ?), "
        "fat_100g = COALESCE(fat_100g, ?), "
        "fiber_100g = COALESCE(fiber_100g, ?), "
        "nutrition_source = CASE WHEN nutrition_source = 'unknown' "
        "  THEN ? ELSE nutrition_source END, "
        "nutrition_confidence = CASE WHEN nutrition_confidence = 'unknown' "
        "  THEN ? ELSE nutrition_confidence END, "
        "nutrition_basis = COALESCE(nutrition_basis, ?), "
        "version = version + 1 "
        "WHERE id = ?",
        (
            display_quantity,
            updated,
            incoming_unit,
            profile.get("kcal_100g"),
            profile.get("protein_100g"),
            profile.get("carbs_100g"),
            profile.get("fat_100g"),
            profile.get("fiber_100g"),
            profile_source,
            profile_confidence,
            profile.get("nutrition_basis"),
            pantry_item_id,
        ),
    )
    return pantry_item_id


def commit(
    receipt_id: int,
    *,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    now = _now()
    summary = {"added": 0, "merged": 0, "skipped": 0}
    with db.tx() as connection:
        receipt = connection.execute(
            "SELECT * FROM receipt_imports WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        if not receipt:
            return {"not_found": True}
        if receipt["status"] == "committed":
            counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM receipt_items "
                "WHERE receipt_id = ? GROUP BY status",
                (receipt_id,),
            ).fetchall()
            completed = {
                row["status"]: int(row["count"])
                for row in counts
            }
            return {
                "idempotent": True,
                "receipt": get(receipt_id),
                "added": completed.get("added", 0),
                "merged": completed.get("merged", 0),
                "skipped": completed.get("skipped", 0),
            }
        if receipt["status"] != "review":
            raise ReceiptConflictError("this receipt is no longer editable")

        stored_rows = connection.execute(
            "SELECT * FROM receipt_items WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchall()
        stored = {int(row["id"]): row for row in stored_rows}
        try:
            submitted_ids = [int(payload["id"]) for payload in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise ReceiptConflictError("receipt item identifiers are invalid") from exc
        if (
            len(items) != len(stored)
            or len(set(submitted_ids)) != len(submitted_ids)
            or set(submitted_ids) != set(stored)
        ):
            raise ReceiptConflictError(
                "the receipt changed; reload it before committing"
            )

        for payload in items:
            item_id = int(payload["id"])
            row = stored.get(item_id)
            if not row:
                raise ReceiptConflictError(
                    "the receipt changed; reload it before committing"
                )
            action = payload.get("action")
            if action not in {"add", "merge", "skip"}:
                raise ReceiptConflictError("receipt item action is invalid")
            if action == "skip":
                connection.execute(
                    "UPDATE receipt_items SET action = 'skip', "
                    "status = 'skipped' WHERE id = ?",
                    (item_id,),
                )
                summary["skipped"] += 1
                continue

            display_name = payload.get("display_name")
            if not isinstance(display_name, str) or not display_name.strip():
                raise ReceiptConflictError("receipt item name is required")
            display_name = display_name.strip()
            if len(display_name) > 200:
                raise ReceiptConflictError("receipt item name is too long")
            try:
                quantity = float(payload["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ReceiptConflictError(
                    "receipt item quantity is invalid"
                ) from exc
            if not math.isfinite(quantity) or not 0 < quantity <= 1_000_000:
                raise ReceiptConflictError("receipt item quantity is invalid")
            unit_value = payload.get("unit")
            if not isinstance(unit_value, str) or len(unit_value.strip()) > 30:
                raise ReceiptConflictError("receipt item unit is invalid")
            unit = normalize_unit(unit_value)
            line_total = payload.get("line_total")
            if line_total not in (None, ""):
                try:
                    line_total = float(line_total)
                except (TypeError, ValueError) as exc:
                    raise ReceiptConflictError(
                        "receipt item price is invalid"
                    ) from exc
                if (
                    not math.isfinite(line_total)
                    or not 0 <= line_total <= 1_000_000
                ):
                    raise ReceiptConflictError("receipt item price is invalid")
            else:
                line_total = None
            selected_key = payload.get("ingredient_key")
            if selected_key is not None and (
                not isinstance(selected_key, str) or len(selected_key) > 160
            ):
                raise ReceiptConflictError("receipt item match is invalid")
            nutrition = (
                nutrition_resolve.by_key(selected_key)
                if selected_key
                else None
            ) or _nutrition_match(display_name)
            key = canonical_key(
                display_name,
                selected_key or nutrition.get("ingredient_key"),
            )
            source = nutrition.get("nutrition_source") or "unknown"
            confidence = nutrition.get("nutrition_confidence") or "unknown"
            basis = nutrition.get("nutrition_basis")

            pantry_item_id = None
            if action == "merge":
                target = row["matched_pantry_item_id"]
                if not target:
                    raise ReceiptConflictError(
                        "no active pantry item is available to merge"
                    )
                pantry_item_id = _merge_into_pantry(
                    connection,
                    pantry_item_id=int(target),
                    quantity=quantity,
                    unit=unit,
                    ingredient_key=key,
                    nutrition_profile=nutrition,
                )
                status = "merged"
                summary["merged"] += 1
            elif action == "add":
                pantry_item_id = pantry_dao.add(
                    ingredient_key=key,
                    display_name=display_name,
                    quantity=quantity,
                    unit=unit,
                    source="receipt_ocr",
                    nutrition_profile=nutrition,
                )
                status = "added"
                summary["added"] += 1

            connection.execute(
                "UPDATE receipt_items SET display_name = ?, quantity = ?, "
                "unit = ?, line_total = ?, ingredient_key = ?, "
                "nutrition_source = ?, nutrition_confidence = ?, "
                "nutrition_basis = ?, action = ?, status = ?, "
                "pantry_item_id = ? WHERE id = ?",
                (
                    display_name,
                    quantity,
                    unit,
                    line_total,
                    key,
                    source,
                    confidence,
                    basis,
                    action,
                    status,
                    pantry_item_id,
                    item_id,
                ),
            )
            if line_total is not None:
                connection.execute(
                    "INSERT INTO price_history "
                    "(ingredient_key, display_name, merchant, purchased_on, "
                    " currency, quantity, unit, line_total, unit_price, "
                    " receipt_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        display_name,
                        receipt["merchant"],
                        receipt["purchased_on"],
                        receipt["currency"],
                        quantity,
                        unit,
                        line_total,
                        line_total / quantity,
                        receipt_id,
                        now,
                    ),
                )

        connection.execute(
            "UPDATE receipt_imports SET status = 'committed', committed_at = ? "
            "WHERE id = ?",
            (now, receipt_id),
        )
        from recognition import resolve_receipt_queue

        resolve_receipt_queue(connection, receipt_id)
    return {
        "idempotent": False,
        "receipt": get(receipt_id),
        **summary,
    }


def discard(receipt_id: int) -> bool:
    now = _now()
    with db.tx() as connection:
        cur = connection.execute(
            "UPDATE receipt_imports SET status = 'discarded' "
            "WHERE id = ? AND status = 'review'",
            (receipt_id,),
        )
        if not cur.rowcount:
            return False
        connection.execute(
            "UPDATE recognition_inbox SET status = 'dismissed', "
            "updated_at = ?, resolved_at = ? "
            "WHERE status = 'open' AND receipt_item_id IN "
            "(SELECT id FROM receipt_items WHERE receipt_id = ?)",
            (now, now, receipt_id),
        )
    return True
