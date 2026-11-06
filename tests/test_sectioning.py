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


# --------------------------------------------------------------------------- #
# optional third angle (only where two angles are not enough)
# --------------------------------------------------------------------------- #
def t_shape():
    return unary_union([box(0, 0, 120, 25), box(45, 25, 70, 80)])


def neighbours_share_angle(res):
    return [(r.id, j) for r in res.regions for j in r.neighbors
            if j > r.id and res.regions[j].angle == r.angle]


def test_two_angles_are_not_always_enough():
    res = section(t_shape(), 900, 2.0)  # default: no third angle
    assert res.conflicts  # T-junctions -> odd cycles
    assert not any(r.extra for r in res.regions)
    assert {r.angle for r in res.regions} <= {45.0, 135.0}


def test_extra_angle_resolves_conflicts_on_the_fewest_regions():
    import itertools

    res = section(t_shape(), 900, 2.0, extra_angle=90.0)
    assert res.conflicts == [] and neighbours_share_angle(res) == []
    extra = [r.id for r in res.regions if r.extra]
    assert extra and all(res.regions[i].angle == 90.0 for i in extra)
    assert all(r.angle in (45.0, 135.0) for r in res.regions if not r.extra)
    # minimality (brute force): no smaller independent set S leaves a bipartite graph
    n = len(res.regions)
    adj = [set(r.neighbors) for r in res.regions]

    def feasible(S):
        if any(b in adj[a] for a, b in itertools.combinations(S, 2)):
            return False
        col = {}
        for s0 in range(n):
            if s0 in S or s0 in col:
                continue
            col[s0], stack = 0, [s0]
            while stack:
                i = stack.pop()
                for j in adj[i] - set(S):
                    if j not in col:
                        col[j] = 1 - col[i]
                        stack.append(j)
                    elif col[j] == col[i]:
                        return False
        return True

    assert feasible(set(extra))
    assert not any(feasible(set(S)) for k in range(len(extra))
                   for S in itertools.combinations(range(n), k))
    check_partition(res, 900, 2.0)


@pytest.mark.parametrize("geom", [rect(), l_shape(), plate_with_hole()],
                         ids=["rect", "L", "hole"])
def test_extra_angle_not_used_when_two_angles_suffice(geom):
    plain = section(geom, 900, 2.0)
    assert plain.conflicts == []
    with_extra = section(geom, 900, 2.0, extra_angle=90.0)
    assert not any(r.extra for r in with_extra.regions)
    assert [r.angle for r in with_extra.regions] == [r.angle for r in plain.regions]


def test_extra_angle_must_differ_from_the_main_angles():
    for bad in (45.0, 135.0, 225.0, -45.0):
        with pytest.raises(ValueError, match="extra angle"):
            section(rect(), 900, 2.0, extra_angle=bad)


def test_conflicts_remain_if_three_angles_are_not_enough():
    tri = Polygon([(0, 0), (160, 0), (60, 90)])
    res = section(tri, 900, 2.0, extra_angle=90.0)
    assert res.conflicts == [] or neighbours_share_angle(res)  # reported, never hidden
    assert bool(res.conflicts) == bool(neighbours_share_angle(res))


def test_cli_extra_angle_end_to_end(tmp_path):
    mesh = trimesh.creation.extrude_polygon(t_shape(), height=4.0)
    mesh.export(tmp_path / "t.stl")
    out = tmp_path / "o"
    section_infill.main([str(tmp_path / "t.stl"), "--out-dir", str(out), "--extra-angle", "90",
                         "--export-3mf", "--plate-rotation", "20"])
    rep = json.loads((out / "t_sections.json").read_text())
    extra = rep["summary"]["extra_angle_regions"]
    assert extra and rep["summary"]["adjacency_conflicts"] == []
    assert {r["infill_angle_deg"] for r in rep["regions"] if r["extra_angle"]} == {90.0}
    assert {r["plate_angle_deg"] for r in rep["regions"] if r["extra_angle"]} == {110.0}
    assert (out / "t_sections.png").stat().st_size > 10_000


