"""Tests for the infill sectioning pipeline.

Reference case: 100 x 20 mm rectangle, max area 900 mm^2, max AR 2.
    100x20 (A=2000, AR=5)   -> bisect
    2 x 50x20 (A=1000, AR=2.5) -> both still out of threshold -> bisect
    4 x 25x20 (A=500, AR=1.25) -> OK
so the paper's method must give 4 regions of 25 x 20 mm, angles alternating.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
import trimesh
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from infill_sectioning.footprint import extract_footprint, load_mesh
from infill_sectioning.moments import polygon_moments, raster_moments
from infill_sectioning.sectioning import coverage_error, modifier_footprints, section

import section_infill


def rect(w=100.0, h=20.0):
    return box(0, 0, w, h)


def l_shape():
    return Polygon([(0, 0), (80, 0), (80, 20), (20, 20), (20, 60), (0, 60)])


def plate_with_hole():
    return box(0, 0, 60, 40).difference(Point(30, 20).buffer(8, quad_segs=64))


def wrench_like():
    """Open-end wrench outline: handle + a head with a jaw cut-out."""
    handle = box(0, -6, 110, 6)
    head = Point(125, 0).buffer(18, quad_segs=64)
    jaw = box(125, -6.5, 150, 6.5)
    ring = Point(-5, 0).buffer(10, quad_segs=64).difference(Point(-5, 0).buffer(5, quad_segs=64))
    return unary_union([handle, head, ring]).difference(jaw)


def check_partition(result, max_area, max_ar):
    cov = coverage_error(result)
    assert cov["uncovered_area"] < 1e-6 * cov["part_area"]
    assert cov["overlap_area"] < 1e-6 * cov["part_area"]
    assert math.isclose(cov["sum_region_area"], cov["part_area"], rel_tol=1e-9)
    for r in result.regions:
        assert r.within_thresholds
        assert r.area <= max_area * (1 + 1e-9)
        assert r.aspect_ratio <= max_ar * (1 + 1e-9)


# --------------------------------------------------------------------------- #
# moments
# --------------------------------------------------------------------------- #
def test_rectangle_moments_analytic():
    m = polygon_moments(rect())
    assert m.area == pytest.approx(2000.0)
    assert (m.cx, m.cy) == pytest.approx((50.0, 10.0))
    assert m.mu20 == pytest.approx(20 * 100**3 / 12)
    assert m.mu02 == pytest.approx(100 * 20**3 / 12)
    assert m.aspect_ratio == pytest.approx(5.0)
    assert m.major_axis_deg == pytest.approx(0.0)


@pytest.mark.parametrize("geom", [rect(), l_shape(), plate_with_hole(), wrench_like(),
                                  affinity.rotate(rect(), 30, origin=(0, 0))],
                         ids=["rect", "L", "hole", "wrench", "rect30"])
def test_vector_vs_raster_moments(geom):
    vm, rm = polygon_moments(geom), raster_moments(geom, pixel=0.05)
    assert rm.area == pytest.approx(vm.area, rel=1e-2)
    assert rm.cx == pytest.approx(vm.cx, abs=0.05)
    assert rm.cy == pytest.approx(vm.cy, abs=0.05)
    assert rm.aspect_ratio == pytest.approx(vm.aspect_ratio, rel=1e-2)
    if vm.aspect_ratio > 1.05:  # axis is ill-defined for near-circular shapes
        d = abs(rm.major_axis_deg - vm.major_axis_deg) % 180
        assert min(d, 180 - d) < 0.5


def test_hole_reduces_area():
    m = polygon_moments(plate_with_hole())
    assert m.area == pytest.approx(60 * 40 - math.pi * 64, rel=1e-3)


# --------------------------------------------------------------------------- #
# sectioning
# --------------------------------------------------------------------------- #
def test_reference_rectangle_gives_four_25mm_sections():
    res = section(rect(), max_area=900, max_ar=2.0)
    assert len(res.regions) == 4
    for i, r in enumerate(res.regions):
        minx, miny, maxx, maxy = r.geom.bounds
        assert (minx, maxx) == pytest.approx((25.0 * i, 25.0 * (i + 1)))
        assert (miny, maxy) == pytest.approx((0.0, 20.0))
        assert r.area == pytest.approx(500.0)
        assert r.aspect_ratio == pytest.approx(1.25)
        assert r.depth == 2
    assert [r.angle for r in res.regions] == [45.0, 135.0, 45.0, 135.0]
    assert res.conflicts == []
    # chain adjacency R0-R1-R2-R3
    assert [sorted(r.neighbors) for r in res.regions] == [[1], [0, 2], [1, 3], [2]]
    check_partition(res, 900, 2.0)


def test_rotation_invariance():
    res = section(affinity.rotate(rect(), 37, origin=(10, 5)), 900, 2.0)
    assert len(res.regions) == 4
    for r in res.regions:
        assert r.area == pytest.approx(500.0)
        assert r.aspect_ratio == pytest.approx(1.25)
    assert res.conflicts == []


def test_small_part_not_split():
    res = section(box(0, 0, 25, 20), 900, 2.0)
    assert len(res.regions) == 1


def test_thresholds_are_strict_inequalities():
    # 60x30: AR exactly 2, area 1800 > 900 -> split into 2 x (30x30, A=900) -> stop
    res = section(box(0, 0, 60, 30), 900, 2.0)
    assert len(res.regions) == 2
    assert all(r.area == pytest.approx(900.0) for r in res.regions)


def test_power_of_two_for_long_strip():
    # 100x10: AR 10 -> 50x10 (AR 5) -> 25x10 (AR 2.5) -> 12.5x10 (AR 1.25): 8 regions
    res = section(box(0, 0, 100, 10), 900, 2.0)
    assert len(res.regions) == 8
    assert all(r.aspect_ratio == pytest.approx(1.25) for r in res.regions)


@pytest.mark.parametrize("geom", [l_shape(), plate_with_hole(), wrench_like()],
                         ids=["L", "hole", "wrench"])
def test_generic_shapes_partition(geom):
    res = section(geom, 900, 2.0)
    check_partition(res, 900, 2.0)
    # neighbours sharing an edge get different angles whenever the graph allows
    for r in res.regions:
        for j in r.neighbors:
            if res.regions[j].angle == r.angle:
                assert any({r.id, j} == {a, b} for a, b, _ in res.conflicts)


def test_custom_thresholds():
    res = section(rect(), max_area=10_000, max_ar=6.0)
    assert len(res.regions) == 1
    res = section(rect(), max_area=10_000, max_ar=3.0)
    assert len(res.regions) == 2


def test_nsplit_experimental_gives_three_sections():
    res = section(rect(), 900, 2.0, mode="nsplit")
    assert len(res.regions) == 3
    for r in res.regions:
        minx, _, maxx, _ = r.geom.bounds
        assert maxx - minx == pytest.approx(100 / 3)
        assert r.aspect_ratio == pytest.approx((100 / 3) / 20)
    assert [r.angle for r in res.regions] == [45.0, 135.0, 45.0]
    check_partition(res, 900, 2.0)


# --------------------------------------------------------------------------- #
# modifiers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("geom", [rect(), l_shape(), plate_with_hole(), wrench_like()],
                         ids=["rect", "L", "hole", "wrench"])
def test_modifiers_cover_part_and_do_not_overlap_on_material(geom):
    res = section(geom, 900, 2.0)
    mods = modifier_footprints(res, margin=2.0)
    assert res.part.difference(unary_union(mods)).area < 1e-6 * res.part.area
    for i, mi in enumerate(mods):
        # each modifier contains its region and no material of other regions
        assert res.regions[i].geom.difference(mi).area < 1e-9 * res.part.area
        for j, r in enumerate(res.regions):
            if j != i:
                assert mi.intersection(r.geom).area < 1e-6 * res.part.area


def test_rectangle_modifiers_are_exact_tiles():
    res = section(rect(), 900, 2.0)
    mods = modifier_footprints(res, margin=2.0)
    expected = [(-2, -2, 25, 22), (25, -2, 50, 22), (50, -2, 75, 22), (75, -2, 102, 22)]
    for m, e in zip(mods, expected):
        assert m.bounds == pytest.approx(e)
        assert m.area == pytest.approx((e[2] - e[0]) * (e[3] - e[1]))


# --------------------------------------------------------------------------- #
# end-to-end (mesh -> files)
# --------------------------------------------------------------------------- #
def make_box_stl(path: Path, w=100.0, h=20.0, t=4.0):
    mesh = trimesh.creation.box(extents=[w, h, t])
    mesh.apply_translation([w / 2 + 10, h / 2 + 5, t / 2])
    mesh.export(path)
    return mesh


def test_footprint_methods_agree(tmp_path):
    make_box_stl(tmp_path / "p.stl")
    mesh = load_mesh(tmp_path / "p.stl")
    a = extract_footprint(mesh, "projection")
    b = extract_footprint(mesh, "section")
    assert a.area == pytest.approx(2000.0, rel=1e-6)
    assert a.symmetric_difference(b).area < 1e-3


def test_footprint_of_mesh_with_hole(tmp_path):
    poly = plate_with_hole()
    mesh = trimesh.creation.extrude_polygon(poly, height=3.0)
    mesh.export(tmp_path / "h.stl")
    fp = extract_footprint(load_mesh(tmp_path / "h.stl"))
    assert fp.area == pytest.approx(poly.area, rel=1e-6)
    assert len(fp.interiors) == 1


def test_cli_end_to_end(tmp_path):
    make_box_stl(tmp_path / "provino.stl")
    out = tmp_path / "out"
    rc = section_infill.main([str(tmp_path / "provino.stl"), "--max-area", "900",
                              "--max-ar", "2.0", "--out-dir", str(out)])
    assert rc == 0
    rep = json.loads((out / "provino_sections.json").read_text())
    assert rep["summary"]["n_regions"] == 4
    assert rep["summary"]["all_within_thresholds"]
    assert [r["infill_angle_deg"] for r in rep["regions"]] == [45, 135, 45, 135]
    assert rep["part"]["moments_check"]["ar_rel_diff"] < 1e-2
    assert (out / "provino_sections.png").stat().st_size > 10_000

    part = trimesh.load(tmp_path / "provino.stl")
    stls = sorted(out.glob("provino_modifier_*.stl"))
    assert [p.name for p in stls] == [
        "provino_modifier_00_45deg.stl", "provino_modifier_01_135deg.stl",
        "provino_modifier_02_45deg.stl", "provino_modifier_03_135deg.stl"]
    for i, p in enumerate(stls):
        m = trimesh.load(p)
        assert m.is_watertight
        # same frame as the part; covers it in Z (from the bed to above the top)
        assert m.bounds[0, 2] == pytest.approx(part.bounds[0, 2])
        assert m.bounds[1, 2] == pytest.approx(part.bounds[1, 2] + 1.0)
        # region i spans x in [10 + 25 i, 10 + 25 (i+1)]
        x0 = 10 + 25 * i
        assert m.bounds[0, 0] == pytest.approx(x0 - (2 if i == 0 else 0))
        assert m.bounds[1, 0] == pytest.approx(x0 + 25 + (2 if i == 3 else 0))
        assert (m.bounds[0, 1], m.bounds[1, 1]) == pytest.approx((3.0, 27.0))
        assert m.volume == pytest.approx(
            (m.bounds[1, 0] - m.bounds[0, 0]) * 24.0 * 5.0)
    # the modifiers together contain every vertex of the part
    union = trimesh.util.concatenate([trimesh.load(p) for p in stls])
    pts = part.vertices * 0.999 + part.centroid * 0.001  # nudge inside
    inside = np.zeros(len(pts), dtype=bool)
    for p in stls:
        inside |= trimesh.load(p).contains(pts)
    assert inside.all()
    assert union.bounds[0, 2] <= part.bounds[0, 2]


def test_step_input(tmp_path):
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(100, 20, 4)).Shape()
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(tmp_path / "provino.step"))

    mesh = load_mesh(tmp_path / "provino.step")
    fp = extract_footprint(mesh)
    assert fp.area == pytest.approx(2000.0, rel=1e-6)
    res = section(fp, 900, 2.0)
    assert len(res.regions) == 4
    assert [r.angle for r in res.regions] == [45.0, 135.0, 45.0, 135.0]
