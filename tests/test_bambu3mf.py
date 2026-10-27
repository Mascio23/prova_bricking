"""Tests for the Bambu Studio .3mf export.

tests/data/bambu_reference_provino.3mf is a project saved by Bambu Studio
2.08.02 (H2D, Generic PA) after importing provino.stl and the 4 modifier STLs
produced by this tool as one multi-part object (Bambu account id removed).
In that file the modifiers were left as "normal_part" by mistake; everything
else (geometry, transforms, per-part keys) is the ground truth we compare to.
"""

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
import trimesh

import section_infill
from infill_sectioning.bambu3mf import pattern_warning, read_template

REF = Path(__file__).parent / "data" / "bambu_reference_provino.3mf"
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
P_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"


def read_3mf(path):
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    main = ET.fromstring(files["3D/3dmodel.model"])
    comps = {c.get("objectid"): c.get("transform")
             for c in main.iterfind(".//m:component", NS)}
    item = main.find(".//m:build/m:item", NS).get("transform")
    app = main.find("m:metadata[@name='Application']", NS).text
    objs = ET.fromstring(files["3D/Objects/object_1.model"])
    meshes = {}
    for o in objs.iterfind(".//m:object", NS):
        v = [tuple(round(float(x.get(k)), 6) for k in "xyz")
             for x in o.iterfind(".//m:vertex", NS)]
        t = o.findall(".//m:triangle", NS)
        meshes[o.get("id")] = (frozenset(v), len(t))
    cfg = ET.fromstring(files["Metadata/model_settings.config"])
    parts = {}
    for p in cfg.iterfind("./object/part"):
        md = {m.get("key"): m.get("value") for m in p.iterfind("metadata")}
        parts[p.get("id")] = {"subtype": p.get("subtype"), **md}
    return {"files": files, "comps": comps, "item": item, "app": app,
            "meshes": meshes, "parts": parts,
            "paths": {c.get(P_PATH) for c in main.iterfind(".//m:component", NS)}}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("g")
    mesh = trimesh.creation.box(extents=[100, 20, 4])
    mesh.apply_translation([50, 10, 2])
    mesh.export(tmp / "provino.stl")
    out = tmp / "out"
    section_infill.main([str(tmp / "provino.stl"), "--out-dir", str(out),
                         "--export-3mf", "--template-3mf", str(REF),
                         "--3mf-setting", "sparse_infill_pattern=zig-zag",
                         "--3mf-setting", "sparse_infill_density=100%"])
    return out


def test_matches_reference_project(generated):
    gen, ref = read_3mf(generated / "provino.3mf"), read_3mf(REF)
    assert gen["app"].startswith("BambuStudio-")
    assert gen["paths"] == ref["paths"] == {"/3D/Objects/object_1.model"}
    # same assembly: component transforms and plate placement
    assert gen["comps"] == ref["comps"]
    assert gen["item"] == ref["item"]
    # same geometry, volume by volume
    assert gen["meshes"] == ref["meshes"]
    # same printer / filament / process settings (copied from the template)
    assert gen["files"]["Metadata/project_settings.config"] == \
        ref["files"]["Metadata/project_settings.config"]
    for pid, rp in ref["parts"].items():
        gp = gen["parts"][pid]
        for key in ("matrix", "source_offset_x", "source_offset_y", "source_offset_z",
                    "extruder"):
            assert gp[key] == rp[key], (pid, key)
        if pid != "1":
            assert gp["name"] == rp["name"]


def test_plate_and_aux_files_follow_template(generated):
    gen, ref = read_3mf(generated / "provino.3mf"), read_3mf(REF)
    for name in ("Metadata/filament_sequence.json", "Metadata/slice_info.config",
                 "Metadata/cut_information.xml"):
        assert gen["files"][name] == ref["files"][name]
    plate = ET.fromstring(gen["files"]["Metadata/model_settings.config"]).find("plate")
    md = {m.get("key"): m.get("value") for m in plate.iterfind("metadata")}
    # H2D (two nozzles): filament <-> nozzle mapping must be kept
    assert md["filament_map_mode"] == "Auto For Flush"
    assert md["filament_maps"] == "2 1 1 1"
    assert md["thumbnail_file"] == "Metadata/plate_1.png"
    for name in ("Metadata/plate_1.png", "Metadata/plate_1_small.png"):
        assert gen["files"][name].startswith(b"\x89PNG")
    assert gen["files"]["_rels/.rels"] == ref["files"]["_rels/.rels"]


