import unittest

from assign_active import ensure_colors


class _BatchUpdate:
    def __init__(self, parent, body):
        self.parent = parent
        self.body = body

    def execute(self):
        self.parent.bodies.append(self.body)
        return {}


class _Sheets:
    def __init__(self):
        self.bodies = []

    def batchUpdate(self, spreadsheetId, body):
        return _BatchUpdate(self, body)


class EnsureColorsTests(unittest.TestCase):
    def test_adds_a_color_rule_for_seventh_member(self):
        sheets = _Sheets()
        members = [{"name": name} for name in [
            "Shanshan", "Dan", "Ivan", "Tina", "Jason", "Jen", "Candice",
        ]]

        ensure_colors(sheets, "sheet", 1, members, [])

        rules = sheets.bodies[0]["requests"]
        formulas = [
            request["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            for request in rules
        ]
        self.assertEqual(7, len(rules))
        self.assertIn('=$I2="Candice"', formulas)


if __name__ == "__main__":
    unittest.main()
