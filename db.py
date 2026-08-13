"""SQLite layer for king-of-meal-prep.

One file at config.DB_PATH. Schema is bootstrapped idempotently on first
use; future migrations append to MIGRATIONS and read user_version.

The connection is per-thread (sqlite3.connect's default). Gunicorn runs us
with 8 gthread workers in one process, so we get one db file + 8 threads
sharing it via SQLite's WAL mode — fine for single-tenant traffic.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import config

log = logging.getLogger("king-of-meal-prep.db")

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """Per-thread SQLite connection. isolation_level=None puts sqlite3 in
    autocommit mode; we use explicit BEGIN inside `tx()` for atomic groups."""
    c = getattr(_local, "conn", None)
    if c is None:
        # autocommit = explicit transactions only when we ask. Mixing implicit
        # transactions (sqlite3 default) with the manual BEGIN in tx() was the
        # original bug — every `_conn().execute()` call would commit
        # immediately, breaking atomicity within tx() blocks.
        c = sqlite3.connect(config.DB_PATH, timeout=10.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("PRAGMA trusted_schema = OFF")
        _local.conn = c
    return c


@contextmanager
def tx():
    """Explicit transaction context.

    Usage:
        with db.tx() as c:
            c.execute(...)
            other_dao_function()   # any nested _conn().execute calls run
                                   # against the SAME thread's connection
                                   # and so participate in this transaction

    Nested `tx()` is forbidden — sqlite3 raises
    `OperationalError: cannot start a transaction within a transaction`.
    """
    c = _conn()
    # Detect nested-tx misuse and fail loudly rather than corrupt state.
    if getattr(_local, "in_tx", False):
        raise RuntimeError("nested db.tx() is not supported")
    _local.in_tx = True
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # Already auto-rolled back by sqlite (e.g. mid-statement crash).
            pass
        raise
    finally:
        _local.in_tx = False


def close_thread_conn() -> None:
    """Close the per-thread connection (called on worker shutdown)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _local.conn = None


# ---------------------------------------------------------------------------
# Schema bootstrap. SQL kept verbatim from the design doc so the doc and code
# stay in lockstep. Migrations go in numbered functions below; user_version
# tracks the highest applied migration.
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profile (
  id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  weight_kg REAL,
  height_cm REAL,
  age_years INTEGER,
  sex TEXT CHECK (sex IN ('m','f')),
  activity_level TEXT CHECK (activity_level IN ('sedentary','light','moderate','active','very_active')),
  goal TEXT CHECK (goal IN ('cut','maintain','bulk')),
  rest_kcal_target INTEGER,
  rest_protein_g INTEGER,
  rest_carbs_g INTEGER,
  rest_fat_g INTEGER,
  training_kcal_delta INTEGER NOT NULL DEFAULT 300,
  training_protein_delta INTEGER NOT NULL DEFAULT 30,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  equipment_json TEXT NOT NULL DEFAULT '[]',
  dislikes_json TEXT NOT NULL DEFAULT '[]',
  allergies_json TEXT NOT NULL DEFAULT '[]',
  favorites_json TEXT NOT NULL DEFAULT '[]',
  supermarkets_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS settings_kv (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recipes (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_it TEXT,
  source TEXT NOT NULL CHECK (source IN ('manual','url','ocr','llm')),
  source_url TEXT,
  servings INTEGER NOT NULL DEFAULT 1,
  total_time_min INTEGER,
  active_time_min INTEGER,
  difficulty INTEGER CHECK (difficulty BETWEEN 1 AND 5),
  cuisine TEXT,
  meal_slot TEXT,
  equipment_json TEXT NOT NULL DEFAULT '[]',
  steps_json TEXT NOT NULL DEFAULT '[]',
  notes TEXT,
  kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, fiber_g REAL,
  created_at TEXT NOT NULL,
  last_cooked_at TEXT,
  cook_count INTEGER NOT NULL DEFAULT 0,
  archived_at TEXT,
  legacy_cook_count INTEGER NOT NULL DEFAULT 0,
  legacy_last_cooked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recipes_last_cooked ON recipes(last_cooked_at);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
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
  fiber_g REAL,
  nutrition_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_source IN ('usda','off','user','manual','unknown')),
  nutrition_confidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_confidence IN ('high','medium','low','unknown')),
  nutrition_basis TEXT
);
CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id, position);

