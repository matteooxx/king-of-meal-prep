"""Atomic meal-plan state transitions and cook/undo side effects."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import db
import prepared
from pantry import dao as pantry_dao

SLOTS = {"breakfast", "lunch", "dinner", "snack"}
STATUSES = {"planned", "cooked", "skipped", "substituted"}
UNSET = object()


class ConflictError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def week_start(date_string: str) -> str:
    day = date.fromisoformat(date_string)
    return (day - timedelta(days=day.weekday())).isoformat()


def _touch_week(c, date_string: str) -> int:
    start = week_start(date_string)
    now = _now()
    c.execute(
        "INSERT INTO plan_weeks (start_date, version, updated_at) VALUES (?, 1, ?) "
        "ON CONFLICT(start_date) DO UPDATE SET "
        "version = plan_weeks.version + 1, updated_at = excluded.updated_at",
        (start, now),
    )
    return int(c.execute(
        "SELECT version FROM plan_weeks WHERE start_date = ?", (start,)
    ).fetchone()["version"])


def _refresh_recipe_history(c, recipe_id: int) -> None:
    recipe = c.execute(
        "SELECT legacy_cook_count, legacy_last_cooked_at FROM recipes WHERE id = ?",
        (recipe_id,),
    ).fetchone()
    if not recipe:
        return
    active = c.execute(
        "SELECT COUNT(*) AS n, MAX(cooked_at) AS latest FROM cook_events "
        "WHERE recipe_id = ? AND undone_at IS NULL",
        (recipe_id,),
    ).fetchone()
    count = int(recipe["legacy_cook_count"] or 0) + int(active["n"] or 0)
    latest = max(
        [
            value
            for value in (recipe["legacy_last_cooked_at"], active["latest"])
            if value
        ],
        default=None,
    )
    c.execute(
        "UPDATE recipes SET cook_count = ?, last_cooked_at = ? WHERE id = ?",
        (count, latest, recipe_id),
    )


def _undo_active_event(c, date_string: str, slot: str, recipe_id: int) -> dict:
    event = c.execute(
        "SELECT * FROM cook_events "
        "WHERE date = ? AND slot = ? AND undone_at IS NULL",
        (date_string, slot),
    ).fetchone()
    if not event:
        # Rows cooked before the ledger migration cannot be reconstructed.
        recipe = c.execute(
            "SELECT legacy_cook_count, legacy_last_cooked_at FROM recipes WHERE id = ?",
            (recipe_id,),
        ).fetchone()
        if recipe and int(recipe["legacy_cook_count"] or 0) > 0:
            c.execute(
                "UPDATE recipes SET legacy_cook_count = legacy_cook_count - 1, "
                "legacy_last_cooked_at = CASE "
                "WHEN legacy_cook_count - 1 <= 0 THEN NULL "
                "ELSE legacy_last_cooked_at END WHERE id = ?",
                (recipe_id,),
            )
            _refresh_recipe_history(c, recipe_id)
        return {
            "restored": [],
            "legacy_without_ledger": True,
            "prepared": {"restored": [], "discarded_created_batch": False},
        }
    try:
        prepared_result = prepared.undo_event(c, event)
    except prepared.PreparedConflict as exc:
        raise ConflictError(str(exc)) from exc
    restored = (
        pantry_dao.restore_for_event(c, event["id"])
        if event["cook_mode"] != "prepared"
        else []
    )
    c.execute(
        "UPDATE cook_events SET undone_at = ? WHERE id = ? AND undone_at IS NULL",
        (_now(), event["id"]),
    )
    _refresh_recipe_history(c, event["recipe_id"])
    return {
        "restored": restored,
        "legacy_without_ledger": False,
        "prepared": prepared_result,
    }


def patch_slot(
    date_string: str,
    slot: str,
    *,
    recipe_id=UNSET,
    servings=UNSET,
    status=UNSET,
    is_training_day=UNSET,
    locked=UNSET,
    expected_version: int | None = None,
    event_key: str | None = None,
    cook_mode=UNSET,
    prepared_servings=UNSET,
    expires_on=UNSET,
    frozen=UNSET,
) -> dict:
    """Apply one slot patch and every derived side effect in one transaction."""
    if slot not in SLOTS:
        raise ValueError("invalid slot")
    date.fromisoformat(date_string)
    pantry_result = {"consumed": [], "missing": [], "restored": []}
    prepared_result = {
        "mode": None,
        "created": None,
        "consumed": [],
        "restored": [],
        "available": 0.0,
    }
    idempotent = False
    has_other_mutation = any(
        value is not UNSET
        for value in (recipe_id, servings, is_training_day, locked)
    )

    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM meal_plan WHERE date = ? AND slot = ?",
            (date_string, slot),
        ).fetchone()
        if expected_version is not None and row and row["version"] != expected_version:
            raise ConflictError("slot changed since it was loaded")

        if recipe_id is not UNSET:
            recipe = c.execute(
                "SELECT id FROM recipes WHERE id = ? AND archived_at IS NULL",
                (recipe_id,),
            ).fetchone()
            if not recipe:
                raise NotFoundError("recipe not found")
            if (
                row
                and row["status"] == "cooked"
                and row["recipe_id"] != recipe_id
            ):
                raise ConflictError("undo the cooked meal before replacing its recipe")
            if row:
                changed_recipe = row["recipe_id"] != recipe_id
                c.execute(
                    "UPDATE meal_plan SET recipe_id = ?, origin = 'manual', "
                    "status = CASE WHEN ? THEN 'planned' ELSE status END, "
                    "cooked_at = CASE WHEN ? THEN NULL ELSE cooked_at END "
                    "WHERE date = ? AND slot = ?",
                    (
                        recipe_id,
                        1 if changed_recipe else 0,
                        1 if changed_recipe else 0,
                        date_string,
                        slot,
                    ),
                )
            else:
                c.execute(
                    "INSERT INTO meal_plan "
                    "(date, slot, recipe_id, servings, status, origin) "
                    "VALUES (?, ?, ?, 1.0, 'planned', 'manual')",
                    (date_string, slot, recipe_id),
                )
            row = c.execute(
                "SELECT * FROM meal_plan WHERE date = ? AND slot = ?",
                (date_string, slot),
            ).fetchone()

        if row is None and any(
            value is not UNSET for value in (servings, status, locked)
        ):
            raise NotFoundError("no plan slot for that date and slot")

        if servings is not UNSET:
            if (
                row
                and row["status"] == "cooked"
                and float(row["servings"] or 1) != float(servings)
            ):
                raise ConflictError(
                    "undo the cooked meal before changing its servings"
                )
            c.execute(
                "UPDATE meal_plan SET servings = ? WHERE date = ? AND slot = ?",
                (servings, date_string, slot),
            )
        if locked is not UNSET:
            c.execute(
                "UPDATE meal_plan SET locked = ? WHERE date = ? AND slot = ?",
                (1 if locked else 0, date_string, slot),
            )

        if status is not UNSET:
            row = c.execute(
                "SELECT * FROM meal_plan WHERE date = ? AND slot = ?",
                (date_string, slot),
            ).fetchone()
            if not row:
                raise NotFoundError("no plan slot for that date and slot")
            previous = row["status"]
            if status == previous:
                if status == "cooked":
                    if not event_key:
                        raise ConflictError(
                            "an idempotency key is required to mark a meal cooked"
                        )
                    active = c.execute(
                        "SELECT event_key FROM cook_events "
                        "WHERE date = ? AND slot = ? AND undone_at IS NULL",
                        (date_string, slot),
                    ).fetchone()
                    if active and active["event_key"] != event_key:
                        raise ConflictError(
                            "meal is already cooked with a different idempotency key"
                        )
                idempotent = True
            else:
                if previous == "cooked":
                    undo_result = _undo_active_event(
                        c, date_string, slot, int(row["recipe_id"])
                    )
                    prepared_result.update(undo_result.pop("prepared"))
                    pantry_result.update(undo_result)
                if status == "cooked":
                    if not row["recipe_id"]:
                        raise ConflictError("cannot cook an empty slot")
                    if not event_key:
                        raise ConflictError(
                            "an idempotency key is required to mark a meal cooked"
                        )
                    key = event_key
                    existing = c.execute(
                        "SELECT * FROM cook_events WHERE event_key = ?", (key,)
                    ).fetchone()
                    if existing:
                        if (
                            existing["date"] == date_string
                            and existing["slot"] == slot
                            and existing["undone_at"] is None
                        ):
                            idempotent = True
                        else:
                            raise ConflictError("idempotency key was already used")
                    else:
                        recipe = c.execute(
                            "SELECT servings FROM recipes WHERE id = ?",
                            (row["recipe_id"],),
                        ).fetchone()
                        if not recipe:
                            raise NotFoundError("recipe not found")
                        eaten_servings = float(row["servings"] or 1)
                        available_before = prepared.available(
                            int(row["recipe_id"]), conn=c
                        )
                        requested_mode = (
                            "auto" if cook_mode is UNSET else str(cook_mode)
                        )
                        actual_mode = requested_mode
                        if requested_mode == "auto":
                            actual_mode = (
                                "prepared"
                                if available_before + prepared.EPSILON
                                >= eaten_servings
                                else "fresh"
                            )
                        if actual_mode not in {"fresh", "prepared"}:
                            raise ConflictError("invalid cooking mode")
                        recipe_yield = max(float(recipe["servings"] or 1), 0.1)
                        batch_servings = 0.0
                        if actual_mode == "fresh":
                            batch_servings = (
                                max(recipe_yield, eaten_servings)
                                if prepared_servings is UNSET
                                else float(prepared_servings)
                            )
                            if batch_servings + prepared.EPSILON < eaten_servings:
                                raise ConflictError(
                                    "prepared portions cannot be less than portions eaten"
                                )
                        try:
                            cur = c.execute(
                                "INSERT INTO cook_events "
                                "(event_key, date, slot, recipe_id, servings, "
                                " cook_mode, prepared_servings, cooked_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    key,
                                    date_string,
                                    slot,
                                    row["recipe_id"],
                                    eaten_servings,
                                    actual_mode,
                                    batch_servings,
                                    _now(),
                                ),
                            )
                        except sqlite3.IntegrityError as exc:
                            raise ConflictError("meal is already being cooked") from exc
                        prepared_result["mode"] = actual_mode
                        if actual_mode == "prepared":
                            try:
                                prepared_result["consumed"] = prepared.consume(
                                    c,
                                    recipe_id=int(row["recipe_id"]),
                                    portions=eaten_servings,
                                    cook_event_id=cur.lastrowid,
                                )
                            except prepared.PreparedConflict as exc:
                                raise ConflictError(str(exc)) from exc
                        else:
                            pantry_result.update(
                                pantry_dao.consume_for_recipe(
                                    int(row["recipe_id"]),
                                    batch_servings,
                                    conn=c,
                                    cook_event_id=cur.lastrowid,
                                )
                            )
                            surplus = max(
                                0.0, batch_servings - eaten_servings
                            )
                            prepared_result["created"] = prepared.create_batch(
                                c,
                                recipe_id=int(row["recipe_id"]),
                                source_cook_event_id=cur.lastrowid,
                                portions_total=surplus,
                                portions_remaining=surplus,
                                frozen=False if frozen is UNSET else bool(frozen),
                                expires_on=(
                                    None if expires_on is UNSET else expires_on
                                ),
                            )
                        _refresh_recipe_history(c, int(row["recipe_id"]))
                    cooked_at = _now()
                else:
                    cooked_at = None
                c.execute(
                    "UPDATE meal_plan SET status = ?, cooked_at = ? "
                    "WHERE date = ? AND slot = ?",
                    (status, cooked_at, date_string, slot),
                )

        if is_training_day is not UNSET:
            row = c.execute(
                "SELECT 1 FROM meal_plan WHERE date = ? AND slot = ?",
                (date_string, slot),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE meal_plan SET is_training_day = ? "
                    "WHERE date = ? AND slot = ?",
                    (1 if is_training_day else 0, date_string, slot),
                )
            else:
                c.execute(
                    "INSERT INTO meal_plan "
                    "(date, slot, recipe_id, servings, status, is_training_day, origin) "
                    "VALUES (?, ?, NULL, 1.0, 'planned', ?, 'manual')",
                    (date_string, slot, 1 if is_training_day else 0),
                )

        if idempotent and not has_other_mutation:
            version_row = c.execute(
                "SELECT version FROM plan_weeks WHERE start_date = ?",
                (week_start(date_string),),
            ).fetchone()
            plan_version = int(version_row["version"]) if version_row else 0
        else:
            c.execute(
                "UPDATE meal_plan SET version = version + 1 "
                "WHERE date = ? AND slot = ?",
                (date_string, slot),
            )
            plan_version = _touch_week(c, date_string)
        result_row = c.execute(
            "SELECT * FROM meal_plan WHERE date = ? AND slot = ?",
            (date_string, slot),
        ).fetchone()
        if result_row and result_row["recipe_id"]:
            prepared_result["available"] = prepared.available(
                int(result_row["recipe_id"]), conn=c
            )

    return {
        "slot": dict(result_row) if result_row else None,
        "plan_version": plan_version,
        "idempotent": idempotent,
        "pantry": pantry_result,
        "prepared": prepared_result,
    }
