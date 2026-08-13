from __future__ import annotations

import gzip
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from nutrition import bootstrap
from nutrition.bootstrap import download


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class NutritionDownloadTests(unittest.TestCase):
    def test_failed_refresh_retains_last_good_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dataset.zip"
            target.write_bytes(b"last-good")
            with mock.patch(
                "nutrition.bootstrap.urllib.request.urlopen",
                side_effect=urllib.error.URLError("offline"),
            ):
                with self.assertRaises(urllib.error.URLError):
                    download(
                        "https://example.com/dataset.zip",
                        target,
                        refresh=True,
                    )
            self.assertEqual(target.read_bytes(), b"last-good")

    def test_successful_refresh_atomically_replaces_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dataset.zip"
            target.write_bytes(b"last-good")
            with mock.patch(
                "nutrition.bootstrap.urllib.request.urlopen",
                return_value=_Response(b"replacement"),
            ):
                result = download(
                    "https://example.com/dataset.zip",
                    target,
                    refresh=True,
                )
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), b"replacement")
            self.assertFalse(
                (Path(tmp) / "dataset.zip.refresh").exists()
            )

    def test_off_loader_keeps_name_only_products_and_gtin_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "off.jsonl.gz"
            products = (
                [],
                {
                    "code": 12345678,
                    "countries_tags": "en:ireland",
                    "product_name": "Malformed product",
                    "brands": [],
                },
                {
                    "code": "3017620422003",
                    "countries_tags": ["en:ireland"],
                    "product_name_en": "Name only product",
                    "brands": "Example",
                    "nutriments": {},
                },
                {
                    "code": "036000291452",
                    "countries_tags": ["en:germany"],
                    "product_name": "European product",
                    "nutriments": {"energy-kcal_100g": 200},
                },
            )
            with gzip.open(archive, "wt", encoding="utf-8") as output:
                for product in products:
                    output.write(json.dumps(product) + "\n")

            database = root / "nutrition.db"
            with mock.patch.object(
                bootstrap, "DATASET_DIR", root
            ), mock.patch.object(
                bootstrap, "DB_PATH", database
            ), mock.patch.object(
                bootstrap, "download", return_value=archive
            ):
                bootstrap.init_schema()
                bootstrap.load_off(max_rows=None)

            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM ingredients"
                    ).fetchone()[0],
                    2,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT kcal_100g FROM ingredients "
                        "WHERE name LIKE '%Name only product'"
                    ).fetchone()[0]
                )
                aliases = {
                    row[0]
                    for row in connection.execute(
                        "SELECT ean FROM barcodes"
                    )
                }
                self.assertIn("036000291452", aliases)
                self.assertIn("0036000291452", aliases)
                self.assertIn("00036000291452", aliases)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
