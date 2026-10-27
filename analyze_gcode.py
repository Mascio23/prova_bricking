#!/usr/bin/env python3
"""Check the infill direction printed in each region, layer by layer.

Reads a sliced Bambu Studio file (.gcode or .gcode.3mf) and the *_sections.json
written by section_infill.py, and reports, for every region and layer, the
dominant direction of the infill lines. Two things are checked:

1. adjacent regions print infill at the configured angle difference on every layer
   (90 deg for 45 vs 135; less where a third angle was needed, see --extra-angle);
2. the direction relative to the part is the configured one (45 or 135, or the
   same plus 90 on the alternate layers), whatever the rotation of the part on
   the plate (the infill direction in Bambu Studio refers to the plate).

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

import numpy as np
from shapely import affinity
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


def circ_dist(a: float, b: float) -> float:
    """Distance between two directions (mod 180 deg)."""
    d = (a - b) % 180.0
    return min(d, 180.0 - d)


def clusters(segments, width: float = 6.0, min_share: float = 0.25):
    """Dominant line directions from [(angle_deg mod 180, length_mm)].

    Returns [(centre_deg, length_mm)], longest first. A second direction is
    reported only if it carries at least `min_share` of the first one (e.g. both
    diagonals of Grid); shorter ones (connections between lines) are ignored."""
    if not segments:
        return []
    ang = np.array([a for a, _ in segments])
    ln = np.array([w for _, w in segments])
    out = []
    while len(ang):
        hist = np.zeros(180)
        np.add.at(hist, np.round(ang).astype(int) % 180, ln)
        smooth = sum(np.roll(hist, k) for k in range(-3, 4))
        peak = float(np.argmax(smooth))
        near = np.array([circ_dist(a, peak) <= width for a in ang])
        w = ln[near]
        if not len(w) or (out and w.sum() < min_share * out[0][1]):
            break
        z = np.sum(w * np.exp(2j * np.radians(ang[near])))
        out.append((float(np.degrees(np.angle(z)) / 2 % 180), float(w.sum())))
        ang, ln = ang[~near], ln[~near]
    return out


def fmt(cl) -> str:
    return "+".join(f"{round(c, 1):g}" for c, _ in cl)


def analyze(lines, regions):
    polys = [prep(r["geom"]) for r in regions]
    segs = collections.defaultdict(list)  # (layer, feat, region) -> [(angle, mm)]
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
                    mid = Point((x + nx) / 2, (y + ny) / 2)
                    ang = math.degrees(math.atan2(ny - y, nx - x)) % 180
                    for i, pp in enumerate(polys):
                        if pp.contains(mid):
                            segs[(layer, feat, i)].append((ang, length))
                            break
            x, y = nx, ny
    return segs


def to_plate(geom, placement, offset, rotation, part_bbox, plate_bbox):
    """Move a region polygon from the model frame to the plate frame."""
    if offset is not None:
        g = affinity.rotate(geom, rotation, origin=(0, 0)) if rotation else geom
        return affinity.translate(g, *offset)
    if placement:
        ox, oy = placement["object_origin_xy"]
        tx, ty = placement["plate_position_xy"]
        g = affinity.translate(geom, -ox, -oy)
        g = affinity.rotate(g, placement["plate_rotation_deg"], origin=(0, 0))
        return affinity.translate(g, tx, ty)
    # no recorded placement: rotate about the centre of the part's bounding box and
    # put the centre of the (rotated) part on the centre of the plate bounding box
    cx, cy = (part_bbox[0] + part_bbox[2]) / 2, (part_bbox[1] + part_bbox[3]) / 2
    g = affinity.rotate(geom, rotation, origin=(cx, cy))
    px, py = (plate_bbox[0] + plate_bbox[2]) / 2, (plate_bbox[1] + plate_bbox[3]) / 2
    return affinity.translate(g, px - cx, py - cy)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gcode", type=Path, help=".gcode or .gcode.3mf from Bambu Studio")
    ap.add_argument("sections_json", type=Path, help="*_sections.json from section_infill.py")
    ap.add_argument("--offset", type=float, nargs=2, metavar=("DX", "DY"),
                    help="translation model -> plate [mm] (applied after --rotation about "
                         "the model origin); default: from the placement recorded in the JSON")
    ap.add_argument("--rotation", type=float, default=None, metavar="DEG",
                    help="rotation of the part on the plate (counter-clockwise, deg); default: "
                         "the one recorded in the JSON, else 0. For a part rotated by hand "
                         "in Bambu Studio: assumes rotation about the centre of a part "
                         "that is symmetric (e.g. a rectangle)")
    ap.add_argument("--tolerance", type=float, default=5.0,
                    help="allowed deviation of the line direction [deg]")
    args = ap.parse_args(argv)

    rep = json.loads(args.sections_json.read_text())
    placement = (rep.get("bambu_3mf") or {}).get("placement")
    rotation = args.rotation if args.rotation is not None else (
        placement["plate_rotation_deg"] if placement else 0.0)
    lines, plate_bbox = read_gcode(args.gcode)
    if args.rotation is not None or args.offset is not None:
        placement = None  # explicit values given: do not mix them with the recorded ones
    if args.offset is None and placement is None and plate_bbox is None:
        sys.exit("error: no placement in the JSON and no bounding box in the file: "
                 "give --offset DX DY (and --rotation)")
    part_bbox = rep["part"]["bbox"]

    regions = []
    for r in rep["regions"]:
        g = shape({"type": "MultiPolygon", "coordinates": [
            [p["exterior"]] + p["holes"] for p in r["polygon"]]})
        regions.append({"id": r["id"], "angle": r["infill_angle_deg"],
                        "neighbors": r["neighbors"],
                        "geom": to_plate(g, placement, args.offset, rotation, part_bbox,
                                         plate_bbox)})
    segs = analyze(lines, regions)

    n = len(regions)
    print("part rotation on the plate: "
          f"{rotation:g} deg;  angles relative to the part: " + "  ".join(
              f"R{r['id']}={r['angle']:g}" for r in regions))
    print(f"\n{'layer':>5} {'feature':<22}" + "".join(f"{'R' + str(i):>18}" for i in range(n)))
    angle_of = {r["id"]: r["angle"] for r in regions}
    ortho_bad, rel_bad, checked, pairs = [], [], 0, 0
    auto_layers = set()
    for L, f in sorted({(k[0], k[1]) for k in segs}):
        cells, dom = [], {}
        for i in range(n):
            s = segs.get((L, f, i))
            if s:
                cl = clusters(s)
                dom[i] = cl
                cells.append(f"{fmt(cl)} ({sum(w for _, w in s):.0f}mm)")
            else:
                cells.append("-")
        print(f"{L:>5} {f:<22}" + "".join(f"{c:>18}" for c in cells))
        if f in AUTOMATIC:
            auto_layers.add(L)
            continue
        for r in regions:
            cl = dom.get(r["id"])
            if cl:
                checked += 1
                ok = len(cl) == 1 and any(
                    circ_dist(cl[0][0] - rotation, r["angle"] + k) <= args.tolerance
                    for k in (0, 90))
                if not ok:
                    rel_bad.append((L, f, r["id"], fmt(cl)))
            for j in r["neighbors"]:
                if j > r["id"] and cl and dom.get(j):
                    pairs += 1
                    a, b = dom[r["id"]], dom[j]
                    cfg = (angle_of[r["id"]] - angle_of[j]) % 180
                    if not (len(a) == 1 and len(b) == 1 and
                            circ_dist(a[0][0] - b[0][0], cfg) <= args.tolerance):
                        ortho_bad.append((L, f, r["id"], j, fmt(a), fmt(b), cfg))

    print(f"\nlayers with automatic bridge direction, not checked: {sorted(auto_layers) or 'none'}")
    print(f"adjacent region pairs checked: {pairs};  region-layers checked against the "
          f"configured angle: {checked}")
    if ortho_bad:
        e = ortho_bad[0]
        kind = "NOT ORTHOGONAL" if circ_dist(e[6], 90) < 1e-6 else \
            f"WRONG ANGLE BETWEEN NEIGHBOURS (configured difference {e[6]:g})"
        print(f"{kind} in {len(ortho_bad)} cases, e.g. layer {e[0]} {e[1]}: "
              f"R{e[2]}={e[4]} vs R{e[3]}={e[5]}")
        print("  ('a+b' = two directions on the same layer, e.g. Grid: no alternation)")
    if rel_bad:
        e = rel_bad[0]
        print(f"WRONG DIRECTION RELATIVE TO THE PART in {len(rel_bad)} cases, e.g. layer "
              f"{e[0]} {e[1]} R{e[2]}: printed {e[3]}, expected "
              f"{(next(r['angle'] for r in regions if r['id'] == e[2]) + rotation) % 180:g} or "
              f"{(next(r['angle'] for r in regions if r['id'] == e[2]) + rotation + 90) % 180:g} "
              "on the plate")
    if ortho_bad or rel_bad:
        return 1
    print("OK: adjacent regions print the configured angle difference, at the configured angle "
          "relative to the part, on every checked layer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
