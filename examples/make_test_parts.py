#!/usr/bin/env python3
"""Generate a few test parts (STL) to try section_infill.py on.

    python examples/make_test_parts.py            # writes into examples/parts/
"""

from pathlib import Path

import trimesh
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

OUT = Path(__file__).with_name("parts")


def extrude(poly, h):
    return trimesh.creation.extrude_polygon(poly, height=h)


def main():
    OUT.mkdir(exist_ok=True)
    parts = {
        # reference specimen of the thesis: 100 x 20 x 4 mm
        "provino_100x20x4": extrude(box(0, 0, 100, 20), 4.0),
        "L_80x60x4": extrude(Polygon([(0, 0), (80, 0), (80, 20), (20, 20),
                                      (20, 60), (0, 60)]), 4.0),
        "plate_hole_60x40x3": extrude(box(0, 0, 60, 40).difference(
            Point(30, 20).buffer(8, quad_segs=64)), 3.0),
        "wrench_5mm": extrude(unary_union([
            box(0, -6, 110, 6),
            Point(125, 0).buffer(18, quad_segs=64),
            Point(-5, 0).buffer(10, quad_segs=64).difference(Point(-5, 0).buffer(5, quad_segs=64)),
        ]).difference(box(125, -6.5, 150, 6.5)), 5.0),
    }
    for name, mesh in parts.items():
        path = OUT / f"{name}.stl"
        mesh.export(path)
        print("wrote", path)


if __name__ == "__main__":
    main()