# --------------------------------------------------------------------------- #
# nsplit on a shape it cannot handle; crowded PNG
# --------------------------------------------------------------------------- #
def frame_with_windows():
    """250 x 112 mm plate with three rectangular windows: long bars parallel to the cuts."""
    g = box(0, 0, 250, 112).buffer(-6).buffer(6, quad_segs=16)
    for w in (box(19, 18.5, 146, 93), box(164, 18.5, 183, 93), box(197, 18.5, 216, 93)):
        g = g.difference(w)
    return g


def test_nsplit_fails_loudly_where_slabs_cannot_meet_the_thresholds():
    with pytest.raises(ValueError, match="nsplit cannot meet the thresholds") as exc:
        section(frame_with_windows(), 900, 2.0, mode="nsplit")
    assert "--mode bisect" in str(exc.value)
    # the paper's method does handle the same shape
    res = section(frame_with_windows(), 900, 2.0, mode="bisect")
    check_partition_area(res)


def check_partition_area(res):
    cov = coverage_error(res)
    assert cov["uncovered_area"] < 1e-6 * cov["part_area"]
    assert cov["overlap_area"] < 1e-6 * cov["part_area"]


def test_cli_nsplit_error_is_clean_and_writes_nothing(tmp_path):
    mesh = trimesh.creation.extrude_polygon(frame_with_windows(), height=4.0)
    mesh.export(tmp_path / "frame.stl")
    with pytest.raises(SystemExit, match="error: mode nsplit cannot meet"):
        section_infill.main([str(tmp_path / "frame.stl"), "--mode", "nsplit",
                             "--out-dir", str(tmp_path / "o")])
    assert not (tmp_path / "o").exists()


def test_png_with_many_regions_is_written(tmp_path):
    mesh = trimesh.creation.extrude_polygon(frame_with_windows(), height=4.0)
    mesh.export(tmp_path / "frame.stl")
    out = tmp_path / "o"
    section_infill.main([str(tmp_path / "frame.stl"), "--out-dir", str(out), "--png-dpi", "80"])
    rep = json.loads((out / "frame_sections.json").read_text())
    assert rep["summary"]["n_regions"] > 16
    assert (out / "frame_sections.png").stat().st_size > 10_000


# --------------------------------------------------------------------------- #
# slivers: thin strips left between a cut and an edge
# --------------------------------------------------------------------------- #
from infill_sectioning.sectioning import thickness  # noqa: E402


def plate_with_windows_close_to_a_cut():
    """266 x 120 mm plate with three windows; the third has its edge at 77.5, so a slanted
    cut leaves a wedge ~1.7 mm wide between the cut and the window (the plate of a real
    test: it used to give 58 regions, 24 of them ~1.5 mm2 slivers)."""
    g = box(-133, -60, 133, 60).buffer(-8).buffer(8, quad_segs=16)
    for w in (box(-112, -40, 22, 40), box(43, -40, 63, 40), box(77.5, -40, 97, 40)):
        g = g.difference(w)
    return g


def test_without_the_width_limit_a_sliver_is_split_into_dozens_of_regions():
    res = section(plate_with_windows_close_to_a_cut(), 900, 2.0, min_width=0.0)
    assert len(res.regions) >= 50
    assert sum(1 for r in res.regions if thickness(r.geom) < 1.0) >= 20


def test_slivers_are_merged_into_the_neighbours():
    part = plate_with_windows_close_to_a_cut()
    res = section(part, 900, 2.0)  # default min_width = 2 mm
    assert len(res.regions) < 40
    assert min(thickness(r.geom) for r in res.regions) >= 2.0
    assert all(r.within_thresholds for r in res.regions)
    check_partition(res, 900, 2.0)  # exact: no area lost, none counted twice
    # the plate has T-junctions: two angles are not always enough, the third resolves them
    assert section(part, 900, 2.0, extra_angle=90.0).conflicts == []