def test_part_types_and_infill_direction(generated):
    parts = read_3mf(generated / "provino.3mf")["parts"]
    assert parts["1"]["subtype"] == "normal_part"
    assert "infill_direction" not in parts["1"]
    for pid, angle in zip("2345", ["45", "135", "45", "135"]):
        assert parts[pid]["subtype"] == "modifier_part"  # Bambu's modifier type
        assert parts[pid]["infill_direction"] == angle
        assert parts[pid]["sparse_infill_pattern"] == "zig-zag"
        assert parts[pid]["sparse_infill_density"] == "100%"
        # kept equal by Bambu Studio, as in the reference project
        assert parts[pid]["skeleton_infill_density"] == "100%"
        assert parts[pid]["skin_infill_density"] == "100%"
        assert parts[pid]["name"].endswith(f"_{angle}deg.stl")


def test_report_mentions_3mf(generated):
    rep = json.loads((generated / "provino_sections.json").read_text())
    assert rep["bambu_3mf"]["file"] == "provino.3mf"
    assert rep["bambu_3mf"]["warning"] is None


def test_template_reading():
    t = read_template(REF)
    assert t.plate_center == (175.0, 160.0)  # H2D 350 x 320
    assert t.extruder == "1"
    assert t.application.startswith("BambuStudio-02.08")
    assert t.global_settings["printer_model"] == "Bambu Lab H2D"


def test_grid_pattern_warning():
    t = read_template(REF)  # global sparse pattern of the reference: grid
    assert "grid" in pattern_warning(t, {})
    assert pattern_warning(t, {"sparse_infill_pattern": "zig-zag"}) is None
    assert pattern_warning(None, {}) is None


def test_without_template(tmp_path):
    mesh = trimesh.creation.box(extents=[100, 20, 4])
    mesh.apply_translation([50, 10, 2])
    mesh.export(tmp_path / "p.stl")
    section_infill.main([str(tmp_path / "p.stl"), "--out-dir", str(tmp_path / "o"),
                         "--export-3mf"])
    g = read_3mf(tmp_path / "o" / "p.3mf")
    assert "Metadata/project_settings.config" not in g["files"]
    assert g["item"].endswith("175 160 2.5")
    assert [g["parts"][k]["subtype"] for k in "12345"] == \
        ["normal_part"] + ["modifier_part"] * 4


def test_lib3mf_can_read_it(generated):
    lib3mf = pytest.importorskip("lib3mf")
    w = lib3mf.get_wrapper()
    model = w.CreateModel()
    reader = model.QueryReader("3mf")
    reader.SetStrictModeActive(False)
    reader.ReadFromFile(str(generated / "provino.3mf"))
    it = model.GetObjects()
    kinds = []
    while it.MoveNext():
        kinds.append(it.GetCurrentObject().IsMeshObject())
    assert kinds == [True] * 5 + [False]


# --------------------------------------------------------------------------- #
# --infill-density / --infill-pattern
# --------------------------------------------------------------------------- #
def run_cli(tmp_path, *extra):
    mesh = trimesh.creation.box(extents=[100, 20, 4])
    mesh.apply_translation([50, 10, 2])
    mesh.export(tmp_path / "p.stl")
    out = tmp_path / "o"
    section_infill.main([str(tmp_path / "p.stl"), "--out-dir", str(out), "--export-3mf",
                         "--template-3mf", str(REF), *extra])
    return read_3mf(out / "p.3mf"), json.loads((out / "p_sections.json").read_text())


def modifier_parts(g):
    return [g["parts"][k] for k in "2345"]


