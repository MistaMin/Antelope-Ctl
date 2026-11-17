"""Effect faceplate layouts (webui/static/faceplate/layouts) against the Discrete 4 profile."""
import json
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACEPLATE = ROOT / "webui" / "static" / "faceplate"
KINDS = ("knob", "stepped", "toggle")


def load(path):
    with open(path) as source:
        return json.load(source)


def names(value):
    """Style names from a list, a {name: ...} map, or a list of {"name": ...}."""
    if isinstance(value, dict):
        if value and all(isinstance(item, list) for item in value.values()):  # styles.json: family -> names
            return {name for family in value.values() for name in family}
        return set(value)
    return {item["name"] if isinstance(item, dict) else item for item in value or []}


def style_sets(path):
    """(knob and switch names, plate names) of styles.json or manifest.json, or None."""
    if not path.exists():
        return None
    data = load(path)
    return names(data.get("knobs")) | names(data.get("switches")), names(data.get("plates"))


class FaceplateLayoutTests(unittest.TestCase):
    def test_layouts(self):
        effects = load(ROOT / "profiles" / "discrete_4_a270.json")["afx_inserts"]["parameters"]["effects"]
        catalogues = {name: style_sets(FACEPLATE / name) for name in ("styles.json", "manifest.json")}
        for path in sorted((FACEPLATE / "layouts").glob("*.json")):
            with self.subTest(layout=path.name):
                layout = load(path)
                self.assertEqual(str(layout["type"]), path.stem)
                self.assertIn(path.stem, effects, "not a mapped effect")
                fields = {field["key"]: field for field in effects[path.stem]["fields"]}
                width, height = layout["width"], layout["height"]
                self.assertTrue(width > 0 and height > 0)
                controls = layout["controls"]
                keys = [control["key"] for control in controls]
                self.assertEqual(len(keys), len(set(keys)), "a key is used twice")
                self.assertEqual(set(keys), set(fields), "controls and fields differ")
                for control in controls:
                    key, kind = control["key"], control["kind"]
                    span = fields[key]["max"] - fields[key]["min"]
                    self.assertIn(kind, KINDS, key)
                    if kind == "toggle":
                        self.assertEqual(span, 1, f"{key}: toggle on a field without exactly two values")
                    if kind == "stepped":
                        self.assertLessEqual(span, 24, f"{key}: too many values for stepped")
                    if control.get("steps") is not None:
                        self.assertEqual(len(control["steps"]), span + 1, f"{key}: steps")
                    half = control["size"] / 2
                    self.assertGreater(half, 0, key)
                    self.assertTrue(half <= control["x"] <= width - half
                                    and half <= control["y"] <= height - half, f"{key}: outside the plate")
                for index, first in enumerate(controls):
                    for second in controls[index + 1:]:
                        distance = math.hypot(first["x"] - second["x"], first["y"] - second["y"])
                        self.assertGreaterEqual(distance, (first["size"] + second["size"]) / 2,
                                                f"{first['key']} overlaps {second['key']}")
                for name, sets in catalogues.items():
                    if sets is None:
                        continue
                    self.assertIn(layout["plate"], sets[1], f"plate not in {name}")
                    for control in controls:
                        self.assertIn(control["style"], sets[0], f"{control['key']}: style not in {name}")


if __name__ == "__main__":
    unittest.main()
