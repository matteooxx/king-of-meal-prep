#!/usr/bin/env python3
"""Run an isolated Flask/API smoke test without external services."""
from __future__ import annotations

import os
from io import BytesIO
import sqlite3
import sys
import tempfile
from pathlib import Path

import bcrypt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _nutrition_fixture(path: Path) -> None:
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
            INSERT INTO ingredients
              (key, name, kcal_100g, protein_100g, carbs_100g,
               fat_100g, fiber_100g, source)
            VALUES ('fixture:rice', 'Rice', 365, 7, 80, 1, 1, 'usda');
            INSERT INTO barcodes (ean, ingredient_key)
            VALUES ('3017620422003', 'fixture:rice');
            """
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="king-smoke-") as tmp:
        root = Path(tmp)
        nutrition_path = root / "nutrition.db"
        _nutrition_fixture(nutrition_path)
        password = "correct-horse-smoke"
        os.environ.update({
            "ADMIN_USER": "admin",
            "ADMIN_PASS_HASH": bcrypt.hashpw(
                password.encode("utf-8"), bcrypt.gensalt()
            ).decode("ascii"),
            "SECRET_KEY": "smoke-test-secret-key-not-for-production",
            "TRUSTED_HOSTS": "localhost,127.0.0.1",
            "DB_PATH": str(root / "data.db"),
            "DB_BACKUP_DIR": str(root / "backups"),
            "NUTRITION_DB": str(nutrition_path),
            "APP_ENV_PATH": str(root / "app.env"),
            "ENV_MARKER_PATH": str(root / ".env-changed"),
        })
        (root / "app.env").write_text("", encoding="utf-8")

        import db
        from app import app

        db.mark_setup_completed()
        app.config.update(TESTING=True)
        client = app.test_client()

        health = client.get("/health")
        assert health.status_code == 200, health.get_data(as_text=True)
        assert health.get_json()["schema_version"] == db.SCHEMA_VERSION
        assert "Content-Security-Policy" in health.headers

        logged_out = client.get("/today")
        assert logged_out.status_code == 302
        assert logged_out.headers["Location"].endswith("/login")

        login = client.post(
            "/api/login",
            json={"username": "admin", "password": password},
        )
        assert login.status_code == 200, login.get_data(as_text=True)
        me = client.get("/api/me")
        csrf = me.get_json()["csrf_token"]

        today = client.get("/today")
        assert today.status_code == 200
        assert b'id="todayRoot"' in today.data
        assert b"/static/js/today.js" in today.data
        assert b'data-prepared-shelf-life="4"' in today.data
        assert b'data-frozen-shelf-life="90"' in today.data
        ingredient_preview = client.post(
            "/api/nutrition/ingredients/preview",
            json={
                "ingredients": [{
                    "display_name": "My rice",
                    "quantity": 50,
                    "unit": "g",
                    "ingredient_key": "fixture:rice",
                }],
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert ingredient_preview.status_code == 200
        preview_payload = ingredient_preview.get_json()
        assert preview_payload["summary"]["complete"]
        assert preview_payload["items"][0]["kcal"] == 182.5
        recipe_created = client.post(
            "/api/recipes",
            json={
                "name": "Smoke timer pasta",
                "source": "manual",
                "servings": 2,
                "meal_slot": "dinner",
                "steps": [
                    "Boil the pasta for 10 minutes.",
                    "Drain and serve.",
                ],
                "ingredients": [],
                "accept_incomplete_nutrition": True,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert recipe_created.status_code == 201, recipe_created.get_data(
            as_text=True
        )
        recipe_id = recipe_created.get_json()["id"]
        guided = client.get(f"/recipes/{recipe_id}/cook")
        assert guided.status_code == 200
        assert b'id="guidedCookRoot"' in guided.data
        assert b"/static/js/guided_cook.js" in guided.data
        feedback = client.patch(
            f"/api/recipes/{recipe_id}/feedback",
            json={"rating": 5, "preference": "make_again"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert feedback.status_code == 200, feedback.get_data(as_text=True)
        assert feedback.get_json()["feedback"]["rating"] == 5
        assert client.get(
            f"/api/recipes/{recipe_id}"
        ).get_json()["feedback"]["preference"] == "make_again"
        invalid_feedback = client.patch(
            f"/api/recipes/{recipe_id}/feedback",
            json={"rating": 6},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert invalid_feedback.status_code == 422
        prepared = client.get("/api/prepared")
        assert prepared.status_code == 200
        assert prepared.get_json()["total_batches"] == 0
        barcode = client.post(
            "/api/pantry/from-barcode",
            json={"ean": "3017620422003"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert barcode.status_code == 200, barcode.get_data(as_text=True)
        assert barcode.get_json()["source"] == "off_index"
        barcode_proposal = barcode.get_json()["proposal"]
        known_food = client.post(
            "/api/pantry",
            json={
                **barcode_proposal,
                "source": "barcode",
                "ean": "3017620422003",
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert known_food.status_code == 201, known_food.get_data(as_text=True)
        known_food_id = known_food.get_json()["id"]
        nutrition_preview = client.post(
            "/api/log/pantry-preview",
            json={
                "pantry_item_id": known_food_id,
                "quantity": 50,
                "unit": "g",
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert nutrition_preview.status_code == 200
        assert nutrition_preview.get_json()["nutrition"]["kcal"] == 182.5
        logged_food = client.post(
            "/api/log/ad-hoc",
            json={
                "date": "2026-07-18",
                "slot": "snack",
                "pantry_item_id": known_food_id,
                "quantity": 50,
                "unit": "g",
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert logged_food.status_code == 200, logged_food.get_data(as_text=True)
        day_log = client.get("/api/log/2026-07-18").get_json()
        assert day_log["totals"]["kcal"] == 182.5
        assert day_log["ad_hoc"][0]["pantry_item_id"] == known_food_id

        db.kv_set("barcode_online_lookup", False, is_default=False)
        local_miss = client.post(
            "/api/pantry/from-barcode",
            json={"ean": "123456"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert local_miss.status_code == 404
        assert "online lookup is disabled" in local_miss.get_json()["error"]
        assert local_miss.get_json()["inbox_item"]["status"] == "open"
        inbox = client.get("/api/recognition-inbox")
        assert inbox.status_code == 200
        assert len(inbox.get_json()["items"]) == 1

        reviewed = client.post(
            "/api/pantry",
            json={
                "display_name": "Reviewed product",
                "quantity": 1,
                "unit": "piece",
                "source": "barcode",
                "ean": "123456",
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert reviewed.status_code == 201, reviewed.get_data(as_text=True)
        assert client.get(
            "/api/recognition-inbox"
        ).get_json()["items"] == []

        portioned = client.post(
            "/api/pantry",
            json={
                "display_name": "Turkey mince",
                "quantity": 400,
                "unit": "g",
                "portions": 2,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert portioned.status_code == 201, portioned.get_data(as_text=True)
        portioned_id = portioned.get_json()["id"]
        pantry = client.get("/api/pantry").get_json()
        pantry_items = [
            item
            for bucket in pantry["buckets"].values()
            for item in bucket
        ]
        turkey = next(item for item in pantry_items if item["id"] == portioned_id)
        assert turkey["portion_quantity"] == 200
        assert turkey["portions_remaining"] == 2
        consumed = client.post(
            f"/api/pantry/{portioned_id}/consume-portion",
            json={},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert consumed.status_code == 200, consumed.get_data(as_text=True)
        assert consumed.get_json()["item"]["quantity"] == 200
        assert consumed.get_json()["item"]["portions_remaining"] == 1

        invalid_barcode = client.post(
            "/api/pantry/from-barcode",
            json={"ean": "3017620422004"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert invalid_barcode.status_code == 422
        assert invalid_barcode.get_json()["field"] == "ean"

        import receipts

        receipt = receipts.create(
            raw_text="1 x Rice 2.50\nTOTAL 2.50\nCARD 2.50\n",
            image_jpeg=None,
            image_sha256=None,
            merchant="Smoke Market",
            purchased_on="2026-07-18",
            currency="EUR",
        )
        receipt_read = client.get(f"/api/receipts/{receipt['id']}")
        assert receipt_read.status_code == 200
        assert receipt_read.get_json()["item_count"] == 1
        item = receipt_read.get_json()["items"][0]
        committed = client.post(
            f"/api/receipts/{receipt['id']}/commit",
            json={
                "items": [{
                    "id": item["id"],
                    "action": "add",
                    "display_name": item["display_name"],
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "line_total": item["line_total"],
                    "ingredient_key": item["ingredient_key"],
                }],
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert committed.status_code == 200, committed.get_data(as_text=True)
        assert committed.get_json()["added"] == 1

        portable = client.get("/api/data/export.json")
        assert portable.status_code == 200
        assert portable.get_json()["privacy"]["contains_secrets"] is False
        assert portable.get_json()["tables"]["recipe_feedback"][0][
            "preference"
        ] == "make_again"
        assert "attachment" in portable.headers["Content-Disposition"]

        passphrase = "smoke backup passphrase"
        backup = client.post(
            "/api/data/backup",
            json={"passphrase": passphrase},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert backup.status_code == 200, backup.get_data(as_text=True)
        backup_bytes = backup.get_data()
        assert backup_bytes.startswith(b"KINGBK1\x00")
        backup.close()
        validation = client.post(
            "/api/data/backup/validate",
            data={
                "file": (BytesIO(backup_bytes), "smoke.kingbackup"),
                "passphrase": passphrase,
            },
            content_type="multipart/form-data",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert validation.status_code == 200, validation.get_data(as_text=True)
        assert validation.get_json()["quick_check"] == "ok"

        rejected = client.patch(
            "/api/settings/kv/prepared_shelf_life_days",
            json={"value": 5},
        )
        assert rejected.status_code == 403
        accepted = client.patch(
            "/api/settings/kv/prepared_shelf_life_days",
            json={"value": 5},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        assert accepted.status_code == 200, accepted.get_data(as_text=True)
        assert accepted.get_json()["value"] == 5
        db.close_thread_conn()

    print("API smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
