"""Build the local nutrition index from USDA and Open Food Facts.

USDA: FoodData Central — full Foundation + SR Legacy CSVs (~50 MB compressed,
  ~600 MB extracted). Free, no API key. Best for raw ingredients.
OFF:  Open Food Facts — European subset filtered from the daily JSONL dump.
  Free, no API key. Best for branded products keyed by GTIN.

Both are normalized into a single SQLite file with two tables:
  ingredients(key, name, category, kcal_100g, protein_100g, carbs_100g, fat_100g, fiber_100g, source)
  barcodes(ean, ingredient_key)

This script is idempotent. Run on first install + monthly via cron.
Run as: python -m nutrition.bootstrap [--force]

The download step is the slow part (~5-10 min depending on bandwidth).
We commit each phase before starting the next so a crash mid-run leaves
a partial-but-queryable DB.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

from barcodes.gtin import BarcodeError, parse as parse_barcode
from barcodes.off import parse_product as parse_off_product

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("king.nutrition.bootstrap")

# --- URLs (these are stable for years; if they change we'll see it in cron logs)
USDA_FOUNDATION_URL = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_csv_2024-04-18.zip"
USDA_SR_LEGACY_URL  = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip"
OFF_IE_JSONL_URL    = "https://static.openfoodfacts.org/data/openfoodfacts-products.jsonl.gz"

OFF_COUNTRY_FILTERS = (
    "ireland",
    "united-kingdom",
    "germany",
    "france",
    "italy",
    "spain",
    "netherlands",
    "belgium",
    "poland",
    "portugal",
    "austria",
    "denmark",
    "sweden",
    "finland",
    "czech",
    "slovakia",
    "romania",
    "greece",
)

DATASET_DIR = Path(os.environ.get("KING_DATASETS", "./datasets"))
DB_PATH     = Path(os.environ.get("NUTRITION_DB", str(DATASET_DIR / "nutrition.db")))


SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,
  kcal_100g REAL,
  protein_100g REAL,
  carbs_100g REAL,
  fat_100g REAL,
  fiber_100g REAL,
  source TEXT NOT NULL CHECK (source IN ('usda', 'off'))
);
CREATE INDEX IF NOT EXISTS idx_ingredients_name ON ingredients(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS barcodes (
  ean TEXT PRIMARY KEY,
  ingredient_key TEXT NOT NULL REFERENCES ingredients(key)
);

CREATE TABLE IF NOT EXISTS bootstrap_state (
  phase TEXT PRIMARY KEY,
  completed_at TEXT NOT NULL,
  rows_added INTEGER
);
"""


# ---------- helpers ---------------------------------------------------------

@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    try:
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        yield c
    finally:
        c.close()


def init_schema():
    with conn() as c:
        c.executescript(SCHEMA)
        c.commit()


def phase_done(phase: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT 1 FROM bootstrap_state WHERE phase = ?", (phase,)).fetchone()
        return r is not None


def mark_phase(phase: str, rows: int):
    from datetime import datetime
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO bootstrap_state (phase, completed_at, rows_added) "
            "VALUES (?, ?, ?)",
            (phase, datetime.utcnow().isoformat(), rows),
        )
        c.commit()


def slugify(name: str) -> str:
    """Simple ingredient_key generator: lowercased, non-alnum → underscore."""
    import re
    s = re.sub(r"[^\w]+", "_", name.lower()).strip("_")
    return s[:80]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path, *, refresh: bool = False) -> Path:
    """Resumable stream-download via .partial.

    On restart, sends Range: bytes=<existing>- so we don't re-fetch what's
    already on disk. Required for the OFF dump (~12 GB) where any flake
    mid-stream would otherwise force a full restart. Skips if the final
    file is already present.
    """
    if target.exists() and target.stat().st_size > 0 and not refresh:
        log.info("skip download (exists): %s", target.name)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    final_target = target
    download_target = (
        target.with_name(target.name + ".refresh") if refresh else target
    )
    if download_target.exists():
        download_target.unlink()
    partial = download_target.with_suffix(download_target.suffix + ".partial")

    headers: dict[str, str] = {}
    mode = "wb"
    start = 0
    if partial.exists():
        start = partial.stat().st_size
        if start > 0:
            headers["Range"] = f"bytes={start}-"
            mode = "ab"
            log.info("resuming %s from byte %d", target.name, start)

    log.info("downloading %s -> %s", url, download_target.name)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(partial, mode) as f:
            # Server may ignore the Range header (200 instead of 206) — in
            # that case we got the full file, so restart from scratch.
            if r.status == 200 and start > 0:
                log.warning("server ignored Range; restarting download from 0")
                f.close()
                partial.unlink()
                with open(partial, "wb") as f2, urllib.request.urlopen(url, timeout=120) as r2:
                    shutil.copyfileobj(r2, f2, length=1 << 20)
            else:
                shutil.copyfileobj(r, f, length=1 << 20)
    except urllib.error.HTTPError as e:
        # 416 Range Not Satisfiable = we already have the whole file
        if e.code == 416 and partial.exists() and partial.stat().st_size > 0:
            log.info("server says we already have it all (416)")
        else:
            raise
    partial.rename(download_target)
    log.info(
        "download complete: %s bytes=%d sha256=%s",
        download_target.name,
        download_target.stat().st_size,
        _sha256(download_target),
    )
    if refresh:
        os.replace(download_target, final_target)
    return final_target


