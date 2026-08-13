from __future__ import annotations

import unittest
from unittest import mock

from nutrition import resolve as nutrition_resolve
from pantry.units import (
    canonical_key,
    from_canonical,
    to_canonical,
)


class UnitTests(unittest.TestCase):
    def test_mass_conversion_round_trip(self):
        amount, unit, dimension = to_canonical(1.5, "kg")
        self.assertEqual((amount, unit, dimension), (1500.0, "g", "mass"))
        self.assertEqual(from_canonical(500, "kg"), 0.5)

    def test_unknown_names_never_share_one_key(self):
        self.assertEqual(canonical_key("Red onion"), "name:red-onion")
        self.assertNotEqual(canonical_key("Red onion"), canonical_key("Garlic"))

    def test_resolved_key_is_preserved(self):
        self.assertEqual(
            canonical_key("Chicken thigh", "usda_123"),
            "usda_123",
        )

    def test_unknown_quantity_does_not_claim_per_100g_macros(self):
        row = {
            "key": "usda:rice",
            "source": "usda",
            "kcal_100g": 365,
            "protein_100g": 7,
            "carbs_100g": 80,
            "fat_100g": 1,
            "fiber_100g": 1,
        }
        with mock.patch.object(
            nutrition_resolve,
            "_lookup_name",
            return_value=(row, "exact_name"),
        ):
            result = nutrition_resolve.resolve("a pinch rice")
        self.assertFalse(result["resolved"])
        self.assertIsNone(result["kcal"])


if __name__ == "__main__":
    unittest.main()
