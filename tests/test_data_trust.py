from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.TemporaryDirectory()
_DATA = Path(_TMP.name)
os.environ["DB_PATH"] = str(_DATA / "data.db")
os.environ["DB_BACKUP_DIR"] = str(_DATA / "backups")
os.environ["APP_ENV_PATH"] = str(_DATA / "app.env")
os.environ["NUTRITION_DB"] = str(_DATA / "nutrition.db")

import db
import meal_service
import prepared
from barcodes import service as barcode_service
from barcodes.gtin import parse as parse_barcode
from pantry import dao as pantry_dao
from planner import solver
from recipes import dao as recipes_dao


def reset_database() -> None:
    db.close_thread_conn()
    for path in _DATA.glob("data.db*"):
        path.unlink()
    backup_dir = _DATA / "backups"
    if backup_dir.exists():
        for path in backup_dir.iterdir():
            path.unlink()
    db.init()


def add_recipe(
    name: str,
    slot: str = "dinner",
    ingredient_qty: float = 500,
    servings: int = 1,
) -> int:
    now = "2026-07-18T12:00:00+00:00"
    cur = db._conn().execute(
        "INSERT INTO recipes "
        "(name, source, servings, meal_slot, equipment_json, steps_json, "
        "kcal, protein_g, carbs_g, fat_g, fiber_g, created_at) "
        "VALUES (?, 'manual', ?, ?, '[]', '[]', 600, 40, 60, 20, 5, ?)",
        (name, servings, slot, now),
    )
    rid = cur.lastrowid
    db._conn().execute(
        "INSERT INTO recipe_ingredients "
        "(recipe_id, position, ingredient_key, display_name, quantity, unit, optional) "
        "VALUES (?, 0, 'name:rice', 'Rice', ?, 'g', 0)",
        (rid, ingredient_qty),
    )
    return rid