# ---------- USDA loader -----------------------------------------------------

def load_usda(*, refresh_downloads: bool = False):
    """Downloads + parses USDA Foundation + SR Legacy ZIPs.

    Each archive contains food.csv + nutrient.csv + food_nutrient.csv. We need
    name (food.csv), the kcal/protein/fat/carbs/fiber rows from food_nutrient
    keyed against nutrient.csv. Filters to foods with nutritional density.
    """
    if phase_done("usda"):
        log.info("usda phase already done; skipping")
        return

    rows_added = 0
    for label, url in [("foundation", USDA_FOUNDATION_URL), ("sr_legacy", USDA_SR_LEGACY_URL)]:
        zip_path = DATASET_DIR / f"usda_{label}.zip"
        download(url, zip_path, refresh=refresh_downloads)

        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(zip_path) as z:
                bad = z.testzip()
                if bad:
                    raise RuntimeError(f"corrupt USDA archive member: {bad}")
                z.extractall(tmp)
            # The CSVs sit one directory deep, name varies by release.
            csv_root = next((p for p in Path(tmp).rglob("food.csv")), None)
            if csv_root is None:
                log.warning("usda %s: no food.csv found in zip; skipping", label)
                continue
            base = csv_root.parent

            # Build lookup: nutrient_id -> nutrient_name (we want kcal, protein, fat, carbs, fiber)
            target_nutrients = {
                "Energy": "kcal",                           # kcal/100g
                "Protein": "protein",
                "Total lipid (fat)": "fat",
                "Carbohydrate, by difference": "carbs",
                "Fiber, total dietary": "fiber",
            }
            nutrient_id_map: dict[str, str] = {}
            with open(base / "nutrient.csv") as f:
                for row in csv.DictReader(f):
                    name = (row.get("name") or "").strip()
                    if name in target_nutrients:
                        # Energy appears in both kJ and kcal; keep kcal only.
                        if name == "Energy" and (row.get("unit_name") or "").upper() != "KCAL":
                            continue
                        nutrient_id_map[row["id"]] = target_nutrients[name]

            # Build food id -> name from food.csv
            food_names: dict[str, str] = {}
            with open(base / "food.csv") as f:
                for row in csv.DictReader(f):
                    fid = row.get("fdc_id") or row.get("food_id")
                    name = (row.get("description") or "").strip()
                    if fid and name:
                        food_names[fid] = name

            # Walk food_nutrient.csv accumulating per-food macros
            food_macros: dict[str, dict[str, float]] = {}
            with open(base / "food_nutrient.csv") as f:
                for row in csv.DictReader(f):
                    fid = row.get("fdc_id") or row.get("food_id")
                    nid = row.get("nutrient_id")
                    if not fid or nid not in nutrient_id_map:
                        continue
                    try:
                        amount = float(row.get("amount") or 0)
                    except ValueError:
                        continue
                    food_macros.setdefault(fid, {})[nutrient_id_map[nid]] = amount

            # Insert: only foods with at least kcal+protein
            with conn() as c:
                for fid, macros in food_macros.items():
                    name = food_names.get(fid)
                    if not name or "kcal" not in macros or "protein" not in macros:
                        continue
                    key = slugify(name)
                    if not key:
                        continue
                    try:
                        c.execute(
                            "INSERT OR IGNORE INTO ingredients "
                            "(key, name, category, kcal_100g, protein_100g, carbs_100g, fat_100g, fiber_100g, source) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'usda')",
                            (key, name, label, macros.get("kcal"),
                             macros.get("protein"), macros.get("carbs"),
                             macros.get("fat"), macros.get("fiber")),
                        )
                        rows_added += 1
                    except sqlite3.IntegrityError:
                        pass
                c.commit()

            log.info("usda %s: %d foods parsed", label, len(food_macros))

    mark_phase("usda", rows_added)
    log.info("usda phase complete: %d ingredients added", rows_added)


# ---------- OFF loader ------------------------------------------------------

