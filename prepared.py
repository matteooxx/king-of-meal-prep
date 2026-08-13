"""Prepared-portion inventory and its reversible consumption ledger."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db
import settings as app_settings

EPSILON = 0.000001


class PreparedConflict(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    timezone_name = app_settings.kv_get("timezone") or "UTC"
    try:
        zone = ZoneInfo(str(timezone_name))
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return datetime.now(zone).date().isoformat()


def _expiry(*, frozen: bool, prepared_at: str | None = None) -> str:
    shelf_key = "frozen_shelf_life_days" if frozen else "prepared_shelf_life_days"
    fallback = 90 if frozen else 4
    days = int(app_settings.kv_get(shelf_key) or fallback)
    base = (
        datetime.fromisoformat(prepared_at).date()
        if prepared_at
        else datetime.now(timezone.utc).date()
    )
    return (base + timedelta(days=days)).isoformat()


def available(recipe_id: int, *, conn=None) -> float:
    c = conn or db._conn()
    row = c.execute(
        "SELECT COALESCE(SUM(portions_remaining), 0) AS total "
        "FROM prepared_batches WHERE recipe_id = ? AND discarded_at IS NULL "
        "AND portions_remaining > ? "
        "AND (expires_on IS NULL OR expires_on >= ?)",
        (recipe_id, EPSILON, _today()),
    ).fetchone()
    return float(row["total"] or 0)


def list_active(recipe_id: int | None = None) -> list[dict]:
    where = (
        "WHERE pb.discarded_at IS NULL AND pb.portions_remaining > ?"
    )
    params: list[object] = [EPSILON]
    if recipe_id is not None:
        where += " AND pb.recipe_id = ?"
        params.append(recipe_id)
    rows = db._conn().execute(
        "SELECT pb.*, r.name AS recipe_name, r.kcal, r.protein_g "
        "FROM prepared_batches pb JOIN recipes r ON r.id = pb.recipe_id "
        f"{where} "
        "ORDER BY pb.frozen, (pb.expires_on IS NULL), pb.expires_on, pb.prepared_at",
        params,
    ).fetchall()
    today = _today()
    items = []
    for row in rows:
        item = dict(row)
        item["expired"] = bool(
            item["expires_on"] and item["expires_on"] < today
        )
        items.append(item)
    return items


def create_batch(
    c,
    *,
    recipe_id: int,
    source_cook_event_id: int,
    portions_total: float,
    portions_remaining: float,
    frozen: bool = False,
    expires_on: str | None = None,
) -> dict | None:
    if portions_remaining <= EPSILON:
        return None
    prepared_at = _now()
    if not expires_on:
        expires_on = _expiry(frozen=frozen, prepared_at=prepared_at)
    cur = c.execute(
        "INSERT INTO prepared_batches "
        "(recipe_id, source_cook_event_id, portions_total, portions_remaining, "
        " prepared_at, expires_on, frozen) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            recipe_id,
            source_cook_event_id,
            portions_total,
            portions_remaining,
            prepared_at,
            expires_on,
            1 if frozen else 0,
        ),
    )
    row = c.execute(
        "SELECT * FROM prepared_batches WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def consume(c, *, recipe_id: int, portions: float, cook_event_id: int) -> list[dict]:
    remaining = float(portions)
    batches = c.execute(
        "SELECT * FROM prepared_batches "
        "WHERE recipe_id = ? AND discarded_at IS NULL AND portions_remaining > ? "
        "AND (expires_on IS NULL OR expires_on >= ?) "
        "ORDER BY frozen, (expires_on IS NULL), expires_on, prepared_at",
        (recipe_id, EPSILON, _today()),
    ).fetchall()
    if sum(float(batch["portions_remaining"]) for batch in batches) + EPSILON < remaining:
        raise PreparedConflict("not enough prepared portions")

    consumed: list[dict] = []
    for batch in batches:
        if remaining <= EPSILON:
            break
        take = min(float(batch["portions_remaining"]), remaining)
        updated = max(0.0, float(batch["portions_remaining"]) - take)
        c.execute(
            "UPDATE prepared_batches SET portions_remaining = ?, version = version + 1 "
            "WHERE id = ?",
            (updated, batch["id"]),
        )
        c.execute(
            "INSERT INTO prepared_movements "
            "(cook_event_id, batch_id, delta_portions, kind, created_at) "
            "VALUES (?, ?, ?, 'consume', ?)",
            (cook_event_id, batch["id"], -take, _now()),
        )
        consumed.append({
            "batch_id": batch["id"],
            "portions": take,
            "expires_on": batch["expires_on"],
            "frozen": bool(batch["frozen"]),
        })
        remaining -= take
    return consumed


def undo_event(c, event) -> dict:
    """Reverse prepared inventory effects before pantry restoration."""
    restored: list[dict] = []
    source = c.execute(
        "SELECT * FROM prepared_batches WHERE source_cook_event_id = ?",
        (event["id"],),
    ).fetchone()
    if source:
        expected = max(
            0.0,
            float(event["prepared_servings"] or 0) - float(event["servings"] or 0),
        )
        if (
            source["discarded_at"] is not None
            or abs(float(source["portions_remaining"]) - expected) > EPSILON
        ):
            raise PreparedConflict(
                "prepared portions from this cook were already used or edited"
            )
        c.execute(
            "UPDATE prepared_batches SET discarded_at = ?, portions_remaining = 0, "
            "version = version + 1 WHERE id = ?",
            (_now(), source["id"]),
        )

    movements = c.execute(
        "SELECT pm.*, pb.discarded_at, pb.portions_total, "
        "pb.portions_remaining FROM prepared_movements pm "
        "JOIN prepared_batches pb ON pb.id = pm.batch_id "
        "WHERE pm.cook_event_id = ? AND pm.kind = 'consume' ORDER BY pm.id",
        (event["id"],),
    ).fetchall()
    already_restored = c.execute(
        "SELECT 1 FROM prepared_movements "
        "WHERE cook_event_id = ? AND kind = 'restore' LIMIT 1",
        (event["id"],),
    ).fetchone()
    if movements and not already_restored:
        for movement in movements:
            if movement["discarded_at"] is not None:
                raise PreparedConflict("the prepared batch was discarded")
            amount = -float(movement["delta_portions"])
            if (
                float(movement["portions_remaining"]) + amount
                > float(movement["portions_total"]) + EPSILON
            ):
                raise PreparedConflict(
                    "the prepared batch was edited after this meal"
                )
            c.execute(
                "UPDATE prepared_batches SET portions_remaining = "
                "portions_remaining + ?, version = version + 1 WHERE id = ?",
                (amount, movement["batch_id"]),
            )
            c.execute(
                "INSERT INTO prepared_movements "
                "(cook_event_id, batch_id, delta_portions, kind, "
                " reverses_movement_id, created_at) "
                "VALUES (?, ?, ?, 'restore', ?, ?)",
                (
                    event["id"],
                    movement["batch_id"],
                    amount,
                    movement["id"],
                    _now(),
                ),
            )
            restored.append({
                "batch_id": movement["batch_id"],
                "portions": amount,
            })
    return {
        "restored": restored,
        "discarded_created_batch": bool(source),
    }


def update_batch(
    batch_id: int,
    *,
    portions_remaining: float | None = None,
    expires_on: str | None = None,
    frozen: bool | None = None,
    discard: bool = False,
) -> dict | None:
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM prepared_batches WHERE id = ? AND discarded_at IS NULL",
            (batch_id,),
        ).fetchone()
        if not row:
            return None
        remaining = (
            float(row["portions_remaining"])
            if portions_remaining is None
            else float(portions_remaining)
        )
        if remaining < 0 or remaining > float(row["portions_total"]) + EPSILON:
            raise ValueError("remaining portions are outside this batch")
        next_frozen = bool(row["frozen"]) if frozen is None else bool(frozen)
        next_expiry = row["expires_on"] if expires_on is None else expires_on
        if frozen is not None and frozen != bool(row["frozen"]) and expires_on is None:
            next_expiry = _expiry(frozen=next_frozen)
        discarded_at = _now() if discard or remaining <= EPSILON else None
        if discard:
            remaining = 0
        c.execute(
            "UPDATE prepared_batches SET portions_remaining = ?, expires_on = ?, "
            "frozen = ?, discarded_at = ?, version = version + 1 WHERE id = ?",
            (
                remaining,
                next_expiry,
                1 if next_frozen else 0,
                discarded_at,
                batch_id,
            ),
        )
        updated = c.execute(
            "SELECT * FROM prepared_batches WHERE id = ?", (batch_id,)
        ).fetchone()
    return dict(updated)