def test_modifiers_still_cover_the_part_after_merging_slivers():
    res = section(plate_with_windows_close_to_a_cut(), 900, 2.0)
    mods = modifier_footprints(res, margin=2.0)
    assert res.part.difference(unary_union(mods)).area < 1e-4 * res.part.area
    for i, mi in enumerate(mods):
        for j, r in enumerate(res.regions):
            if j != i:
                assert mi.intersection(r.geom).area < 1e-4 * res.part.area


def test_an_isolated_thin_rib_stays_a_region_flagged_as_sliver():
    part = unary_union([box(0, 0, 60, 40), box(60, 19, 160, 20.5)])  # 100 x 1.5 mm rib
    res = section(part, 900, 2.0)
    slivers = [r for r in res.regions if r.stop_reason == "sliver"]
    assert slivers and all(not r.within_thresholds for r in slivers)
    cov = coverage_error(res)
    assert cov["uncovered_area"] < 1e-5 * cov["part_area"]
    assert cov["overlap_area"] < 1e-5 * cov["part_area"]
    # width limit off: the rib is bisected like any other part
    assert len(section(part, 900, 2.0, min_width=0.0).regions) > len(res.regions)


def plate_with_a_thin_rib():
    """60 x 60 plate with a 0.8 x 150 mm rib: the cuts through the plate used to leave nine
    fragments of 0.006-0.02 mm2 along the rib, each one with its own modifier."""
    return unary_union([box(0, 0, 60, 60), box(60, 29.6, 210, 30.4)])


def test_a_thin_rib_leaves_no_dust_regions():
    part = plate_with_a_thin_rib()
    res = section(part, 900, 2.0, extra_angle=90.0)
    assert min(r.area for r in res.regions if r.stop_reason == "sliver") > 2.0 ** 2
    assert len([r for r in res.regions if r.stop_reason == "sliver"]) == 1  # the rib itself
    assert len(res.regions) <= 14
    cov = coverage_error(res)  # exact partition (the rib itself is flagged, so no check_partition)
    assert cov["uncovered_area"] < 1e-6 * cov["part_area"]
    assert cov["overlap_area"] < 1e-6 * cov["part_area"]
    mods = modifier_footprints(res, margin=2.0)
    assert res.part.difference(unary_union(mods)).area < 1e-4 * res.part.area


def test_dust_is_merged_even_when_the_contact_is_shorter_than_min_contact():
    from infill_sectioning.sectioning import Region, merge_dust
    big = box(0, 0, 20, 20)
    dust = box(20, 0, 20.5, 0.4)  # 0.4 mm of shared edge, 0.2 mm2
    mk = lambda g, reason, ok: Region(g, polygon_moments(g), 1, "x", ok, reason)
    out = merge_dust([mk(big, "within thresholds", True), mk(dust, "sliver", False)],
                     2.0, 900, 2.0)
    assert len(out) == 1
    assert out[0].geom.area == pytest.approx(400.2) and out[0].geom.geom_type == "Polygon"


def test_dust_that_touches_nothing_is_left_alone():
    from infill_sectioning.sectioning import Region, merge_dust
    mk = lambda g, reason, ok: Region(g, polygon_moments(g), 1, "x", ok, reason)
    out = merge_dust([mk(box(0, 0, 20, 20), "within thresholds", True),
                      mk(box(50, 0, 50.5, 0.4), "sliver", False)], 2.0, 900, 2.0)
    assert len(out) == 2


def clean_meshes(part, **kw):
    from infill_sectioning.export import extrude
    res = section(part, 900, 2.0, extra_angle=[90.0, 0.0], **kw)
    out = []
    for fp in modifier_footprints(res, margin=2.0):
        out.append(extrude(fp, 0.0, 5.0))
    return res, out


