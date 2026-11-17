#!/usr/bin/env python3
"""Write webui/static/faceplate/preview-data.json for faceplate/preview.html.

For every mapped Discrete 4 effect: its label and its fields in the shape the
server's /api/afx-inserts/parameters returns, each value at mid-range.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "discrete_4_a270.json"
OUT = ROOT / "webui" / "static" / "faceplate" / "preview-data.json"


def preview_data(profile):
    inserts = profile["afx_inserts"]
    data = {}
    for type_id, effect in inserts["parameters"]["effects"].items():
        data[type_id] = {
            "type": int(type_id),
            "label": inserts["type_labels"].get(type_id, {}).get("label", f"type {type_id}"),
            "fields": [{"key": f["key"], "label": f["label"], "min": f["min"], "max": f["max"],
                        "scale": f.get("scale", 1), "unit": f.get("unit", ""),
                        "value": (f["min"] + f["max"]) // 2} for f in effect["fields"]],
        }
    return data


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(preview_data(json.loads(PROFILE.read_text())), indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