def load_off(country_filters: tuple[str, ...] = OFF_COUNTRY_FILTERS,
             brand_keepers: tuple[str, ...] = ("tesco", "lidl", "milbona",
                                                "aldi", "dunnes", "supervalu",
                                                "marks-spencer", "waitrose",
                                                "sainsbury", "asda", "morrison"),
             max_rows: int | None = 800_000,
             refresh_downloads: bool = False):
    """Streams the OFF JSONL.gz dump, indexes by EAN.

    Coverage includes Ireland, the UK, common European origins, and major
    Irish supermarket brands. Products with a usable name are indexed even
    when OFF has no nutrition values; recognition must not depend on complete
    macros.

    max_rows raised from 200k → 800k to actually capture meaningful coverage.
    """
    if phase_done("off"):
        log.info("off phase already done; skipping")
        return

    gz_path = DATASET_DIR / "off.jsonl.gz"
    download(OFF_IE_JSONL_URL, gz_path, refresh=refresh_downloads)

    rows_added = 0
    with gzip.open(gz_path, "rt", encoding="utf-8") as f, conn() as c:
        for i, line in enumerate(f):
            if max_rows and rows_added >= max_rows:
                break
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(p, dict):
                continue
            country_values = p.get("countries_tags")
            countries = [
                tag.lower()
                for tag in (
                    country_values if isinstance(country_values, list) else []
                )
                if isinstance(tag, str)
            ]
            raw_brands = p.get("brands")
            brands = raw_brands.lower() if isinstance(raw_brands, str) else ""
            country_match = any(any(cf in t for t in countries) for cf in country_filters)
            brand_match = any(b in brands for b in brand_keepers)
            if not (country_match or brand_match):
                continue
            ean = p.get("code")
            if not isinstance(ean, str):
                continue
            ean = ean.strip()
            if not ean:
                continue
            try:
                barcode = parse_barcode(ean)
            except BarcodeError:
                continue
            product = parse_off_product(p, barcode)
            if not product:
                continue
            key = product["ingredient_key"]
            try:
                c.execute(
                    "INSERT OR IGNORE INTO ingredients "
                    "(key, name, category, kcal_100g, protein_100g, carbs_100g, fat_100g, fiber_100g, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'off')",
                    (
                        key,
                        product["display_name"],
                        "off",
                        product.get("kcal_100g"),
                        product.get("protein_100g"),
                        product.get("carbs_100g"),
                        product.get("fat_100g"),
                        product.get("fiber_100g"),
                    ),
                )
                for alias in barcode.aliases:
                    c.execute(
                        "INSERT OR IGNORE INTO barcodes "
                        "(ean, ingredient_key) VALUES (?, ?)",
                        (alias, key),
                    )
                rows_added += 1
            except sqlite3.IntegrityError:
                pass
            if i % 50_000 == 0:
                c.commit()
                log.info("off scanning: %d rows seen, %d added", i, rows_added)
        c.commit()

    mark_phase("off", rows_added)
    log.info("off phase complete: %d ingredients added", rows_added)


# ---------- entry point -----------------------------------------------------

def _validate_database(path: Path, *, expect_off: bool) -> tuple[int, int]:
    c = sqlite3.connect(str(path))
    try:
        quick = c.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise RuntimeError(f"nutrition quick_check failed: {quick}")
        if c.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("nutrition foreign-key check failed")
        ingredients = c.execute("SELECT COUNT(*) FROM ingredients").fetchone()[0]
        barcodes = c.execute("SELECT COUNT(*) FROM barcodes").fetchone()[0]
        if ingredients < 10_000:
            raise RuntimeError(
                f"nutrition ingredient count too low: {ingredients}"
            )
        if expect_off and barcodes < 1_000:
            raise RuntimeError(f"nutrition barcode count too low: {barcodes}")
        return ingredients, barcodes
    finally:
        c.close()


def main():
    global DB_PATH
    p = argparse.ArgumentParser()
    p.add_argument(
        "--force", action="store_true",
        help="build a fresh database (retained for wrapper compatibility)",
    )
    p.add_argument("--phase", choices=["usda", "off"], help="run a single phase")
    p.add_argument(
        "--refresh-downloads", action="store_true",
        help="download source archives again instead of reusing local copies",
    )
    args = p.parse_args()

    target = DB_PATH
    build = target.with_name(target.name + ".build")
    for candidate in (build, Path(str(build) + "-wal"), Path(str(build) + "-shm")):
        candidate.unlink(missing_ok=True)
    DB_PATH = build
    try:
        init_schema()
        if args.phase in (None, "usda"):
            load_usda(refresh_downloads=args.refresh_downloads)
        if args.phase in (None, "off"):
            load_off(refresh_downloads=args.refresh_downloads)
        with conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        n_ing, n_ean = _validate_database(
            build, expect_off=args.phase in (None, "off")
        )
        os.chmod(build, 0o600)
        os.replace(build, target)
        log.info(
            "nutrition database swapped atomically: %d ingredients, %d barcodes",
            n_ing,
            n_ean,
        )
    except Exception:
        log.exception("nutrition refresh failed; live database was not changed")
        raise
    finally:
        DB_PATH = target
        for candidate in (
            build,
            Path(str(build) + "-wal"),
            Path(str(build) + "-shm"),
        ):
            candidate.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