def test_default_is_solid_rectilinear(tmp_path):
    g, _ = run_cli(tmp_path)
    for m, angle in zip(modifier_parts(g), ["45", "135", "45", "135"]):
        assert m["sparse_infill_density"] == "100%"
        assert m["sparse_infill_pattern"] == "zig-zag"  # Bambu's "Rectilinear"
        assert m["infill_direction"] == angle


def test_sparse_density_keeps_directions(tmp_path):
    g, rep = run_cli(tmp_path, "--infill-density", "20")
    for m, angle in zip(modifier_parts(g), ["45", "135", "45", "135"]):
        assert m["sparse_infill_density"] == "20%"
        assert m["skeleton_infill_density"] == m["skin_infill_density"] == "20%"
        assert m["sparse_infill_pattern"] == "zig-zag"
        assert m["infill_direction"] == angle  # shells + sparse share this key
    # the number of solid layers is NOT touched: it comes from the template
    assert all("top_shell_layers" not in m and "bottom_shell_layers" not in m
               for m in modifier_parts(g))
    assert "infill_direction" not in g["parts"]["1"]
    assert rep["bambu_3mf"]["modifier_settings"]["sparse_infill_density"] == "20%"
    assert rep["bambu_3mf"]["warning"] is None


def test_3mf_setting_overrides_infill_options(tmp_path):
    g, _ = run_cli(tmp_path, "--infill-density", "20", "--3mf-setting",
                   "sparse_infill_density=30%", "--3mf-setting", "top_shell_layers=7")
    for m in modifier_parts(g):
        assert m["sparse_infill_density"] == "30%"
        assert m["top_shell_layers"] == "7"


def test_pattern_names_and_warning(tmp_path):
    from infill_sectioning.bambu3mf import infill_settings, pattern_key
    assert pattern_key("Rectilinear") == "zig-zag"
    assert pattern_key("line") == "line"
    assert pattern_key("aligned-rectilinear") == "alignedrectilinear"
    assert infill_settings(15.5)["sparse_infill_density"] == "15.5%"
    g, rep = run_cli(tmp_path, "--infill-density", "15", "--infill-pattern", "grid")
    assert modifier_parts(g)[0]["sparse_infill_pattern"] == "grid"
    assert "grid" in rep["bambu_3mf"]["warning"]


@pytest.mark.parametrize("bad", ["-1", "101"])
def test_invalid_density(tmp_path, bad):
    with pytest.raises(SystemExit):
        run_cli(tmp_path, "--infill-density", bad)


def test_all_menu_patterns_are_accepted():
    from infill_sectioning.bambu3mf import PATTERNS, pattern_key
    for key, label in PATTERNS:
        assert pattern_key(key) == key
        if key != "zigzag":  # "Zig Zag" would be confused with "zig-zag" (Rectilinear)
            assert pattern_key(label) == key
            assert pattern_key(label.lower().replace(" ", "-")) == key


def test_pattern_written_to_3mf_and_warnings(tmp_path):
    g, rep = run_cli(tmp_path, "--infill-density", "15", "--infill-pattern", "Adaptive Cubic")
    assert modifier_parts(g)[0]["sparse_infill_pattern"] == "adaptivecubic"
    assert "not verified" in rep["bambu_3mf"]["warning"]


def test_unknown_pattern_fails_before_writing(tmp_path):
    mesh = trimesh.creation.box(extents=[100, 20, 4])
    mesh.export(tmp_path / "p.stl")
    with pytest.raises(SystemExit, match="unknown infill pattern"):
        section_infill.main([str(tmp_path / "p.stl"), "--out-dir", str(tmp_path / "o"),
                             "--export-3mf", "--infill-pattern", "rettilineo"])
    assert not (tmp_path / "o").exists()


# --------------------------------------------------------------------------- #
# --plate-rotation
# --------------------------------------------------------------------------- #
def item_transform(g):
    return [float(v) for v in g["item"].split()]


