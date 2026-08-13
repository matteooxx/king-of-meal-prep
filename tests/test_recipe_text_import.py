from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recipes import text_import


class TextImportTests(unittest.TestCase):
    def test_sectioned_recipe(self):
        parsed = text_import.parse(
            """Lemon chicken
Serves 4
Total time: 35 minutes

Ingredients
- 600 g chicken breast
- 2 lemons

Method
1. Heat the oven.
2. Roast until cooked.
"""
        )
        self.assertEqual(parsed["name"], "Lemon chicken")
        self.assertEqual(parsed["servings"], 4)
        self.assertEqual(parsed["total_time_min"], 35)
        self.assertEqual(
            parsed["ingredients"],
            ["600 g chicken breast", "2 lemons"],
        )
        self.assertEqual(
            parsed["steps"],
            ["Heat the oven.", "Roast until cooked."],
        )

    def test_requires_ingredients(self):
        with self.assertRaisesRegex(ValueError, "Ingredients"):
            text_import.parse("A title\n\nJust some prose")


if __name__ == "__main__":
    unittest.main()