CREATE TABLE IF NOT EXISTS recipe_feedback (
  recipe_id INTEGER PRIMARY KEY REFERENCES recipes(id) ON DELETE CASCADE,
  rating INTEGER CHECK (rating BETWEEN 1 AND 5),
  preference TEXT NOT NULL DEFAULT 'neutral'
    CHECK (preference IN ('neutral','make_again','avoid')),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pantry_items (
  id INTEGER PRIMARY KEY,
  ingredient_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  quantity REAL NOT NULL,
  unit TEXT NOT NULL,
  expires_on TEXT,
  source TEXT NOT NULL CHECK (source IN ('manual','receipt_ocr','barcode','recipe_undo')),
  added_at TEXT NOT NULL,
  exhausted_at TEXT,
  canonical_quantity REAL,
  canonical_unit TEXT,
  dimension TEXT,
  portion_size_canonical REAL
    CHECK (portion_size_canonical IS NULL OR portion_size_canonical > 0),
  ean TEXT,
  kcal_100g REAL,
  protein_100g REAL,
  carbs_100g REAL,
  fat_100g REAL,
  fiber_100g REAL,
  nutrition_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_source IN ('usda','off','user','manual','unknown')),
  nutrition_confidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_confidence IN ('high','medium','low','unknown')),
  nutrition_basis TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pantry_active ON pantry_items(exhausted_at, expires_on);
CREATE INDEX IF NOT EXISTS idx_pantry_ean ON pantry_items(ean);

CREATE TABLE IF NOT EXISTS meal_plan (
  date TEXT NOT NULL,
  slot TEXT NOT NULL CHECK (slot IN ('breakfast','lunch','dinner','snack')),
  recipe_id INTEGER REFERENCES recipes(id),
  servings REAL NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','cooked','skipped','substituted')),
  cooked_at TEXT,
  is_training_day INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual','planner')),
  locked INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (date, slot)
);

CREATE TABLE IF NOT EXISTS ad_hoc_meals (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  slot TEXT NOT NULL,
  recipe_id INTEGER REFERENCES recipes(id),
  free_text TEXT,
  servings REAL,
  est_kcal REAL,
  est_protein_g REAL,
  est_carbs_g REAL,
  est_fat_g REAL,
  pantry_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
  food_quantity REAL,
  food_unit TEXT,
  nutrition_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_source IN ('usda','off','user','manual','unknown')),
  nutrition_confidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_confidence IN ('high','medium','low','unknown')),
  nutrition_basis TEXT,
  logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ad_hoc_date ON ad_hoc_meals(date);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  status TEXT NOT NULL CHECK (status IN ('ok','rate_limited','error'))
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);

CREATE TABLE IF NOT EXISTS translations (
  english TEXT PRIMARY KEY,
  italian TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('static','gemini'))
);

