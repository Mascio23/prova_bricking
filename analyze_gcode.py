#!/usr/bin/env python3
"""Check the infill direction printed in each region, layer by layer.

Reads a sliced Bambu Studio file (.gcode or .gcode.3mf) and the *_sections.json
written by section_infill.py, and reports, for every region and layer, the
dominant direction of the infill lines. Adjacent regions must differ by 90 deg
(45 vs 135) on every layer for the sectioning to work as intended.

Example:
    python analyze_gcode.py provino.gcode.3mf output/provino_sections.json
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
import zipfile
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep

FEATURES = ("Bottom surface", "Internal solid infill", "Sparse infill", "Top surface", "Bridge")
# "Bridge" layers (the first layer over sparse infill) get their direction from Bambu
# Studio automatically (anchors of the infill below); the setting does not apply there.
AUTOMATIC = {"Bridge"}


def read_gcode(path: Path):
    """Return (lines, plate_bbox) from a .gcode or a .gcode.3mf file."""
    if path.suffix.lower() == ".3mf":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith(".gcode"))
            text = z.read(name).decode("utf-8", "replace")
            plate = None
            if "Metadata/plate_1.json" in z.namelist():
                objs = json.loads(z.read("Metadata/plate_1.json")).get("bbox_objects", [])
                if objs:
                    plate = objs[0]["bbox"]
        return text.splitlines(), plate
    return path.read_text(encoding="utf-8", errors="replace").splitlines(), None


def dominant(angles: collections.Counter, total: float):
    """'45' if one direction dominates, '45+135' if two do, '' if there is no data."""
    keep = [(a, v) for a, v in angles.most_common() if v >= 0.25 * angles.most_common(1)[0][1]]
    return "+".join(str(a) for a, _ in keep)


def analyze(lines, regions, offset):
    polys = [prep(r["geom"]) for r in regions]
    stats = collections.defaultdict(collections.Counter)  # (layer, feat, region) -> angle -> mm
    x = y = 0.0
    feat, layer = None, -1
    for line in lines:
        if line.startswith("; CHANGE_LAYER"):
            layer += 1
        elif line.startswith("; FEATURE:"):
            feat = line[10:].strip()
        elif line[:2] in ("G1", "G0"):
            mx, my, me = (re.search(p + r"(-?[\d.]+)", line) for p in "XYE")
            nx = float(mx.group(1)) if mx else x
            ny = float(my.group(1)) if my else y
            if me and float(me.group(1)) > 0 and feat in FEATURES:
                length = math.hypot(nx - x, ny - y)
                if length > 1.0:
                    mid = Point((x + nx) / 2 - offset[0], (y + ny) / 2 - offset[1])
                    ang = round(math.degrees(math.atan2(ny - y, nx - x)) % 180) % 180
                    for i, pp in enumerate(polys):
                        if pp.contains(mid):
                            stats[(layer, feat, i)][ang] += length
                            break
            x, y = nx, ny
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gcode", type=Path, help=".gcode or .gcode.3mf from Bambu Studio")
    ap.add_argument("sections_json", type=Path, help="*_sections.json from section_infill.py")
    ap.add_argument("--offset", type=float, nargs=2, metavar=("DX", "DY"),
                    help="translation model -> plate [mm]; default: from the object "
                         "bounding box stored in the .gcode.3mf")
    ap.add_argument("--tolerance", type=float, default=5.0,
                    help="allowed deviation from the expected 90 deg difference [deg]")
    args = ap.parse_args(argv)

    rep = json.loads(args.sections_json.read_text())
    regions = [{"id": r["id"], "angle": r["infill_angle_deg"], "neighbors": r["neighbors"],
                "geom": shape({"type": "MultiPolygon", "coordinates": [
                    [p["exterior"]] + p["holes"] for p in r["polygon"]]})}
               for r in rep["regions"]]
    lines, plate = read_gcode(args.gcode)
    if args.offset:
        offset = tuple(args.offset)
    elif plate:
        pb = rep["part"]["bbox"]
        offset = (plate[0] - pb[0], plate[1] - pb[1])
    else:
        sys.exit("error: give --offset DX DY (model -> plate translation)")

    stats = analyze(lines, regions, offset)
    n = len(regions)
    print("region angle set in the project: " + "  ".join(
        f"R{r['id']}={r['angle']:g}" for r in regions))
    print(f"\n{'layer':>5} {'feature':<22}" + "".join(f"{'R' + str(i):>16}" for i in range(n)))
    bad, checked, auto_layers = [], 0, set()
    for L, f in sorted({(k[0], k[1]) for k in stats}):
        row, dom = [], {}
        for i in range(n):
            c = stats.get((L, f, i))
            if c:
                d = dominant(c, sum(c.values()))
                dom[i] = d
                row.append(f"{d} ({sum(c.values()):.0f}mm)")
            else:
                row.append("-")
        print(f"{L:>5} {f:<22}" + "".join(f"{c:>16}" for c in row))
        if f in AUTOMATIC:
            auto_layers.add(L)
            continue
        for r in regions:
            for j in r["neighbors"]:
                if j > r["id"] and r["id"] in dom and j in dom:
                    checked += 1
                    a, b = dom[r["id"]], dom[j]
                    ok = ("+" not in a and "+" not in b and
                          abs(((int(a) - int(b) + 90) % 180) - 90) >= 90 - args.tolerance)
                    if not ok:
                        bad.append((L, f, r["id"], j, a, b))

    print(f"\nadjacent region pairs checked: {checked}  (layers with automatic bridge "
          f"direction, not checked: {sorted(auto_layers) or 'none'})")
    if bad:
        print(f"NOT ORTHOGONAL in {len(bad)} cases, e.g. layer {bad[0][0]} {bad[0][1]}: "
              f"R{bad[0][2]}={bad[0][4]} vs R{bad[0][3]}={bad[0][5]}")
        print("  ('45+135' = both directions on the same layer, e.g. Grid: no alternation)")
        return 1
    print("OK: adjacent regions print orthogonal infill on every checked layer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