@pytest.mark.parametrize("rot", [30.0, 42.4, 100.0, 180.0])
def test_plate_rotation_turns_object_and_angles(tmp_path, rot):
    import math
    g, rep = run_cli(tmp_path, "--plate-rotation", f"{rot:g}")
    m = item_transform(g)
    c, s_ = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    # 3MF 3x4: image of X axis, image of Y axis, image of Z axis, translation
    assert m[0:3] == pytest.approx([c, s_, 0], abs=1e-6)
    assert m[3:6] == pytest.approx([-s_, c, 0], abs=1e-6)
    assert m[6:9] == pytest.approx([0, 0, 1])
    assert m[9:11] == pytest.approx([175, 160])  # still centred on the plate
    for part, rel in zip(modifier_parts(g), [45, 135, 45, 135]):
        assert float(part["infill_direction"]) == pytest.approx((rel + rot) % 180, abs=1e-5)
    # angles relative to the part are unchanged in the report
    assert [r["infill_angle_deg"] for r in rep["regions"]] == [45, 135, 45, 135]
    assert [r["plate_angle_deg"] for r in rep["regions"]] == pytest.approx(
        [(a + rot) % 180 for a in (45, 135, 45, 135)], abs=1e-5)
    assert rep["bambu_3mf"]["placement"]["plate_rotation_deg"] == rot
    assert rep["bambu_3mf"]["placement"]["plate_position_xy"] == [175.0, 160.0]


def test_zero_rotation_is_unchanged(tmp_path):
    g, rep = run_cli(tmp_path)
    assert item_transform(g)[:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    assert [float(p["infill_direction"]) for p in modifier_parts(g)] == [45, 135, 45, 135]
    assert [r["plate_angle_deg"] for r in rep["regions"]] == [45, 135, 45, 135]


def test_rotated_3mf_geometry_read_back_with_lib3mf(tmp_path):
    lib3mf = pytest.importorskip("lib3mf")
    import math
    mesh = trimesh.creation.box(extents=[100, 20, 4])
    mesh.apply_translation([50, 10, 2])
    mesh.export(tmp_path / "p.stl")
    section_infill.main([str(tmp_path / "p.stl"), "--out-dir", str(tmp_path / "o"),
                         "--export-3mf", "--template-3mf", str(REF), "--plate-rotation", "45"])
    w = lib3mf.get_wrapper()
    model = w.CreateModel()
    reader = model.QueryReader("3mf")
    reader.SetStrictModeActive(False)
    reader.ReadFromFile(str(tmp_path / "o" / "p.3mf"))
    items = model.GetBuildItems()
    assert items.MoveNext()
    fields = items.GetCurrent().GetObjectTransform().Fields
    # row vector convention of the 3MF spec: the X axis goes to (cos, sin, 0)
    assert fields[0][:2] == pytest.approx([math.cos(math.pi / 4), math.sin(math.pi / 4)], abs=1e-6)


def test_plate_fit_warning():
    from infill_sectioning.bambu3mf import Volume, plate_fit_warning
    t = read_template(REF)  # H2D 350 x 320
    box = trimesh.creation.box(extents=[330, 20, 4])
    vols = [Volume("p.stl", box, "normal_part")]
    assert plate_fit_warning(vols, t, 0.0) is None
    assert "beyond the printable area" in plate_fit_warning(vols, t, 90.0)
    assert plate_fit_warning(vols, None, 90.0) is None


def test_inherit_infill_writes_only_the_direction(tmp_path):
    g, rep = run_cli(tmp_path, "--inherit-infill", "--infill-density", "20")
    for m, angle in zip(modifier_parts(g), ["45", "135", "45", "135"]):
        assert m["infill_direction"] == angle
        assert not any(k in m for k in ("sparse_infill_density", "sparse_infill_pattern",
                                        "skeleton_infill_density", "skin_infill_density"))
    # the template's pattern (grid) is the one that will print: the warning must say so
    assert "grid" in rep["bambu_3mf"]["warning"]
    assert rep["bambu_3mf"]["modifier_settings"] == {}
