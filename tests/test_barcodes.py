from __future__ import annotations

import unittest

from barcodes.gtin import BarcodeError, parse, valid_check_digit
from barcodes.off import parse_product


class GtinTests(unittest.TestCase):
    def test_upc_ean_and_gtin_aliases_share_one_canonical_key(self):
        upc = parse("036000291452")
        ean = parse("0 03600-029145 2")

        self.assertEqual(upc.canonical, "00036000291452")
        self.assertEqual(ean.canonical, upc.canonical)
        self.assertIn("036000291452", ean.aliases)
        self.assertIn("0036000291452", upc.aliases)
        self.assertIn("00036000291452", upc.aliases)
        self.assertTrue(valid_check_digit("036000291452"))

    def test_invalid_check_digit_is_rejected(self):
        with self.assertRaisesRegex(BarcodeError, "check digit"):
            parse("036000291453")

    def test_non_gtin_legacy_code_keeps_exact_representation(self):
        barcode = parse("123456")
        self.assertFalse(barcode.is_gtin)
        self.assertEqual(barcode.canonical, "123456")
        self.assertEqual(barcode.aliases, ("123456",))


class OpenFoodFactsProductTests(unittest.TestCase):
    def test_name_only_product_is_retained_with_package_quantity(self):
        product = parse_product(
            {
                "code": "3017620422003",
                "product_name_en": "Hazelnut spread",
                "brands": "Example Brand, Parent Company",
                "quantity": "2 x 250 g",
                "nutriments": {},
            },
            parse("3017620422003"),
        )

        self.assertIsNotNone(product)
        self.assertEqual(
            product["display_name"], "Example Brand Hazelnut spread"
        )
        self.assertEqual((product["quantity"], product["unit"]), (500, "g"))
        self.assertFalse(product["nutrition_available"])
        self.assertIsNone(product["kcal_100g"])

    def test_energy_kilojoules_and_macros_are_normalized(self):
        product = parse_product(
            {
                "product_name": "Test food",
                "nutriments": {
                    "energy-kj_100g": 418.4,
                    "proteins_100g": "4.5",
                    "carbohydrates_100g": 12,
                    "fat_100g": 3,
                },
            },
            parse("3017620422003"),
        )

        self.assertAlmostEqual(product["kcal_100g"], 100)
        self.assertEqual(product["protein_100g"], 4.5)
        self.assertTrue(product["nutrition_available"])


if __name__ == "__main__":
    unittest.main()