@pytest.mark.parametrize("part", [
    plate_with_windows_close_to_a_cut(),
    plate_with_a_thin_rib(),
    box(0, 0, 200, 200).difference(unary_union(
        [Point(x, y).buffer(4, quad_segs=16) for x in range(15, 200, 25)
         for y in range(15, 200, 25)])),
    Point(0, 0).buffer(60, quad_segs=32).difference(Point(0, 0).buffer(50, quad_segs=32)),
], ids=["windows", "thin_rib", "perforated", "ring"])
def test_every_modifier_mesh_is_closed_and_free_of_degenerate_triangles(part):
    """Open meshes, zero-area triangles and thousands of micro-vertices make Bambu Studio
    slow or unresponsive: a real plate used to give 14 open modifiers out of 34."""
    res, meshes = clean_meshes(part)
    for r, m in zip(res.regions, meshes):
        assert m.is_watertight and m.is_winding_consistent and m.volume > 0, r.id
        assert m.area_faces.min() > 1e-6, r.id
        assert len(m.faces) < 800, (r.id, len(m.faces))  # was > 2300 for a merged sliver
    mods = modifier_footprints(res, margin=2.0)
    assert res.part.difference(unary_union(mods)).area < 1e-4 * res.part.area


def test_clean_outline_removes_spikes_and_redundant_vertices():
    from infill_sectioning.sectioning import clean_outline
    # square with a zero-width spike, collinear points on every side and a tiny arc
    pts = [(0, 0), (5, 0), (10, 0), (10, 5), (10, 10), (30, 10), (10, 10), (5, 10), (0, 10)]
    g = clean_outline(Polygon(pts))
    assert g.geom_type == "Polygon" and g.is_valid
    assert g.area == pytest.approx(100.0, abs=0.01)
    assert len(g.exterior.coords) - 1 == 4
    arc = Point(0, 0).buffer(5, quad_segs=200)  # 800 vertices, sagitta far below 5 um
    assert len(clean_outline(arc).exterior.coords) < 400


def test_cli_writes_the_clean_and_the_detailed_png(tmp_path):
    mesh = trimesh.creation.extrude_polygon(t_shape(), height=4.0)
    mesh.export(tmp_path / "t.stl")
    out = tmp_path / "o"
    section_infill.main([str(tmp_path / "t.stl"), "--out-dir", str(out), "--png-dpi", "80",
                         "--extra-angle", "90"])
    clean, detailed = out / "t_sections.png", out / "t_sections_details.png"
    assert clean.stat().st_size > 5_000 and detailed.stat().st_size > 5_000
    assert clean.read_bytes() != detailed.read_bytes()
    rep = json.loads((out / "t_sections.json").read_text())
    assert rep["parameters"]["min_width_mm"] == 2.0
    out2 = tmp_path / "o2"
    section_infill.main([str(tmp_path / "t.stl"), "--out-dir", str(out2), "--png-dpi", "80",
                         "--no-png-details", "--min-width", "0"])
    assert (out2 / "t_sections.png").exists() and not (out2 / "t_sections_details.png").exists()
    out3 = tmp_path / "o3"
    section_infill.main([str(tmp_path / "t.stl"), "--out-dir", str(out3), "--no-png"])
    assert not list(out3.glob("*.png"))


# --------------------------------------------------------------------------- #
# more than three angles: a region ringed by an odd number of neighbours
# --------------------------------------------------------------------------- #
def _sector(cx, cy, r0, r1, a0, a1, n=60):
    a = [math.radians(a0 + (a1 - a0) * i / n) for i in range(n + 1)]
    outer = [(cx + r1 * math.cos(t), cy + r1 * math.sin(t)) for t in a]
    inner = [(cx + r0 * math.cos(t), cy + r0 * math.sin(t)) for t in reversed(a)]
    return Polygon(outer + inner)


