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
