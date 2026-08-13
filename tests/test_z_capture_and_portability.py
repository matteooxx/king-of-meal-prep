from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from PIL import Image


_FALLBACK = None
if "DB_PATH" not in os.environ:
    _FALLBACK = tempfile.TemporaryDirectory()
    _ROOT = Path(_FALLBACK.name)
    os.environ["DB_PATH"] = str(_ROOT / "data.db")
    os.environ["DB_BACKUP_DIR"] = str(_ROOT / "backups")
    os.environ["APP_ENV_PATH"] = str(_ROOT / "app.env")
    os.environ["NUTRITION_DB"] = str(_ROOT / "nutrition.db")

import config
import data_portability
import db
import image_processing
import receipts
import recognition
from barcodes.gtin import parse as parse_barcode
from nutrition import resolve as nutrition_resolve
from pantry import dao as pantry_dao
from recipes import dao as recipes_dao


def reset_database() -> None:
    db.close_thread_conn()
    nutrition_resolve.close_thread_conn()
    database = Path(config.DB_PATH)
    for path in database.parent.glob(database.name + "*"):
        path.unlink()
    backup_dir = Path(os.environ["DB_BACKUP_DIR"])
    if backup_dir.exists():
        for path in backup_dir.iterdir():
            path.unlink()
    Path(config.APP_ENV_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(config.APP_ENV_PATH).write_text("", encoding="utf-8")
    db.init()


def nutrition_fixture() -> None:
    nutrition_resolve.close_thread_conn()
    path = Path(config.NUTRITION_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE ingredients (
              key TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              category TEXT,
              kcal_100g REAL,
              protein_100g REAL,
              carbs_100g REAL,
              fat_100g REAL,
              fiber_100g REAL,
              source TEXT NOT NULL
            );
            CREATE TABLE barcodes (
              ean TEXT PRIMARY KEY,
              ingredient_key TEXT NOT NULL
            );
            INSERT INTO ingredients VALUES
              ('usda:rice', 'Rice', 'grain', 365, 7, 80, 1, 1, 'usda');
            INSERT INTO ingredients VALUES
              ('off:bar', 'Example bar', 'snack', 420, 8, 60, 16, NULL, 'off');
            """
        )
        connection.commit()
    finally:
        connection.close()


def review_image() -> tuple[bytes, str]:
    image = Image.new("RGB", (80, 60), "#d6cfb0")
    try:
        return image_processing.review_jpeg(image)
    finally:
        image.close()


class RecognitionInboxTests(unittest.TestCase):
    def setUp(self):
        reset_database()

    def test_barcode_miss_is_upserted_and_resolved_with_photo(self):
        barcode = parse_barcode("3017620422003")
        first = recognition.record_barcode_miss(
            barcode,
            reason="not found",
        )
        second = recognition.record_barcode_miss(
            barcode,
            reason="provider unavailable",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["attempt_count"], 2)

        image_jpeg, image_sha256 = review_image()
        attached = recognition.attach_photo(
            second["id"],
            image_jpeg=image_jpeg,
            image_sha256=image_sha256,
        )
        self.assertTrue(attached["has_image"])
        self.assertEqual(recognition.image(second["id"]), image_jpeg)

        result = recognition.resolve_item(
            second["id"],
            display_name="Reviewed spread",
            ingredient_key=None,
            quantity=350,
            unit="g",
            expires_on="2026-08-01",
            add_to_pantry=True,
        )
        self.assertIsNotNone(result["pantry_item_id"])
        self.assertEqual(result["item"]["status"], "resolved")
        pantry = db._conn().execute(
            "SELECT display_name, quantity, unit FROM pantry_items"
        ).fetchone()
        self.assertEqual(tuple(pantry), ("Reviewed spread", 350, "g"))
        saved = db._conn().execute(
            "SELECT display_name, quantity FROM user_barcodes"
        ).fetchone()
        self.assertEqual(tuple(saved), ("Reviewed spread", 350))

    def test_product_photo_is_metadata_free_and_dismissible(self):
        source = Image.new("RGB", (1600, 900), "green")
        exif = Image.Exif()
        exif[0x010E] = "private description"
        stream = BytesIO()
        source.save(stream, format="JPEG", exif=exif)
        source.close()

        decoded = image_processing.decode(stream.getvalue())
        try:
            image_jpeg, image_sha256 = image_processing.review_jpeg(decoded)
        finally:
            decoded.close()
        with Image.open(BytesIO(image_jpeg)) as restored:
            self.assertEqual(len(restored.getexif()), 0)
            self.assertLessEqual(max(restored.size), 1200)
        self.assertLessEqual(
            len(image_jpeg),
            image_processing.MAX_REVIEW_BYTES,
        )

        item = recognition.create_product_photo(
            image_jpeg=image_jpeg,
            image_sha256=image_sha256,
            note="front label",
        )
        self.assertTrue(item["has_image"])
        self.assertTrue(recognition.dismiss(item["id"]))
        self.assertEqual(recognition.get(item["id"])["status"], "dismissed")

    def test_review_image_size_failure_is_a_validation_error(self):
        image = Image.new("RGB", (80, 60), "green")
        try:
            with self.assertRaises(image_processing.ImageValidationError):
                image_processing.review_jpeg(image, max_bytes=1)
        finally:
            image.close()


class ReceiptReconciliationTests(unittest.TestCase):
    def setUp(self):
        reset_database()

    def test_receipt_commit_merges_flags_duplicates_and_records_price(self):
        pantry_id = pantry_dao.add(
            ingredient_key="name:apples",
            display_name="Apples",
            quantity=3,
            unit="piece",
        )
        receipt = receipts.create(
            raw_text=(
                "1 x Apples 2.00\n"
                "1 x Apples 2.10\n"
                "SUBTOTAL 4.10\n"
                "TOTAL 4.10\n"
                "CARD 4.10\n"
            ),
            image_jpeg=None,
            image_sha256=None,
            merchant="Example Market",
            purchased_on="2026-07-18",
            currency="EUR",
        )
        self.assertEqual(receipt["item_count"], 2)
        first, duplicate = receipt["items"]
        self.assertEqual(first["matched_pantry_item_id"], pantry_id)
        self.assertEqual(first["action"], "merge")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["action"], "skip")
        self.assertEqual(len(recognition.list_items()), 2)

        payload = [
            {
                "id": first["id"],
                "action": "merge",
                "display_name": "Apples",
                "quantity": 1,
                "unit": "piece",
                "line_total": 2,
                "ingredient_key": "name:apples",
            },
            {"id": duplicate["id"], "action": "skip"},
        ]
        result = receipts.commit(receipt["id"], items=payload)
        self.assertEqual(
            (result["added"], result["merged"], result["skipped"]),
            (0, 1, 1),
        )
        pantry = db._conn().execute(
            "SELECT quantity FROM pantry_items WHERE id = ?",
            (pantry_id,),
        ).fetchone()
        self.assertEqual(pantry["quantity"], 4)
        price = db._conn().execute(
            "SELECT line_total, unit_price, quantity, unit "
            "FROM price_history"
        ).fetchone()
        self.assertEqual(tuple(price), (2, 2, 1, "piece"))
        self.assertEqual(len(recognition.list_items()), 0)
        self.assertIsNone(
            result["receipt"]["items"][0]["previous_price"]
        )

        repeated = receipts.commit(receipt["id"], items=[])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(
            (repeated["added"], repeated["merged"], repeated["skipped"]),
            (0, 1, 1),
        )

        later = receipts.create(
            raw_text="1 x Apples 2.50\n",
            image_jpeg=None,
            image_sha256=None,
            merchant="Another Market",
            purchased_on="2026-07-19",
            currency="EUR",
        )
        self.assertEqual(
            later["items"][0]["previous_price"]["unit_price"],
            2,
        )

    def test_receipt_commit_rolls_back_every_item_on_conflict(self):
        receipt = receipts.create(
            raw_text="1 x Apples 2.00\n1 x Pears 3.00\n",
            image_jpeg=None,
            image_sha256=None,
            merchant=None,
            purchased_on=None,
            currency="EUR",
        )
        first, second = receipt["items"]
        payload = [
            {
                "id": first["id"],
                "action": "add",
                "display_name": "Apples",
                "quantity": 1,
                "unit": "piece",
                "line_total": 2,
                "ingredient_key": "name:apples",
            },
            {
                "id": second["id"],
                "action": "merge",
                "display_name": "Pears",
                "quantity": 1,
                "unit": "piece",
                "line_total": 3,
                "ingredient_key": "name:pears",
            },
        ]
        with self.assertRaises(receipts.ReceiptConflictError):
            receipts.commit(receipt["id"], items=payload)
        self.assertEqual(
            db._conn().execute("SELECT COUNT(*) FROM pantry_items").fetchone()[0],
            0,
        )
        self.assertEqual(
            db._conn().execute("SELECT COUNT(*) FROM price_history").fetchone()[0],
            0,
        )
        self.assertEqual(receipts.get(receipt["id"])["status"], "review")


class NutritionConfidenceTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        nutrition_fixture()

    def tearDown(self):
        nutrition_resolve.close_thread_conn()

    def test_confidence_reflects_match_source_and_basis(self):
        exact = nutrition_resolve.resolve("100 g Rice")
        self.assertEqual(exact["nutrition_source"], "usda")
        self.assertEqual(exact["nutrition_confidence"], "high")
        self.assertEqual(exact["nutrition_basis"], "exact_name")

        prefix = nutrition_resolve.resolve("100 g Ric")
        self.assertEqual(prefix["nutrition_confidence"], "medium")
        self.assertEqual(prefix["nutrition_basis"], "name_prefix")

        selected = nutrition_resolve.by_key("off:bar")
        self.assertEqual(selected["nutrition_source"], "off")
        self.assertEqual(selected["nutrition_confidence"], "medium")
        self.assertEqual(selected["nutrition_basis"], "user_selected")

    def test_selected_profile_is_rescaled_from_structured_fields(self):
        selected = nutrition_resolve.resolve_fields(
            display_name="My rice",
            quantity=50,
            unit="g",
            ingredient_key="usda:rice",
        )

        self.assertEqual(selected["ingredient_key"], "usda:rice")
        self.assertEqual(selected["nutrition_status"], "counted")
        self.assertEqual(selected["nutrition_basis"], "user_selected")
        self.assertAlmostEqual(selected["kcal"], 182.5)
        self.assertAlmostEqual(selected["protein_g"], 3.5)

    def test_missing_amount_is_reported_instead_of_counted_as_100g(self):
        unresolved_amount = nutrition_resolve.resolve_fields(
            display_name="Rice",
            quantity=None,
            unit="g",
            ingredient_key="usda:rice",
        )

        self.assertEqual(
            unresolved_amount["nutrition_status"],
            "missing_amount",
        )
        self.assertEqual(
            unresolved_amount["nutrition_basis"],
            "amount_missing",
        )
        self.assertIsNone(unresolved_amount["kcal"])

    def test_pantry_profile_scales_for_food_logging(self):
        pantry_id = pantry_dao.add(
            ingredient_key="off:bar",
            display_name="Example bar",
            quantity=200,
            unit="g",
            source="barcode",
            ean="123",
            nutrition_profile=nutrition_resolve.by_key("off:bar"),
        )
        item = pantry_dao.get(pantry_id)
        logged = pantry_dao.nutrition_for_amount(
            item,
            quantity=30,
            unit="g",
        )

        self.assertTrue(item["nutrition_available"])
        self.assertEqual(logged["nutrition_status"], "counted")
        self.assertAlmostEqual(logged["kcal"], 126)
        self.assertAlmostEqual(logged["protein_g"], 2.4)

    def test_packaged_food_does_not_inherit_whole_food_piece_weight(self):
        pantry_id = pantry_dao.add(
            ingredient_key="off:juice",
            display_name="Orange juice",
            quantity=1,
            unit="piece",
            source="barcode",
            ean="5012345678900",
            nutrition_profile={
                "ingredient_key": "off:juice",
                "kcal_100g": 45,
                "protein_100g": 0.7,
                "carbs_100g": 10.4,
                "fat_100g": 0.2,
                "fiber_100g": 0.2,
                "nutrition_source": "off",
                "nutrition_confidence": "medium",
                "nutrition_basis": "barcode_exact",
            },
        )
        item = pantry_dao.get(pantry_id)

        self.assertFalse(item["nutrition_amount_available"])
        self.assertEqual(item["nutrition"]["nutrition_status"], "unknown_unit")
        logged = pantry_dao.nutrition_for_amount(
            item,
            quantity=250,
            unit="g",
        )
        self.assertEqual(logged["nutrition_status"], "counted")
        self.assertAlmostEqual(logged["kcal"], 112.5)

    def test_whole_food_piece_estimates_allow_safe_name_modifiers(self):
        profile = {
            "ingredient_key": "usda:orange",
            "kcal_100g": 47,
            "nutrition_source": "usda",
            "nutrition_confidence": "medium",
            "nutrition_basis": "user_selected",
        }
        whole_food = nutrition_resolve.scale_profile(
            profile,
            quantity=1,
            unit="piece",
            display_name="large orange",
        )
        product = nutrition_resolve.scale_profile(
            profile,
            quantity=1,
            unit="piece",
            display_name="orange juice",
        )

        self.assertEqual(whole_food["nutrition_status"], "counted")
        self.assertAlmostEqual(whole_food["grams"], 130)
        self.assertEqual(product["nutrition_status"], "unknown_unit")
        self.assertIsNone(product["kcal"])

    def test_pantry_identity_edit_refreshes_or_clears_nutrition(self):
        pantry_id = pantry_dao.add(
            ingredient_key="usda:rice",
            display_name="Rice",
            quantity=100,
            unit="g",
        )

        pantry_dao.update(
            pantry_id,
            display_name="Example bar",
            ingredient_key="off:bar",
        )
        updated = pantry_dao.get(pantry_id)
        self.assertEqual(updated["ingredient_key"], "off:bar")
        self.assertEqual(updated["nutrition_source"], "off")
        self.assertAlmostEqual(updated["kcal_100g"], 420)

        pantry_dao.update(
            pantry_id,
            display_name="Unknown product",
            ingredient_key="name:unknown-product",
        )
        cleared = pantry_dao.get(pantry_id)
        self.assertFalse(cleared["nutrition_available"])
        self.assertEqual(cleared["nutrition_source"], "unknown")

    def test_barcode_name_correction_keeps_label_nutrition(self):
        pantry_id = pantry_dao.add(
            ingredient_key="off:bar",
            display_name="Example bar",
            quantity=1,
            unit="piece",
            source="barcode",
            ean="123",
            nutrition_profile=nutrition_resolve.by_key("off:bar"),
        )

        pantry_dao.update(
            pantry_id,
            display_name="My corrected label",
            ingredient_key="name:my-corrected-label",
        )
        corrected = pantry_dao.get(pantry_id)
        self.assertEqual(corrected["display_name"], "My corrected label")
        self.assertEqual(corrected["nutrition_source"], "off")
        self.assertAlmostEqual(corrected["kcal_100g"], 420)

    def test_like_wildcards_are_treated_as_literal_search_text(self):
        self.assertEqual(nutrition_resolve.search("%"), [])
        self.assertEqual(nutrition_resolve.search("_"), [])

    def test_recipe_provenance_round_trips_on_create_and_update(self):
        ingredient = {
            "ingredient_key": "usda:rice",
            "display_name": "Rice",
            "display_name_it": None,
            "quantity": 100,
            "unit": "g",
            "optional": False,
            "kcal": 365,
            "protein_g": 7,
            "carbs_g": 80,
            "fat_g": 1,
            "fiber_g": 1,
            "nutrition_source": "usda",
            "nutrition_confidence": "high",
            "nutrition_basis": "exact_name",
        }
        recipe_id = recipes_dao.create(
            name="Rice bowl",
            name_it=None,
            source="manual",
            source_url=None,
            servings=1,
            total_time_min=20,
            active_time_min=5,
            difficulty=1,
            cuisine=None,
            meal_slot="dinner",
            equipment=[],
            steps=[],
            notes=None,
            ingredients=[ingredient],
        )
        saved = recipes_dao.get(recipe_id)["ingredients"][0]
        self.assertEqual(
            (
                saved["nutrition_source"],
                saved["nutrition_confidence"],
                saved["nutrition_basis"],
            ),
            ("usda", "high", "exact_name"),
        )

        revised = {
            **ingredient,
            "nutrition_source": "user",
            "nutrition_confidence": "low",
            "nutrition_basis": "user_entered",
        }
        recipes_dao.update(recipe_id, fields={}, ingredients=[revised])
        saved = recipes_dao.get(recipe_id)["ingredients"][0]
        self.assertEqual(
            (
                saved["nutrition_source"],
                saved["nutrition_confidence"],
                saved["nutrition_basis"],
            ),
            ("user", "low", "user_entered"),
        )


class DataPortabilityTests(unittest.TestCase):
    def setUp(self):
        reset_database()

    def test_portable_exports_exclude_secrets_images_and_transient_tables(self):
        Path(config.APP_ENV_PATH).write_text(
            "SECRET_KEY=do-not-export\nGEMINI_API_KEY=also-private\n",
            encoding="utf-8",
        )
        db.kv_set(
            "public_base_url",
            "https://private.example.invalid",
            is_default=False,
        )
        db._conn().execute(
            "INSERT INTO reset_tokens "
            "(token_hash, created_at, expires_at) VALUES "
            "('private-reset-hash', '2026-07-18', '2026-07-19')"
        )
        db._conn().execute(
            "INSERT INTO llm_calls (ts, model, purpose, status) "
            "VALUES ('2026-07-18', 'private-model', 'test', 'ok')"
        )
        pantry_dao.add(
            ingredient_key="name:formula",
            display_name="=HYPERLINK(\"https://example.invalid\")",
            quantity=1,
            unit="piece",
        )
        image_jpeg, image_sha256 = review_image()
        recognition.create_product_photo(
            image_jpeg=image_jpeg,
            image_sha256=image_sha256,
            note="portable",
        )
        recipe_id = recipes_dao.create(
            name="Rated portable recipe",
            name_it=None,
            source="manual",
            source_url=None,
            servings=2,
            total_time_min=20,
            active_time_min=10,
            difficulty=1,
            cuisine=None,
            meal_slot="dinner",
            equipment=[],
            steps=["Cook for 10 minutes."],
            notes=None,
            ingredients=[],
        )
        recipes_dao.set_feedback(
            recipe_id,
            rating=5,
            preference="make_again",
        )

        payload = data_portability.portable_payload()
        encoded = json.dumps(payload)
        self.assertFalse(payload["privacy"]["contains_secrets"])
        self.assertNotIn("reset_tokens", payload["tables"])
        self.assertNotIn("llm_calls", payload["tables"])
        self.assertNotIn("barcode_cache", payload["tables"])
        self.assertNotIn("do-not-export", encoded)
        self.assertNotIn("also-private", encoded)
        self.assertNotIn("private-reset-hash", encoded)
        self.assertNotIn("private.example.invalid", encoded)
        self.assertTrue(
            payload["tables"]["recognition_inbox"][0]["has_image"]
        )
        self.assertNotIn(
            "image_jpeg",
            payload["tables"]["recognition_inbox"][0],
        )
        self.assertEqual(
            payload["tables"]["recipe_feedback"][0]["rating"],
            5,
        )

        archive = zipfile.ZipFile(
            BytesIO(data_portability.portable_csv_zip_bytes())
        )
        pantry_csv = archive.read("pantry_items.csv").decode("utf-8")
        self.assertIn("'=HYPERLINK", pantry_csv)

    def test_encrypted_backup_round_trip_wrong_password_and_tamper(self):
        secret_env = "SECRET_KEY=backup-secret\nADMIN_PASS_HASH=private-hash\n"
        Path(config.APP_ENV_PATH).write_text(secret_env, encoding="utf-8")
        pantry_dao.add(
            ingredient_key="name:rice",
            display_name="Rice",
            quantity=500,
            unit="g",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "king.kingbackup"
            passphrase = "correct horse battery staple"
            manifest = data_portability.create_encrypted_backup(
                backup,
                passphrase,
            )
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(manifest["schema_version"], db.SCHEMA_VERSION)

            report = data_portability.validate_encrypted_backup(
                backup,
                passphrase,
            )
            self.assertEqual(report["quick_check"], "ok")
            self.assertTrue(report["contains_app_env"])

            with self.assertRaises(data_portability.BackupError):
                data_portability.validate_encrypted_backup(
                    backup,
                    "incorrect passphrase value",
                )

            tampered = root / "tampered.kingbackup"
            content = bytearray(backup.read_bytes())
            content[len(content) // 2] ^= 0x01
            tampered.write_bytes(content)
            with self.assertRaises(data_portability.BackupError):
                data_portability.validate_encrypted_backup(
                    tampered,
                    passphrase,
                )

            staged = root / "staged"
            staged_report = data_portability.stage_restore(
                backup,
                passphrase,
                staged,
            )
            self.assertEqual(staged_report["quick_check"], "ok")
            self.assertEqual(
                (staged / "app.env.restored").read_text("utf-8"),
                secret_env,
            )
            restored = sqlite3.connect(staged / "data.db.restored")
            try:
                self.assertEqual(
                    restored.execute("PRAGMA quick_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(
                    restored.execute(
                        "SELECT display_name FROM pantry_items"
                    ).fetchone()[0],
                    "Rice",
                )
            finally:
                restored.close()

    def test_failed_backup_removes_plaintext_and_partial_files(self):
        Path(config.APP_ENV_PATH).write_bytes(
            b"x" * (data_portability.MAX_ENV_BYTES + 1)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "failed.kingbackup"
            with self.assertRaises(data_portability.BackupError):
                data_portability.create_encrypted_backup(
                    backup,
                    "correct horse battery staple",
                )
            self.assertFalse(backup.exists())
            self.assertEqual(
                [
                    path.name
                    for path in root.iterdir()
                    if path.name.startswith(".king-backup-")
                ],
                [],
            )


if __name__ == "__main__":
    unittest.main()