def s_shape():
    """Large S-shaped part with screw holes (~40000 mm2, 69 regions): its regions form a
    mosaic rich in triangles, so three angles are not enough."""
    g = unary_union([_sector(1340, 300, 40, 105, -15, 255), _sector(1360, 215, 45, 105, 165, 435)])
    for hole in [(1415, 354), (1247, 297), (1447, 216), (1265, 170), (1372, 280)]:
        g = g.difference(Point(*hole).buffer(4.5))
    return g


def same_angle_neighbours(res):
    return [(r.id, j) for r in res.regions for j in r.neighbors
            if j > r.id and res.regions[j].angle == r.angle]


def test_three_angles_are_not_enough_on_the_s_shape_and_the_failure_is_reported():
    res = section(s_shape(), 900, 2.0, extra_angle=90.0)
    assert res.conflicts and len(res.conflicts) == len(same_angle_neighbours(res))
    assert not any(r.extra for r in res.regions)  # nothing half-applied


def test_a_fourth_angle_resolves_it_on_few_regions():
    res = section(s_shape(), 900, 2.0, extra_angle=[90.0, 0.0])
    assert res.conflicts == [] and same_angle_neighbours(res) == []
    assert {r.angle for r in res.regions} <= {45.0, 135.0, 90.0, 0.0}
    levels = [r.extra_level for r in res.regions]
    assert levels.count(1) > 0 and levels.count(2) > 0
    # lower bound: 16 vertex-disjoint triangles of mutually adjacent regions each need one
    # region with an extra angle; the search must stay close to it
    assert 16 <= levels.count(1) + levels.count(2) <= 20
    check_partition(res, 900, 2.0)


def test_the_fourth_angle_is_only_used_when_three_are_not_enough():
    res = section(s_shape(), 900, 2.0, extra_angle=[90.0, 0.0], min_contact=3.0)
    assert res.conflicts == []
    assert max(r.extra_level for r in res.regions) == 1  # three angles were enough here


def test_min_contact_ignores_corner_contacts():
    few = section(s_shape(), 900, 2.0, min_contact=5.0)
    many = section(s_shape(), 900, 2.0, min_contact=0.01)
    assert sum(len(r.neighbors) for r in few.regions) < sum(len(r.neighbors) for r in many.regions)
    assert all(w >= 5.0 for r in few.regions for w in r.neighbors.values())


def test_the_extra_angles_must_all_differ():
    for bad in ([90.0, 90.0], [45.0, 0.0], [90.0, 270.0]):
        with pytest.raises(ValueError, match="coincide"):
            section(rect(), 900, 2.0, extra_angle=bad)
    with pytest.raises(ValueError, match="at most two"):
        section(rect(), 900, 2.0, extra_angle=[90.0, 0.0, 10.0])


def test_cli_explains_what_to_do_when_angles_are_not_enough(tmp_path, capsys):
    trimesh.creation.extrude_polygon(s_shape(), height=4.0).export(tmp_path / "s.stl")
    base = [str(tmp_path / "s.stl"), "--png-dpi", "60", "--no-png-details", "--no-stl"]
    section_infill.main(base + ["--out-dir", str(tmp_path / "a")])
    assert "add --extra-angle 90" in capsys.readouterr().out
    section_infill.main(base + ["--out-dir", str(tmp_path / "b"), "--extra-angle", "90"])
    assert "Three angles are not enough" in capsys.readouterr().out
    section_infill.main(base + ["--out-dir", str(tmp_path / "c"), "--extra-angle", "90", "0"])
    out = capsys.readouterr().out
    assert "WARNING" not in out and "extra angle(s)" in out
    rep = json.loads((tmp_path / "c" / "s_sections.json").read_text())
    assert rep["summary"]["adjacency_conflicts"] == []
    assert rep["parameters"]["extra_angles_deg"] == [90.0, 0.0]
    assert {r["extra_angle_level"] for r in rep["regions"]} == {0, 1, 2}