class DataTrustTests(unittest.TestCase):
    def setUp(self):
        reset_database()

    def test_online_barcode_result_is_cached_for_offline_reuse(self):
        product = {
            "ingredient_key": "off:03017620422003",
            "display_name": "Nutella",
            "brand": "Nutella",
            "quantity": 400,
            "unit": "g",
            "kcal_100g": 539,
            "protein_100g": 6.3,
            "carbs_100g": 57.5,
            "fat_100g": 30.9,
            "fiber_100g": None,
            "nutrition_available": True,
        }
        with mock.patch.object(
            barcode_service.nutrition_resolve,
            "resolve_by_ean",
            return_value=None,
        ), mock.patch.object(
            barcode_service,
            "_fetch_online",
            return_value=product,
        ) as fetch:
            online = barcode_service.lookup(
                "3017620422003", online=True
            )
            cached = barcode_service.lookup(
                "3017620422003", online=False
            )

        self.assertEqual(online["source"], "off_online")
        self.assertEqual(cached["source"], "off_cache")
        self.assertEqual(cached["proposal"]["quantity"], 400)
        self.assertEqual(fetch.call_count, 1)
        row = db._conn().execute(
            "SELECT status, display_name FROM barcode_cache"
        ).fetchone()
        self.assertEqual((row["status"], row["display_name"]), ("found", "Nutella"))

    def test_negative_barcode_lookup_is_cached_temporarily(self):
        with mock.patch.object(
            barcode_service.nutrition_resolve,
            "resolve_by_ean",
            return_value=None,
        ), mock.patch.object(
            barcode_service,
            "_fetch_online",
            return_value=None,
        ) as fetch:
            self.assertIsNone(
                barcode_service.lookup("3017620422003", online=True)
            )
            self.assertIsNone(
                barcode_service.lookup("3017620422003", online=True)
            )

        self.assertEqual(fetch.call_count, 1)
        row = db._conn().execute(
            "SELECT status FROM barcode_cache"
        ).fetchone()
        self.assertEqual(row["status"], "not_found")

    def test_definitive_miss_replaces_stale_positive_cache(self):
        barcode = parse_barcode("3017620422003")
        product = {
            "display_name": "Stale product",
            "brand": "",
            "quantity": 1,
            "unit": "piece",
            "kcal_100g": None,
            "protein_100g": None,
            "carbs_100g": None,
            "fat_100g": None,
            "fiber_100g": None,
        }
        barcode_service._save_found(barcode, product)
        db._conn().execute(
            "UPDATE barcode_cache SET expires_at = '2000-01-01T00:00:00+00:00'"
        )

        with mock.patch.object(
            barcode_service.nutrition_resolve,
            "resolve_by_ean",
            return_value=None,
        ), mock.patch.object(
            barcode_service,
            "_fetch_online",
            return_value=None,
        ):
            self.assertIsNone(
                barcode_service.lookup("3017620422003", online=True)
            )

        row = db._conn().execute(
            "SELECT status, display_name FROM barcode_cache"
        ).fetchone()
        self.assertEqual(row["status"], "not_found")
        self.assertIsNone(row["display_name"])

    def test_stale_positive_cache_is_used_during_provider_outage(self):
        barcode = parse_barcode("3017620422003")
        product = {
            "display_name": "Stale product",
            "brand": "",
            "quantity": 250,
            "unit": "g",
            "kcal_100g": None,
            "protein_100g": None,
            "carbs_100g": None,
            "fat_100g": None,
            "fiber_100g": None,
        }
        barcode_service._save_found(barcode, product)
        db._conn().execute(
            "UPDATE barcode_cache SET expires_at = '2000-01-01T00:00:00+00:00'"
        )

        with mock.patch.object(
            barcode_service.nutrition_resolve,
            "resolve_by_ean",
            return_value=None,
        ), mock.patch.object(
            barcode_service,
            "_fetch_online",
            side_effect=barcode_service.OnlineLookupError("offline"),
        ):
            result = barcode_service.lookup(
                "3017620422003", online=True
            )

        self.assertEqual(result["source"], "off_cache")
        self.assertEqual(result["proposal"]["quantity"], 250)

    def test_user_barcode_override_matches_equivalent_ean(self):
        barcode_service.remember_user_barcode(
            db._conn(),
            parse_barcode("036000291452"),
            display_name="Saved product",
            quantity=750,
            unit="g",
        )
        result = barcode_service.lookup(
            "0036000291452", online=False
        )

        self.assertEqual(result["source"], "local")
        self.assertEqual(result["proposal"]["display_name"], "Saved product")
        self.assertEqual(result["proposal"]["quantity"], 750)
        row = db._conn().execute(
            "SELECT ean, quantity FROM user_barcodes"
        ).fetchone()
        self.assertEqual(row["ean"], "00036000291452")
        self.assertEqual(row["quantity"], 750)

    def test_remembered_barcode_keeps_cached_nutrition(self):
        barcode = parse_barcode("3017620422003")
        barcode_service._save_found(barcode, {
            "display_name": "Label name",
            "brand": "",
            "quantity": 400,
            "unit": "g",
            "kcal_100g": 539,
            "protein_100g": 6.3,
            "carbs_100g": 57.5,
            "fat_100g": 30.9,
            "fiber_100g": None,
        })
        barcode_service.remember_user_barcode(
            db._conn(),
            barcode,
            display_name="My saved name",
            quantity=400,
            unit="g",
        )

        result = barcode_service.lookup(
            "3017620422003", online=False
        )

        self.assertEqual(result["source"], "local")
        self.assertEqual(result["proposal"]["display_name"], "My saved name")
        self.assertTrue(result["details"]["nutrition"]["available"])
        self.assertEqual(
            result["details"]["nutrition"]["kcal_100g"],
            539,
        )

    def test_open_food_facts_fetch_is_bounded_and_proxy_free(self):
        real_client = httpx.Client
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "product": {
                        "code": "3017620422003",
                        "product_name_en": "Nutella",
                        "brands": "Nutella",
                        "nutriments": {"energy-kcal_100g": 539},
                    },
                },
            )

        def client_factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return real_client(
                transport=httpx.MockTransport(handler),
                **kwargs,
            )

        with mock.patch.object(
            barcode_service.httpx,
            "Client",
            side_effect=client_factory,
        ):
            product = barcode_service._fetch_code(
                "3017620422003",
                parse_barcode("3017620422003"),
            )

        self.assertEqual(product["display_name"], "Nutella")
        self.assertEqual(product["kcal_100g"], 539)
        self.assertEqual(requests[0].url.host, "world.openfoodfacts.org")
        self.assertIn("fields", requests[0].url.params)
        self.assertTrue(
            requests[0].headers["user-agent"].startswith("KingOfMealPrep/")
        )

    def test_pantry_portions_follow_recipe_consumption_and_undo(self):
        rid = add_recipe("Rice side", ingredient_qty=100)
        pantry_id = pantry_dao.add(
            ingredient_key="name:rice",
            display_name="Rice",
            quantity=400,
            unit="g",
            portions=2,
        )

        item = pantry_dao.list_active()[0]
        self.assertEqual(item["id"], pantry_id)
        self.assertAlmostEqual(item["portion_quantity"], 200)
        self.assertAlmostEqual(item["portions_remaining"], 2)

        meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            recipe_id=rid,
            status="cooked",
            event_key="cook-portioned-pantry",
        )
        item = pantry_dao.list_active()[0]
        self.assertAlmostEqual(item["quantity"], 300)
        self.assertAlmostEqual(item["portion_quantity"], 200)
        self.assertAlmostEqual(item["portions_remaining"], 1.5)

        meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            status="planned",
        )
        item = pantry_dao.list_active()[0]
        self.assertAlmostEqual(item["quantity"], 400)
        self.assertAlmostEqual(item["portions_remaining"], 2)

    def test_consume_portion_uses_smaller_final_remainder(self):
        pantry_id = pantry_dao.add(
            ingredient_key="name:turkey-mince",
            display_name="Turkey mince",
            quantity=400,
            unit="g",
            portions=2,
        )

        first = pantry_dao.consume_portion(pantry_id)
        self.assertAlmostEqual(first["consumed_quantity"], 200)
        self.assertAlmostEqual(first["quantity"], 200)
        self.assertAlmostEqual(first["portions_remaining"], 1)

        pantry_dao.update(pantry_id, quantity=50)
        final = pantry_dao.consume_portion(pantry_id)
        self.assertAlmostEqual(final["consumed_quantity"], 50)
        self.assertAlmostEqual(final["quantity"], 0)
        self.assertAlmostEqual(final["portions_remaining"], 0)
        self.assertEqual(pantry_dao.list_active(), [])
        self.assertIsNone(pantry_dao.consume_portion(pantry_id))

    def test_pantry_portion_plan_can_be_recalculated_and_cleared(self):
        pantry_id = pantry_dao.add(
            ingredient_key="name:turkey-mince",
            display_name="Turkey mince",
            quantity=400,
            unit="g",
        )
        with self.assertRaisesRegex(
            pantry_dao.PortionError,
            "not split into portions",
        ):
            pantry_dao.consume_portion(pantry_id)

        pantry_dao.update(pantry_id, portions=4)
        item = pantry_dao.list_active()[0]
        self.assertAlmostEqual(item["portion_quantity"], 100)
        self.assertAlmostEqual(item["portions_remaining"], 4)

        pantry_dao.update(pantry_id, quantity=600)
        item = pantry_dao.list_active()[0]
        self.assertAlmostEqual(item["portion_quantity"], 100)
        self.assertAlmostEqual(item["portions_remaining"], 6)

        pantry_dao.update(pantry_id, portions=3)
        item = pantry_dao.list_active()[0]
        self.assertAlmostEqual(item["portion_quantity"], 200)
        self.assertAlmostEqual(item["portions_remaining"], 3)

        pantry_dao.update(pantry_id, portions=None)
        item = pantry_dao.list_active()[0]
        self.assertIsNone(item["portion_quantity"])
        self.assertIsNone(item["portions_remaining"])

    def test_cook_retry_and_undo_compensate_later_edits(self):
        rid = add_recipe("Rice bowl")
        pantry_id = pantry_dao.add(
            ingredient_key="name:rice",
            display_name="Rice",
            quantity=1,
            unit="kg",
        )

        cooked = meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            recipe_id=rid,
            status="cooked",
            event_key="cook-1",
        )
        self.assertFalse(cooked["idempotent"])
        row = db._conn().execute(
            "SELECT quantity, canonical_quantity FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertAlmostEqual(row["quantity"], 0.5)
        self.assertAlmostEqual(row["canonical_quantity"], 500)

        retry = meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            status="cooked",
            event_key="cook-1",
        )
        self.assertTrue(retry["idempotent"])
        self.assertEqual(
            retry["slot"]["version"], cooked["slot"]["version"]
        )
        self.assertEqual(retry["plan_version"], cooked["plan_version"])
        self.assertEqual(
            db._conn().execute(
                "SELECT COUNT(*) FROM cook_events"
            ).fetchone()[0],
            1,
        )

        # A later user edit adds 100 g. Undo must add the consumed 500 g
        # without erasing that unrelated edit.
        pantry_dao.update(pantry_id, quantity=0.6, unit="kg")
        undone = meal_service.patch_slot(
            "2026-07-20", "dinner", status="planned"
        )
        self.assertFalse(undone["pantry"]["legacy_without_ledger"])
        row = db._conn().execute(
            "SELECT quantity, canonical_quantity, exhausted_at "
            "FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertAlmostEqual(row["quantity"], 1.1)
        self.assertAlmostEqual(row["canonical_quantity"], 1100)
        self.assertIsNone(row["exhausted_at"])
        recipe = db._conn().execute(
            "SELECT cook_count, last_cooked_at FROM recipes WHERE id = ?", (rid,)
        ).fetchone()
        self.assertEqual(recipe["cook_count"], 0)
        self.assertIsNone(recipe["last_cooked_at"])

    def test_cooked_slot_requires_key_and_rejects_servings_change(self):
        rid = add_recipe("Rice bowl")
        meal_service.patch_slot(
            "2026-07-20", "dinner", recipe_id=rid
        )
        with self.assertRaisesRegex(
            meal_service.ConflictError, "idempotency key"
        ):
            meal_service.patch_slot(
                "2026-07-20", "dinner", status="cooked"
            )

        meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            status="cooked",
            event_key="cook-servings",
        )
        with self.assertRaisesRegex(
            meal_service.ConflictError, "changing its servings"
        ):
            meal_service.patch_slot(
                "2026-07-20", "dinner", servings=2
            )

    def test_batch_yield_creates_and_reuses_prepared_portions(self):
        rid = add_recipe("Four rice bowls", servings=4)
        pantry_id = pantry_dao.add(
            ingredient_key="name:rice",
            display_name="Rice",
            quantity=1,
            unit="kg",
        )

        first = meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            recipe_id=rid,
            status="cooked",
            event_key="batch-fresh",
            cook_mode="fresh",
            prepared_servings=4,
        )
        self.assertEqual(first["prepared"]["mode"], "fresh")
        self.assertAlmostEqual(first["prepared"]["available"], 3)
        pantry = db._conn().execute(
            "SELECT canonical_quantity FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertAlmostEqual(pantry["canonical_quantity"], 500)

        second = meal_service.patch_slot(
            "2026-07-21",
            "lunch",
            recipe_id=rid,
            status="cooked",
            event_key="batch-leftover",
            cook_mode="prepared",
        )
        self.assertEqual(second["prepared"]["mode"], "prepared")
        self.assertAlmostEqual(second["prepared"]["available"], 2)
        pantry = db._conn().execute(
            "SELECT canonical_quantity FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertAlmostEqual(pantry["canonical_quantity"], 500)

        with self.assertRaisesRegex(
            meal_service.ConflictError, "already used or edited"
        ):
            meal_service.patch_slot(
                "2026-07-20", "dinner", status="planned"
            )

        meal_service.patch_slot(
            "2026-07-21", "lunch", status="planned"
        )
        undone = meal_service.patch_slot(
            "2026-07-20", "dinner", status="planned"
        )
        self.assertTrue(undone["prepared"]["discarded_created_batch"])
        pantry = db._conn().execute(
            "SELECT canonical_quantity FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertAlmostEqual(pantry["canonical_quantity"], 1000)

    def test_scaled_batch_consumes_recipe_fraction(self):
        rid = add_recipe("Two rice bowls", servings=4)
        pantry_id = pantry_dao.add(
            ingredient_key="name:rice",
            display_name="Rice",
            quantity=1,
            unit="kg",
        )
        meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            recipe_id=rid,
            servings=1,
            status="cooked",
            event_key="scaled-batch",
            cook_mode="fresh",
            prepared_servings=2,
        )
        pantry = db._conn().execute(
            "SELECT canonical_quantity FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertAlmostEqual(pantry["canonical_quantity"], 750)
        batch = db._conn().execute(
            "SELECT portions_total, portions_remaining FROM prepared_batches"
        ).fetchone()
        self.assertEqual(
            (batch["portions_total"], batch["portions_remaining"]),
            (1, 1),
        )

    def test_expired_prepared_portions_are_visible_but_not_usable(self):
        rid = add_recipe("Expired rice bowls", servings=4)
        pantry_dao.add(
            ingredient_key="name:rice",
            display_name="Rice",
            quantity=1,
            unit="kg",
        )
        meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            recipe_id=rid,
            status="cooked",
            event_key="expired-source",
            cook_mode="fresh",
            prepared_servings=4,
        )
        expired_on = (date.today() - timedelta(days=1)).isoformat()
        db._conn().execute(
            "UPDATE prepared_batches SET expires_on = ?", (expired_on,)
        )

        self.assertEqual(prepared.available(rid), 0)
        items = prepared.list_active(rid)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["expired"])
        with self.assertRaisesRegex(
            meal_service.ConflictError, "not enough prepared portions"
        ):
            meal_service.patch_slot(
                "2026-07-21",
                "lunch",
                recipe_id=rid,
                status="cooked",
                event_key="expired-consume",
                cook_mode="prepared",
            )

    def test_undo_after_unit_dimension_change_creates_replacement(self):
        rid = add_recipe("Rice bowl")
        pantry_id = pantry_dao.add(
            ingredient_key="name:rice",
            display_name="Rice",
            quantity=1,
            unit="kg",
        )
        meal_service.patch_slot(
            "2026-07-20",
            "dinner",
            recipe_id=rid,
            status="cooked",
            event_key="cook-dimension",
        )

        pantry_dao.update(pantry_id, quantity=2, unit="piece")
        meal_service.patch_slot(
            "2026-07-20", "dinner", status="planned"
        )

        original = db._conn().execute(
            "SELECT quantity, unit, dimension FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertEqual(
            (original["quantity"], original["unit"], original["dimension"]),
            (2, "piece", "count"),
        )
        replacement = db._conn().execute(
            "SELECT quantity, unit, canonical_quantity, dimension "
            "FROM pantry_items WHERE id != ? AND source = 'recipe_undo'",
            (pantry_id,),
        ).fetchone()
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["unit"], "kg")
        self.assertAlmostEqual(replacement["quantity"], 0.5)
        self.assertAlmostEqual(replacement["canonical_quantity"], 500)
        self.assertEqual(replacement["dimension"], "mass")

    def test_legacy_undo_keeps_last_cooked_until_count_reaches_zero(self):
        rid = add_recipe("Legacy meal")
        last_cooked = "2026-07-17T12:00:00+00:00"
        db._conn().execute(
            "UPDATE recipes SET cook_count = 2, legacy_cook_count = 2, "
            "last_cooked_at = ?, legacy_last_cooked_at = ? WHERE id = ?",
            (last_cooked, last_cooked, rid),
        )
        db._conn().execute(
            "INSERT INTO meal_plan "
            "(date, slot, recipe_id, servings, status, cooked_at, origin) "
            "VALUES ('2026-07-20', 'dinner', ?, 1, 'cooked', ?, 'manual')",
            (rid, last_cooked),
        )

        meal_service.patch_slot(
            "2026-07-20", "dinner", status="planned"
        )
        recipe = db._conn().execute(
            "SELECT cook_count, last_cooked_at, legacy_cook_count, "
            "legacy_last_cooked_at FROM recipes WHERE id = ?",
            (rid,),
        ).fetchone()
        self.assertEqual(recipe["cook_count"], 1)
        self.assertEqual(recipe["legacy_cook_count"], 1)
        self.assertEqual(recipe["last_cooked_at"], last_cooked)
        self.assertEqual(recipe["legacy_last_cooked_at"], last_cooked)

    def test_proposal_does_not_change_plan_and_preserves_manual_slot(self):
        manual = add_recipe("Manual dinner")
        add_recipe("Breakfast", "breakfast", 0)
        add_recipe("Lunch", "lunch", 0)
        add_recipe("Generated dinner", "dinner", 0)
        add_recipe("Snack", "snack", 0)
        meal_service.patch_slot(
            "2026-07-20", "dinner", recipe_id=manual, servings=2
        )
        before = [
            tuple(row) for row in db._conn().execute(
                "SELECT date, slot, recipe_id, servings, status "
                "FROM meal_plan ORDER BY date, slot"
            ).fetchall()
        ]

        proposal = solver.create_proposal(
            "2026-07-20", preserve_manual=True
        )
        after_proposal = [
            tuple(row) for row in db._conn().execute(
                "SELECT date, slot, recipe_id, servings, status "
                "FROM meal_plan ORDER BY date, slot"
            ).fetchall()
        ]
        self.assertEqual(before, after_proposal)
        self.assertTrue(proposal["plan"]["2026-07-20"]["dinner"]["preserved"])

        solver.commit_proposal(
            proposal["proposal_id"], proposal["expected_version"]
        )
        row = db._conn().execute(
            "SELECT recipe_id, servings, origin FROM meal_plan "
            "WHERE date = '2026-07-20' AND slot = 'dinner'"
        ).fetchone()
        self.assertEqual(row["recipe_id"], manual)
        self.assertEqual(row["servings"], 2)
        self.assertEqual(row["origin"], "manual")

    def test_stale_proposal_is_rejected(self):
        add_recipe("Dinner")
        proposal = solver.create_proposal("2026-07-20")
        meal_service.patch_slot(
            "2026-07-20", "snack", is_training_day=True
        )
        with self.assertRaisesRegex(RuntimeError, "week changed"):
            solver.commit_proposal(
                proposal["proposal_id"], proposal["expected_version"]
            )

    def test_proposal_detects_change_during_generation(self):
        add_recipe("Dinner")
        original = solver.plan_week

        def mutate_then_plan(*args, **kwargs):
            meal_service.patch_slot(
                "2026-07-20", "snack", is_training_day=True
            )
            return original(*args, **kwargs)

        with mock.patch.object(
            solver, "plan_week", side_effect=mutate_then_plan
        ):
            with self.assertRaisesRegex(RuntimeError, "while the proposal"):
                solver.create_proposal("2026-07-20")

    def test_favorite_max_once_per_week_is_enforced(self):
        favorite = add_recipe("Favorite dinner")
        db._conn().execute(
            "UPDATE recipes SET last_cooked_at = ? WHERE id = ?",
            ("2026-07-18T12:00:00+00:00", favorite),
        )
        db.update_preferences(favorites=[favorite])
        db.kv_set(
            "favorites_bypass_mode", "max_once_per_week", is_default=False
        )

        proposal = solver.plan_week(
            "2026-07-20", preserve_manual=True
        )
        picked = [
            item["recipe_id"]
            for slots in proposal["plan"].values()
            for item in slots.values()
            if item is not None
        ]
        self.assertEqual(picked.count(favorite), 1)

    def test_feedback_round_trip_and_rating_changes_planner_ranking(self):
        neutral = add_recipe("Neutral dinner")
        preferred = add_recipe("Preferred dinner")

        self.assertEqual(
            recipes_dao.get(neutral)["feedback"],
            {
                "rating": None,
                "preference": "neutral",
                "updated_at": None,
            },
        )
        saved = recipes_dao.set_feedback(
            preferred,
            rating=5,
            preference="make_again",
        )
        self.assertEqual(saved["rating"], 5)
        self.assertEqual(saved["preference"], "make_again")

        proposal = solver.plan_week("2026-07-20")
        dinner = proposal["plan"]["2026-07-20"]["dinner"]
        self.assertEqual(dinner["recipe_id"], preferred)
        self.assertIn("Marked make again", dinner["reasons"])
        self.assertIn("Rated 5/5", dinner["reasons"])

        listed = {
            item["id"]: item
            for item in recipes_dao.list_()
        }
        self.assertEqual(listed[preferred]["rating"], 5)
        self.assertEqual(listed[preferred]["preference"], "make_again")

    def test_avoided_recipe_is_never_planner_eligible(self):
        avoided = add_recipe("Avoided dinner")
        available = add_recipe("Available dinner")
        recipes_dao.set_feedback(
            avoided,
            rating=1,
            preference="avoid",
        )

        proposal = solver.plan_week("2026-07-20")
        picked = [
            item["recipe_id"]
            for slots in proposal["plan"].values()
            for item in slots.values()
            if item is not None
        ]
        self.assertNotIn(avoided, picked)
        self.assertIn(available, picked)

    def test_training_placeholder_is_replaceable_and_flag_reaches_all_slots(self):
        for slot in ("breakfast", "lunch", "dinner", "snack"):
            add_recipe(f"Training {slot}", slot, 0)
        meal_service.patch_slot(
            "2026-07-20", "snack", is_training_day=True
        )

        proposal = solver.create_proposal(
            "2026-07-20", preserve_manual=True
        )
        day = proposal["plan"]["2026-07-20"]
        self.assertTrue(all(item is not None for item in day.values()))
        self.assertTrue(
            all(item["is_training_day"] for item in day.values())
        )
        self.assertFalse(day["snack"]["preserved"])

        solver.commit_proposal(
            proposal["proposal_id"], proposal["expected_version"]
        )
        rows = db._conn().execute(
            "SELECT recipe_id, is_training_day, origin FROM meal_plan "
            "WHERE date = '2026-07-20'"
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["recipe_id"] is not None for row in rows))
        self.assertTrue(all(row["is_training_day"] == 1 for row in rows))
        self.assertTrue(all(row["origin"] == "planner" for row in rows))