-- User-learned barcode→name links. Populated when the user scans an EAN
-- that's not in nutrition.db and types the name themselves. Next scan of
-- the same EAN finds it without going through OFF. Distinct from the
-- nutrition.db barcodes table (read-only static dataset) so a re-import
-- of OFF doesn't wipe the user's local additions.
CREATE TABLE IF NOT EXISTS user_barcodes (
  ean TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  quantity REAL NOT NULL DEFAULT 1,
  unit TEXT,
  added_at TEXT NOT NULL,
  use_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS barcode_cache (
  ean TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('found','not_found')),
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
  expires_at TEXT NOT NULL,
  CHECK (status = 'not_found' OR display_name IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_barcode_cache_expiry
  ON barcode_cache(expires_at);

CREATE TABLE IF NOT EXISTS receipt_imports (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'review'
    CHECK (status IN ('review','committed','discarded')),
  merchant TEXT,
  purchased_on TEXT,
  currency TEXT NOT NULL DEFAULT 'EUR',
  raw_text TEXT NOT NULL,
  image_jpeg BLOB,
  image_sha256 TEXT,
  created_at TEXT NOT NULL,
  committed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipt_imports_status
  ON receipt_imports(status, created_at);

CREATE TABLE IF NOT EXISTS receipt_items (
  id INTEGER PRIMARY KEY,
  receipt_id INTEGER NOT NULL REFERENCES receipt_imports(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  raw_line TEXT NOT NULL,
  display_name TEXT NOT NULL,
  quantity REAL NOT NULL DEFAULT 1 CHECK (quantity > 0),
  unit TEXT NOT NULL DEFAULT 'piece',
  line_total REAL CHECK (line_total IS NULL OR line_total >= 0),
  ingredient_key TEXT NOT NULL,
  nutrition_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_source IN ('usda','off','user','manual','unknown')),
  nutrition_confidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_confidence IN ('high','medium','low','unknown')),
  nutrition_basis TEXT,
  ocr_confidence TEXT NOT NULL DEFAULT 'low'
    CHECK (ocr_confidence IN ('high','medium','low')),
  matched_pantry_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
  duplicate_of_id INTEGER REFERENCES receipt_items(id) ON DELETE SET NULL,
  action TEXT NOT NULL DEFAULT 'add'
    CHECK (action IN ('add','merge','skip')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','added','merged','skipped')),
  pantry_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  UNIQUE (receipt_id, position)
);
CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt
  ON receipt_items(receipt_id, position);

CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY,
  ingredient_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  merchant TEXT,
  purchased_on TEXT,
  currency TEXT NOT NULL DEFAULT 'EUR',
  quantity REAL NOT NULL,
  unit TEXT NOT NULL,
  line_total REAL NOT NULL CHECK (line_total >= 0),
  unit_price REAL NOT NULL CHECK (unit_price >= 0),
  receipt_id INTEGER REFERENCES receipt_imports(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_ingredient
  ON price_history(ingredient_key, purchased_on, created_at);

CREATE TABLE IF NOT EXISTS recognition_inbox (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL
    CHECK (kind IN ('barcode','product_photo','receipt_line')),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','resolved','dismissed')),
  barcode TEXT,
  barcode_display TEXT,
  raw_text TEXT,
  suggested_name TEXT,
  suggested_key TEXT,
  quantity REAL,
  unit TEXT,
  nutrition_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_source IN ('usda','off','user','manual','unknown')),
  nutrition_confidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK (nutrition_confidence IN ('high','medium','low','unknown')),
  nutrition_basis TEXT,
  image_jpeg BLOB,
  image_sha256 TEXT,
  receipt_item_id INTEGER REFERENCES receipt_items(id) ON DELETE SET NULL,
  attempt_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recognition_inbox_status
  ON recognition_inbox(status, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recognition_open_barcode
  ON recognition_inbox(barcode)
  WHERE kind = 'barcode' AND status = 'open' AND barcode IS NOT NULL;

CREATE TABLE IF NOT EXISTS plan_weeks (
  start_date TEXT PRIMARY KEY,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_proposals (
  id TEXT PRIMARY KEY,
  start_date TEXT NOT NULL,
  expected_version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_proposals_expiry ON plan_proposals(expires_at);

CREATE TABLE IF NOT EXISTS cook_events (
  id INTEGER PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE,
  date TEXT NOT NULL,
  slot TEXT NOT NULL,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id),
  servings REAL NOT NULL,
  cook_mode TEXT NOT NULL DEFAULT 'fresh',
  prepared_servings REAL NOT NULL DEFAULT 0,
  cooked_at TEXT NOT NULL,
  undone_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cook_events_active_slot
  ON cook_events(date, slot) WHERE undone_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_cook_events_recipe
  ON cook_events(recipe_id, cooked_at);

CREATE TABLE IF NOT EXISTS pantry_movements (
  id INTEGER PRIMARY KEY,
  cook_event_id INTEGER NOT NULL REFERENCES cook_events(id),
  pantry_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
  ingredient_key TEXT NOT NULL,
  delta_canonical REAL NOT NULL,
  canonical_unit TEXT NOT NULL,
  display_name TEXT NOT NULL,
  display_unit TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('consume','restore')),
  reverses_movement_id INTEGER REFERENCES pantry_movements(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pantry_movements_event
  ON pantry_movements(cook_event_id, kind);

CREATE TABLE IF NOT EXISTS prepared_batches (
  id INTEGER PRIMARY KEY,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id),
  source_cook_event_id INTEGER UNIQUE REFERENCES cook_events(id),
  portions_total REAL NOT NULL CHECK (portions_total >= 0),
  portions_remaining REAL NOT NULL CHECK (portions_remaining >= 0),
  prepared_at TEXT NOT NULL,
  expires_on TEXT,
  frozen INTEGER NOT NULL DEFAULT 0,
  discarded_at TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_prepared_batches_active
  ON prepared_batches(recipe_id, discarded_at, expires_on);

CREATE TABLE IF NOT EXISTS prepared_movements (
  id INTEGER PRIMARY KEY,
  cook_event_id INTEGER NOT NULL REFERENCES cook_events(id),
  batch_id INTEGER NOT NULL REFERENCES prepared_batches(id),
  delta_portions REAL NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('consume','restore')),
  reverses_movement_id INTEGER REFERENCES prepared_movements(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prepared_movements_event
  ON prepared_movements(cook_event_id, kind);

CREATE TABLE IF NOT EXISTS shopping_checks (
  week_start TEXT NOT NULL,
  item_key TEXT NOT NULL,
  checked INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (week_start, item_key)
);

CREATE TABLE IF NOT EXISTS reset_tokens (
  token_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  request_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_reset_tokens_expiry ON reset_tokens(expires_at);

CREATE TABLE IF NOT EXISTS migration_history (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
"""

SCHEMA_VERSION = 7


# Default values for the long tail of editable knobs. Inserted into
# settings_kv on first run (one INSERT OR IGNORE per key) so the user can
# see them in /settings and override via the wizard.
KV_DEFAULTS: dict[str, object] = {
    "slot_kcal_split": {
        "breakfast": 0.20, "lunch": 0.30, "dinner": 0.35, "snack": 0.15,
    },
    "cook_time_budget_min": {
        "mon": 30, "tue": 30, "wed": 30, "thu": 30, "fri": 30, "sat": 90, "sun": 90,
    },
    "rotation_window_days": 14,
    "favorites_bypass_mode": "always",
    "default_servings": 1,
    "leftover_behavior": "next_day_lunch",
    "prepared_shelf_life_days": 4,
    "frozen_shelf_life_days": 90,
    "aisle_order": ["produce", "meat", "fish", "dairy", "dry", "frozen", "other"],
    "shopping_include_optional": False,
    "barcode_online_lookup": True,
    "translation_mode": "hover",
    "timezone": "Europe/Dublin",
    "planner_preserve_manual": True,
    "expiry_days_by_category": {
        "produce": 5, "dairy": 14, "dry": 365, "frozen": 90, "meat": 3, "fish": 2,
    },
    "macro_split_override": None,
    "difficulty_labels": ["trivial", "easy", "medium", "hard", "project"],
    "training_delta_per_slot": None,
    "weekday_set": ["mon", "tue", "wed", "thu", "fri"],
    "setup_completed_at": None,
    # Bumped on every logout / password change / password reset so that any
    # session cookie issued before that point is rejected by check_auth(),
    # even though Flask cookies are stateless. See auth.py:check_auth.
    "auth_epoch": 1,
    # Public base URL for password-reset links. Set this in /settings to your
    # tailnet HTTPS URL; do NOT trust request.host_url (Host-header injection).
    "public_base_url": "",
}


def _column_names(c: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _add_column(c: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _column_names(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migration_1(c: sqlite3.Connection) -> None:
    """Data-trust release: ledgers, planner versions, reset tokens and units."""
    for definition in (
        "archived_at TEXT",
        "legacy_cook_count INTEGER NOT NULL DEFAULT 0",
        "legacy_last_cooked_at TEXT",
    ):
        _add_column(c, "recipes", definition)
    for definition in (
        "kcal REAL",
        "protein_g REAL",
        "carbs_g REAL",
        "fat_g REAL",
        "fiber_g REAL",
    ):
        _add_column(c, "recipe_ingredients", definition)
    for definition in (
        "canonical_quantity REAL",
        "canonical_unit TEXT",
        "dimension TEXT",
        "version INTEGER NOT NULL DEFAULT 1",
    ):
        _add_column(c, "pantry_items", definition)
    for definition in (
        "origin TEXT NOT NULL DEFAULT 'manual'",
        "locked INTEGER NOT NULL DEFAULT 0",
        "version INTEGER NOT NULL DEFAULT 1",
    ):
        _add_column(c, "meal_plan", definition)

    c.execute(
        "UPDATE recipes SET legacy_cook_count = cook_count, "
        "legacy_last_cooked_at = last_cooked_at"
    )

    from pantry.units import canonical_key, to_canonical

    for row in c.execute(
        "SELECT id, ingredient_key, display_name, quantity, unit FROM pantry_items"
    ).fetchall():
        amount, base_unit, dimension = to_canonical(row["quantity"], row["unit"])
        c.execute(
            "UPDATE pantry_items SET ingredient_key = ?, canonical_quantity = ?, "
            "canonical_unit = ?, dimension = ? WHERE id = ?",
            (
                canonical_key(row["display_name"], row["ingredient_key"]),
                amount,
                base_unit,
                dimension,
                row["id"],
            ),
        )
    for row in c.execute(
        "SELECT id, ingredient_key, display_name FROM recipe_ingredients"
    ).fetchall():
        key = canonical_key(row["display_name"], row["ingredient_key"])
        if key != row["ingredient_key"]:
            c.execute(
                "UPDATE recipe_ingredients SET ingredient_key = ? WHERE id = ?",
                (key, row["id"]),
            )

    statements = (
        """CREATE TABLE IF NOT EXISTS plan_weeks (
          start_date TEXT PRIMARY KEY,
          version INTEGER NOT NULL DEFAULT 1,
          updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS plan_proposals (
          id TEXT PRIMARY KEY,
          start_date TEXT NOT NULL,
          expected_version INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_plan_proposals_expiry "
        "ON plan_proposals(expires_at)",
        """CREATE TABLE IF NOT EXISTS cook_events (
          id INTEGER PRIMARY KEY,
          event_key TEXT NOT NULL UNIQUE,
          date TEXT NOT NULL,
          slot TEXT NOT NULL,
          recipe_id INTEGER NOT NULL REFERENCES recipes(id),
          servings REAL NOT NULL,
          cooked_at TEXT NOT NULL,
          undone_at TEXT
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cook_events_active_slot "
        "ON cook_events(date, slot) WHERE undone_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_cook_events_recipe "
        "ON cook_events(recipe_id, cooked_at)",
        """CREATE TABLE IF NOT EXISTS pantry_movements (
          id INTEGER PRIMARY KEY,
          cook_event_id INTEGER NOT NULL REFERENCES cook_events(id),
          pantry_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
          ingredient_key TEXT NOT NULL,
          delta_canonical REAL NOT NULL,
          canonical_unit TEXT NOT NULL,
          display_name TEXT NOT NULL,
          display_unit TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('consume','restore')),
          reverses_movement_id INTEGER REFERENCES pantry_movements(id),
          created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pantry_movements_event "
        "ON pantry_movements(cook_event_id, kind)",
        """CREATE TABLE IF NOT EXISTS reset_tokens (
          token_hash TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          used_at TEXT,
          request_ip TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_reset_tokens_expiry "
        "ON reset_tokens(expires_at)",
        """CREATE TABLE IF NOT EXISTS migration_history (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )""",
    )
    for statement in statements:
        c.execute(statement)

    from datetime import date, timedelta

    now = datetime.now(timezone.utc).isoformat()
    for row in c.execute("SELECT DISTINCT date FROM meal_plan").fetchall():
        day = date.fromisoformat(row["date"])
        monday = (day - timedelta(days=day.weekday())).isoformat()
        c.execute(
            "INSERT OR IGNORE INTO plan_weeks (start_date, version, updated_at) "
            "VALUES (?, 1, ?)",
            (monday, now),
        )


def _migration_2(c: sqlite3.Connection) -> None:
    """Batch-yield release: prepared portions and durable shopping checks."""
    for definition in (
        "cook_mode TEXT NOT NULL DEFAULT 'fresh'",
        "prepared_servings REAL NOT NULL DEFAULT 0",
    ):
        _add_column(c, "cook_events", definition)

    statements = (
        """CREATE TABLE IF NOT EXISTS prepared_batches (
          id INTEGER PRIMARY KEY,
          recipe_id INTEGER NOT NULL REFERENCES recipes(id),
          source_cook_event_id INTEGER UNIQUE REFERENCES cook_events(id),
          portions_total REAL NOT NULL CHECK (portions_total >= 0),
          portions_remaining REAL NOT NULL CHECK (portions_remaining >= 0),
          prepared_at TEXT NOT NULL,
          expires_on TEXT,
          frozen INTEGER NOT NULL DEFAULT 0,
          discarded_at TEXT,
          version INTEGER NOT NULL DEFAULT 1
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prepared_batches_active "
        "ON prepared_batches(recipe_id, discarded_at, expires_on)",
        """CREATE TABLE IF NOT EXISTS prepared_movements (
          id INTEGER PRIMARY KEY,
          cook_event_id INTEGER NOT NULL REFERENCES cook_events(id),
          batch_id INTEGER NOT NULL REFERENCES prepared_batches(id),
          delta_portions REAL NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('consume','restore')),
          reverses_movement_id INTEGER REFERENCES prepared_movements(id),
          created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prepared_movements_event "
        "ON prepared_movements(cook_event_id, kind)",
        """CREATE TABLE IF NOT EXISTS shopping_checks (
          week_start TEXT NOT NULL,
          item_key TEXT NOT NULL,
          checked INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (week_start, item_key)
        )""",
    )
    for statement in statements:
        c.execute(statement)


def _migration_3(c: sqlite3.Connection) -> None:
    """Hybrid barcode lookup cache and remembered package quantities."""
    c.execute(
        """CREATE TABLE IF NOT EXISTS user_barcodes (
          ean TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          quantity REAL NOT NULL DEFAULT 1,
          unit TEXT,
          added_at TEXT NOT NULL,
          use_count INTEGER NOT NULL DEFAULT 1
        )"""
    )
    _add_column(
        c, "user_barcodes", "quantity REAL NOT NULL DEFAULT 1"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS barcode_cache (
          ean TEXT PRIMARY KEY,
          status TEXT NOT NULL CHECK (status IN ('found','not_found')),
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
          expires_at TEXT NOT NULL,
          CHECK (status = 'not_found' OR display_name IS NOT NULL)
        )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_barcode_cache_expiry "
        "ON barcode_cache(expires_at)"
    )


def _migration_4(c: sqlite3.Connection) -> None:
    """Recognition review, receipt reconciliation, prices, and provenance."""
    for definition in (
        "nutrition_source TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK (nutrition_source IN ('usda','off','user','manual','unknown'))",
        "nutrition_confidence TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK (nutrition_confidence IN ('high','medium','low','unknown'))",
        "nutrition_basis TEXT",
    ):
        _add_column(c, "recipe_ingredients", definition)

    statements = (
        """CREATE TABLE IF NOT EXISTS receipt_imports (
          id INTEGER PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'review'
            CHECK (status IN ('review','committed','discarded')),
          merchant TEXT,
          purchased_on TEXT,
          currency TEXT NOT NULL DEFAULT 'EUR',
          raw_text TEXT NOT NULL,
          image_jpeg BLOB,
          image_sha256 TEXT,
          created_at TEXT NOT NULL,
          committed_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_receipt_imports_status "
        "ON receipt_imports(status, created_at)",
        """CREATE TABLE IF NOT EXISTS receipt_items (
          id INTEGER PRIMARY KEY,
          receipt_id INTEGER NOT NULL
            REFERENCES receipt_imports(id) ON DELETE CASCADE,
          position INTEGER NOT NULL,
          raw_line TEXT NOT NULL,
          display_name TEXT NOT NULL,
          quantity REAL NOT NULL DEFAULT 1 CHECK (quantity > 0),
          unit TEXT NOT NULL DEFAULT 'piece',
          line_total REAL CHECK (line_total IS NULL OR line_total >= 0),
          ingredient_key TEXT NOT NULL,
          nutrition_source TEXT NOT NULL DEFAULT 'unknown'
            CHECK (nutrition_source IN ('usda','off','user','manual','unknown')),
          nutrition_confidence TEXT NOT NULL DEFAULT 'unknown'
            CHECK (nutrition_confidence IN ('high','medium','low','unknown')),
          nutrition_basis TEXT,
          ocr_confidence TEXT NOT NULL DEFAULT 'low'
            CHECK (ocr_confidence IN ('high','medium','low')),
          matched_pantry_item_id INTEGER
            REFERENCES pantry_items(id) ON DELETE SET NULL,
          duplicate_of_id INTEGER
            REFERENCES receipt_items(id) ON DELETE SET NULL,
          action TEXT NOT NULL DEFAULT 'add'
            CHECK (action IN ('add','merge','skip')),
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','added','merged','skipped')),
          pantry_item_id INTEGER
            REFERENCES pantry_items(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          UNIQUE (receipt_id, position)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt "
        "ON receipt_items(receipt_id, position)",
        """CREATE TABLE IF NOT EXISTS price_history (
          id INTEGER PRIMARY KEY,
          ingredient_key TEXT NOT NULL,
          display_name TEXT NOT NULL,
          merchant TEXT,
          purchased_on TEXT,
          currency TEXT NOT NULL DEFAULT 'EUR',
          quantity REAL NOT NULL,
          unit TEXT NOT NULL,
          line_total REAL NOT NULL CHECK (line_total >= 0),
          unit_price REAL NOT NULL CHECK (unit_price >= 0),
          receipt_id INTEGER
            REFERENCES receipt_imports(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_price_history_ingredient "
        "ON price_history(ingredient_key, purchased_on, created_at)",
        """CREATE TABLE IF NOT EXISTS recognition_inbox (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL
            CHECK (kind IN ('barcode','product_photo','receipt_line')),
          status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open','resolved','dismissed')),
          barcode TEXT,
          barcode_display TEXT,
          raw_text TEXT,
          suggested_name TEXT,
          suggested_key TEXT,
          quantity REAL,
          unit TEXT,
          nutrition_source TEXT NOT NULL DEFAULT 'unknown'
            CHECK (nutrition_source IN ('usda','off','user','manual','unknown')),
          nutrition_confidence TEXT NOT NULL DEFAULT 'unknown'
            CHECK (nutrition_confidence IN ('high','medium','low','unknown')),
          nutrition_basis TEXT,
          image_jpeg BLOB,
          image_sha256 TEXT,
          receipt_item_id INTEGER
            REFERENCES receipt_items(id) ON DELETE SET NULL,
          attempt_count INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          resolved_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_recognition_inbox_status "
        "ON recognition_inbox(status, updated_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_recognition_open_barcode "
        "ON recognition_inbox(barcode) "
        "WHERE kind = 'barcode' AND status = 'open' AND barcode IS NOT NULL",
    )
    for statement in statements:
        c.execute(statement)

    c.execute(
        "UPDATE recipe_ingredients SET "
        "nutrition_source = CASE "
        "  WHEN ingredient_key LIKE 'off:%' THEN 'off' "
        "  WHEN ingredient_key LIKE 'usda:%' "
        "    OR ingredient_key LIKE 'fdc:%' THEN 'usda' "
        "  ELSE 'unknown' END, "
        "nutrition_confidence = CASE "
        "  WHEN ingredient_key LIKE 'off:%' "
        "    OR ingredient_key LIKE 'usda:%' "
        "    OR ingredient_key LIKE 'fdc:%' THEN 'low' "
        "  ELSE 'unknown' END, "
        "nutrition_basis = CASE "
        "  WHEN ingredient_key LIKE 'off:%' "
        "    OR ingredient_key LIKE 'usda:%' "
        "    OR ingredient_key LIKE 'fdc:%' "
        "  THEN 'migrated_without_match_detail' ELSE NULL END"
    )


def _migration_5(c: sqlite3.Connection) -> None:
    """Optional planned portion sizes for raw pantry items."""
    _add_column(c, "pantry_items", "portion_size_canonical REAL")


def _migration_6(c: sqlite3.Connection) -> None:
    """Recipe feedback used by the adaptive planner."""
    c.execute(
        """CREATE TABLE IF NOT EXISTS recipe_feedback (
          recipe_id INTEGER PRIMARY KEY
            REFERENCES recipes(id) ON DELETE CASCADE,
          rating INTEGER CHECK (rating BETWEEN 1 AND 5),
          preference TEXT NOT NULL DEFAULT 'neutral'
            CHECK (preference IN ('neutral','make_again','avoid')),
          updated_at TEXT NOT NULL
        )"""
    )


def _migration_7(c: sqlite3.Connection) -> None:
    """Durable pantry nutrition and traceable pantry-food logging."""
    for definition in (
        "ean TEXT",
        "kcal_100g REAL",
        "protein_100g REAL",
        "carbs_100g REAL",
        "fat_100g REAL",
        "fiber_100g REAL",
        "nutrition_source TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK (nutrition_source IN ('usda','off','user','manual','unknown'))",
        "nutrition_confidence TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK (nutrition_confidence IN ('high','medium','low','unknown'))",
        "nutrition_basis TEXT",
    ):
        _add_column(c, "pantry_items", definition)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_pantry_ean ON pantry_items(ean)"
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS ad_hoc_meals (
          id INTEGER PRIMARY KEY,
          date TEXT NOT NULL,
          slot TEXT NOT NULL,
          recipe_id INTEGER REFERENCES recipes(id),
          free_text TEXT,
          servings REAL,
          est_kcal REAL,
          est_protein_g REAL,
          est_carbs_g REAL,
          est_fat_g REAL,
          logged_at TEXT NOT NULL
        )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_ad_hoc_date ON ad_hoc_meals(date)"
    )
    for definition in (
        "pantry_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL",
        "food_quantity REAL",
        "food_unit TEXT",
        "nutrition_source TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK (nutrition_source IN ('usda','off','user','manual','unknown'))",
        "nutrition_confidence TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK (nutrition_confidence IN ('high','medium','low','unknown'))",
        "nutrition_basis TEXT",
    ):
        _add_column(c, "ad_hoc_meals", definition)

    # Older scan saves retained the product name and package quantity in
    # user_barcodes but dropped the EAN on the pantry row. Recover the most
    # frequently used matching barcode, then copy any cached label nutrition.
    pantry_columns = _column_names(c, "pantry_items")
    if "source" in pantry_columns:
        c.execute(
            "UPDATE pantry_items AS p SET ean = ("
            "  SELECT ub.ean FROM user_barcodes ub "
            "  WHERE LOWER(ub.display_name) = LOWER(p.display_name) "
            "  ORDER BY ub.use_count DESC, ub.added_at DESC LIMIT 1"
            ") WHERE p.source = 'barcode' AND p.ean IS NULL"
        )
    c.execute(
        "UPDATE pantry_items AS p SET "
        "kcal_100g = (SELECT bc.kcal_100g FROM barcode_cache bc "
        "             WHERE bc.ean = p.ean AND bc.status = 'found'), "
        "protein_100g = (SELECT bc.protein_100g FROM barcode_cache bc "
        "                WHERE bc.ean = p.ean AND bc.status = 'found'), "
        "carbs_100g = (SELECT bc.carbs_100g FROM barcode_cache bc "
        "              WHERE bc.ean = p.ean AND bc.status = 'found'), "
        "fat_100g = (SELECT bc.fat_100g FROM barcode_cache bc "
        "            WHERE bc.ean = p.ean AND bc.status = 'found'), "
        "fiber_100g = (SELECT bc.fiber_100g FROM barcode_cache bc "
        "              WHERE bc.ean = p.ean AND bc.status = 'found'), "
        "nutrition_source = 'off', "
        "nutrition_confidence = CASE "
        "  WHEN (SELECT "
        "    (bc.kcal_100g IS NOT NULL) + (bc.protein_100g IS NOT NULL) + "
        "    (bc.carbs_100g IS NOT NULL) + (bc.fat_100g IS NOT NULL) + "
        "    (bc.fiber_100g IS NOT NULL) "
        "    FROM barcode_cache bc "
        "    WHERE bc.ean = p.ean AND bc.status = 'found') >= 4 "
        "  THEN 'medium' ELSE 'low' END, "
        "nutrition_basis = 'migrated_barcode_cache' "
        "WHERE p.ean IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM barcode_cache bc "
        "  WHERE bc.ean = p.ean AND bc.status = 'found' AND ("
        "    bc.kcal_100g IS NOT NULL OR bc.protein_100g IS NOT NULL OR "
        "    bc.carbs_100g IS NOT NULL OR bc.fat_100g IS NOT NULL OR "
        "    bc.fiber_100g IS NOT NULL"
        "  )"
        ")"
    )

    # Recipe totals are derived from ingredient rows. If none of those rows has
    # a macro value, an older recipe-level subtotal is not reproducible and
    # must be shown as missing rather than as precise (often zero) nutrition.
    recipe_columns = _column_names(c, "recipes")
    macro_columns = {"kcal", "protein_g", "carbs_g", "fat_g", "fiber_g"}
    if macro_columns.issubset(recipe_columns):
        c.execute(
            "UPDATE recipes SET kcal = NULL, protein_g = NULL, carbs_g = NULL, "
            "fat_g = NULL, fiber_g = NULL "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM recipe_ingredients ri "
            "  WHERE ri.recipe_id = recipes.id AND ("
            "    ri.kcal IS NOT NULL OR ri.protein_g IS NOT NULL OR "
            "    ri.carbs_g IS NOT NULL OR ri.fat_g IS NOT NULL OR "
            "    ri.fiber_g IS NOT NULL"
            "  )"
            ")"
        )


MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
    7: _migration_7,
}


def _backup_before_migration(c: sqlite3.Connection, old: int, new: int) -> str:
    backup_dir = os.environ.get(
        "DB_BACKUP_DIR",
        os.path.join(os.path.dirname(config.DB_PATH), "backups"),
    )
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(backup_dir, f"data.db.pre-v{old}-to-v{new}.{stamp}")
    destination = sqlite3.connect(path)
    try:
        c.backup(destination)
    finally:
        destination.close()
    os.chmod(path, 0o600)
    log.info("database backup created before migration: %s", path)
    return path


def _apply_migrations(c: sqlite3.Connection) -> None:
    current = int(c.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than application v{SCHEMA_VERSION}"
        )
    if current == SCHEMA_VERSION:
        return
    _backup_before_migration(c, current, SCHEMA_VERSION)
    for version in range(current + 1, SCHEMA_VERSION + 1):
        migration = MIGRATIONS[version]
        c.execute("BEGIN IMMEDIATE")
        try:
            migration(c)
            c.execute(
                "INSERT OR REPLACE INTO migration_history (version, applied_at) "
                "VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            c.execute(f"PRAGMA user_version = {version}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        log.info("database migration applied: v%s", version)


def _secure_database_files() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = config.DB_PATH + suffix
        if os.path.exists(path):
            os.chmod(path, 0o600)


def init() -> None:
    """Create a fresh schema or transactionally migrate an existing one."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    c = _conn()
    has_tables = bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone())
    if has_tables:
        _apply_migrations(c)
    else:
        c.executescript(SCHEMA)
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.execute(
            "INSERT INTO migration_history (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
    # Seed singleton rows for tables that demand id=1
    c.execute("INSERT OR IGNORE INTO user_profile (id) VALUES (1)")
    c.execute("INSERT OR IGNORE INTO preferences (id) VALUES (1)")
    # Seed defaults — only INSERTs that don't already exist, so a user override
    # never gets clobbered on restart.
    now = datetime.now(timezone.utc).isoformat()
    for k, v in KV_DEFAULTS.items():
        c.execute(
            "INSERT OR IGNORE INTO settings_kv (key, value_json, updated_at, is_default) "
            "VALUES (?, ?, ?, 1)",
            (k, json.dumps(v), now),
        )
    _secure_database_files()
    log.info("db init complete: %s (schema v%s)", config.DB_PATH, SCHEMA_VERSION)


def kv_get(key: str):
    row = _conn().execute(
        "SELECT value_json FROM settings_kv WHERE key = ?", (key,)
    ).fetchone()
    if not row:
        return None
    return json.loads(row["value_json"])


def kv_set(key: str, value, *, is_default: bool = False) -> None:
    from datetime import datetime
    _conn().execute(
        "INSERT INTO settings_kv (key, value_json, updated_at, is_default) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "  value_json = excluded.value_json, "
        "  updated_at = excluded.updated_at, "
        "  is_default = excluded.is_default",
        (
            key,
            json.dumps(value, allow_nan=False),
            datetime.now(timezone.utc).isoformat(),
            1 if is_default else 0,
        ),
    )


def kv_reset(key: str) -> bool:
    if key not in KV_DEFAULTS:
        return False
    kv_set(key, KV_DEFAULTS[key], is_default=True)
    return True


def kv_all() -> dict:
    rows = _conn().execute(
        "SELECT key, value_json, is_default FROM settings_kv"
    ).fetchall()
    return {
        r["key"]: {"value": json.loads(r["value_json"]), "is_default": bool(r["is_default"])}
        for r in rows
    }


def get_user_profile() -> dict:
    row = _conn().execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return dict(row) if row else {}


def update_user_profile(**fields) -> None:
    from datetime import datetime
    if not fields:
        return
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    fields["updated_at"] = datetime.utcnow().isoformat()
    cols = cols + ", updated_at = :updated_at"
    _conn().execute(
        f"UPDATE user_profile SET {cols} WHERE id = 1",
        fields,
    )


def get_preferences() -> dict:
    row = _conn().execute("SELECT * FROM preferences WHERE id = 1").fetchone()
    if not row:
        return {}
    out = dict(row)
    for k in ("equipment_json", "dislikes_json", "allergies_json",
              "favorites_json", "supermarkets_json"):
        try:
            out[k.replace("_json", "")] = json.loads(out.pop(k))
        except (json.JSONDecodeError, TypeError):
            out[k.replace("_json", "")] = []
    return out


def update_preferences(**fields) -> None:
    from datetime import datetime
    if not fields:
        return
    # Convert any list/dict values to JSON, mapping bare names → _json columns.
    json_cols = {"equipment", "dislikes", "allergies", "favorites", "supermarkets"}
    db_fields = {}
    for k, v in fields.items():
        if k in json_cols:
            db_fields[f"{k}_json"] = json.dumps(v, allow_nan=False)
        else:
            db_fields[k] = v
    db_fields["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = :{k}" for k in db_fields)
    _conn().execute(
        f"UPDATE preferences SET {cols} WHERE id = 1",
        db_fields,
    )


def setup_completed() -> bool:
    return kv_get("setup_completed_at") is not None


def mark_setup_completed() -> None:
    from datetime import datetime
    kv_set("setup_completed_at", datetime.utcnow().isoformat(), is_default=False)