class MigrationTests(unittest.TestCase):
    def test_schema_seven_backfills_cached_barcode_nutrition(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(
                """
                CREATE TABLE pantry_items (
                  id INTEGER PRIMARY KEY,
                  ingredient_key TEXT,
                  display_name TEXT,
                  quantity REAL,
                  unit TEXT,
                  source TEXT,
                  portion_size_canonical REAL
                );
                CREATE TABLE recipes (id INTEGER PRIMARY KEY);
                CREATE TABLE ad_hoc_meals (
                  id INTEGER PRIMARY KEY,
                  date TEXT NOT NULL,
                  slot TEXT NOT NULL,
                  recipe_id INTEGER,
                  free_text TEXT,
                  servings REAL,
                  est_kcal REAL,
                  est_protein_g REAL,
                  est_carbs_g REAL,
                  est_fat_g REAL,
                  logged_at TEXT NOT NULL
                );
                CREATE TABLE user_barcodes (
                  ean TEXT PRIMARY KEY,
                  display_name TEXT NOT NULL,
                  quantity REAL NOT NULL,
                  unit TEXT,
                  added_at TEXT NOT NULL,
                  use_count INTEGER NOT NULL
                );
                CREATE TABLE barcode_cache (
                  ean TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  display_name TEXT,
                  brand TEXT,
                  package_quantity REAL,
                  package_unit TEXT,
                  kcal_100g REAL,
                  protein_100g REAL,
                  carbs_100g REAL,
                  fat_100g REAL,
                  fiber_100g REAL,
                  fetched_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );
                INSERT INTO pantry_items VALUES
                  (1, 'off:3017620422003', 'Saved juice', 1000, 'ml',
                   'barcode', NULL);
                INSERT INTO user_barcodes VALUES
                  ('3017620422003', 'Saved juice', 1000, 'ml',
                   '2026-07-18', 4);
                INSERT INTO barcode_cache VALUES
                  ('3017620422003', 'found', 'Juice', '', 1000, 'ml',
                   45, 0.8, 10, 0.1, 0.2, '2026-07-18', '2026-08-18');
                """
            )

            db._migration_7(connection)

            row = connection.execute(
                "SELECT ean, kcal_100g, protein_100g, nutrition_source, "
                "nutrition_confidence, nutrition_basis "
                "FROM pantry_items WHERE id = 1"
            ).fetchone()
            self.assertEqual(
                tuple(row),
                (
                    "3017620422003",
                    45,
                    0.8,
                    "off",
                    "medium",
                    "migrated_barcode_cache",
                ),
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(ad_hoc_meals)"
                )
            }
            self.assertIn("pantry_item_id", columns)
            self.assertIn("food_quantity", columns)
        finally:
            connection.close()

    def test_legacy_database_is_backed_up_and_migrated(self):
        db.close_thread_conn()
        for path in _DATA.glob("data.db*"):
            path.unlink()
        legacy = sqlite3.connect(os.environ["DB_PATH"])
        legacy.executescript(
            """
            CREATE TABLE user_profile (id INTEGER PRIMARY KEY);
            CREATE TABLE preferences (
              id INTEGER PRIMARY KEY,
              equipment_json TEXT DEFAULT '[]',
              dislikes_json TEXT DEFAULT '[]',
              allergies_json TEXT DEFAULT '[]',
              favorites_json TEXT DEFAULT '[]',
              supermarkets_json TEXT DEFAULT '[]'
            );
            CREATE TABLE settings_kv (
              key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT, is_default INTEGER
            );
            CREATE TABLE recipes (
              id INTEGER PRIMARY KEY, name TEXT, cook_count INTEGER DEFAULT 0,
              last_cooked_at TEXT
            );
            CREATE TABLE recipe_ingredients (
              id INTEGER PRIMARY KEY, recipe_id INTEGER, ingredient_key TEXT,
              display_name TEXT, quantity REAL, unit TEXT
            );
            CREATE TABLE pantry_items (
              id INTEGER PRIMARY KEY, ingredient_key TEXT, display_name TEXT,
              quantity REAL, unit TEXT
            );
            CREATE TABLE meal_plan (
              date TEXT, slot TEXT, recipe_id INTEGER, servings REAL,
              status TEXT, cooked_at TEXT, is_training_day INTEGER,
              PRIMARY KEY (date, slot)
            );
            PRAGMA user_version = 0;
            """
        )
        legacy.execute(
            "INSERT INTO pantry_items VALUES "
            "(1, 'unknown', 'Red onion', 1, 'kg')"
        )
        legacy.commit()
        legacy.close()

        db.init()
        self.assertEqual(
            db._conn().execute("PRAGMA user_version").fetchone()[0],
            db.SCHEMA_VERSION,
        )
        row = db._conn().execute(
            "SELECT ingredient_key, canonical_quantity, canonical_unit "
            "FROM pantry_items WHERE id = 1"
        ).fetchone()
        self.assertEqual(row["ingredient_key"], "name:red-onion")
        self.assertEqual(row["canonical_quantity"], 1000)
        self.assertEqual(row["canonical_unit"], "g")
        backups = list(
            (_DATA / "backups").glob(
                f"data.db.pre-v0-to-v{db.SCHEMA_VERSION}.*"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        columns = {
            row["name"]
            for row in db._conn().execute(
                "PRAGMA table_info(user_barcodes)"
            )
        }
        self.assertIn("quantity", columns)
        pantry_columns = {
            row["name"]
            for row in db._conn().execute(
                "PRAGMA table_info(pantry_items)"
            )
        }
        self.assertIn("portion_size_canonical", pantry_columns)
        self.assertIsNotNone(
            db._conn().execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'barcode_cache'"
            ).fetchone()
        )

    def test_schema_two_barcode_rows_gain_default_quantity(self):
        db.close_thread_conn()
        for path in _DATA.glob("data.db*"):
            path.unlink()
        legacy = sqlite3.connect(os.environ["DB_PATH"])
        legacy.executescript(db.SCHEMA)
        legacy.execute("DROP TABLE barcode_cache")
        legacy.execute(
            "CREATE TABLE user_barcodes_v2 ("
            "ean TEXT PRIMARY KEY, display_name TEXT NOT NULL, unit TEXT, "
            "added_at TEXT NOT NULL, use_count INTEGER NOT NULL DEFAULT 1)"
        )
        legacy.execute(
            "INSERT INTO user_barcodes_v2 VALUES "
            "('3017620422003', 'Saved product', 'g', "
            "'2026-07-18T12:00:00+00:00', 3)"
        )
        legacy.execute("DROP TABLE user_barcodes")
        legacy.execute(
            "ALTER TABLE user_barcodes_v2 RENAME TO user_barcodes"
        )
        legacy.execute("PRAGMA user_version = 2")
        legacy.commit()
        legacy.close()

        db.init()

        row = db._conn().execute(
            "SELECT display_name, quantity, unit, use_count "
            "FROM user_barcodes"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("Saved product", 1, "g", 3),
        )
        self.assertEqual(
            db._conn().execute("PRAGMA user_version").fetchone()[0],
            db.SCHEMA_VERSION,
        )
        self.assertIsNotNone(
            db._conn().execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'recognition_inbox'"
            ).fetchone()
        )

    def test_schema_three_gains_review_tables_and_nutrition_provenance(self):
        db.close_thread_conn()
        for path in _DATA.glob("data.db*"):
            path.unlink()
        legacy = sqlite3.connect(os.environ["DB_PATH"])
        legacy.executescript(db.SCHEMA)
        legacy.execute(
            "INSERT INTO recipes "
            "(id, name, source, servings, equipment_json, steps_json, "
            "created_at) VALUES "
            "(1, 'Legacy recipe', 'manual', 1, '[]', '[]', "
            "'2026-07-18T12:00:00+00:00')"
        )
        legacy.execute(
            "INSERT INTO recipe_ingredients "
            "(id, recipe_id, position, ingredient_key, display_name, "
            "quantity, unit, optional) "
            "VALUES (1, 1, 0, 'off:123', 'Legacy product', 100, 'g', 0)"
        )
        legacy.executescript(
            """
            DROP TABLE recognition_inbox;
            DROP TABLE price_history;
            DROP TABLE receipt_items;
            DROP TABLE receipt_imports;
            ALTER TABLE recipe_ingredients RENAME TO recipe_ingredients_v4;
            CREATE TABLE recipe_ingredients (
              id INTEGER PRIMARY KEY,
              recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
              position INTEGER NOT NULL,
              ingredient_key TEXT NOT NULL,
              display_name TEXT NOT NULL,
              display_name_it TEXT,
              quantity REAL,
              unit TEXT,
              optional INTEGER NOT NULL DEFAULT 0,
              kcal REAL,
              protein_g REAL,
              carbs_g REAL,
              fat_g REAL,
              fiber_g REAL
            );
            INSERT INTO recipe_ingredients
              (id, recipe_id, position, ingredient_key, display_name,
               display_name_it, quantity, unit, optional, kcal, protein_g,
               carbs_g, fat_g, fiber_g)
            SELECT id, recipe_id, position, ingredient_key, display_name,
                   display_name_it, quantity, unit, optional, kcal, protein_g,
                   carbs_g, fat_g, fiber_g
            FROM recipe_ingredients_v4;
            DROP TABLE recipe_ingredients_v4;
            CREATE INDEX idx_recipe_ingredients_recipe
              ON recipe_ingredients(recipe_id, position);
            PRAGMA user_version = 3;
            """
        )
        legacy.commit()
        legacy.close()

        db.init()

        self.assertEqual(
            db._conn().execute("PRAGMA user_version").fetchone()[0],
            db.SCHEMA_VERSION,
        )
        row = db._conn().execute(
            "SELECT nutrition_source, nutrition_confidence, nutrition_basis "
            "FROM recipe_ingredients WHERE id = 1"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("off", "low", "migrated_without_match_detail"),
        )
        for table in (
            "receipt_imports",
            "receipt_items",
            "price_history",
            "recognition_inbox",
        ):
            self.assertIsNotNone(
                db._conn().execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
            )

    def test_schema_five_gains_recipe_feedback(self):
        db.close_thread_conn()
        for path in _DATA.glob("data.db*"):
            path.unlink()
        legacy = sqlite3.connect(os.environ["DB_PATH"])
        legacy.executescript(db.SCHEMA)
        legacy.execute("DROP TABLE recipe_feedback")
        legacy.execute("PRAGMA user_version = 5")
        legacy.commit()
        legacy.close()

        db.init()

        self.assertEqual(
            db._conn().execute("PRAGMA user_version").fetchone()[0],
            db.SCHEMA_VERSION,
        )
        self.assertIsNotNone(
            db._conn().execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'recipe_feedback'"
            ).fetchone()
        )
        self.assertIsNotNone(
            db._conn().execute(
                "SELECT applied_at FROM migration_history WHERE version = 6"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
