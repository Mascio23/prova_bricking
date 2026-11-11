"""Modifiers only on the bottom and top layers (--bottom-layers / --top-layers)."""

import json
import re
import zipfile
from pathlib import Path

import pytest
import trimesh
from shapely.geometry import box
from shapely.ops import unary_union

import analyze_gcode
import section_infill
from infill_sectioning.gui_model import FormState
from infill_sectioning.shells import (CENTRAL_SETTINGS, layer_edges, layer_of_modifier,
                                      plan_shells, prismatic_deviation)

REF = Path(__file__).parent / "data" / "bambu_reference_provino.3mf"


# ---------------------------------------------------------------------------- layers
def test_layer_grid_follows_bambu():
    assert len(layer_edges(4.0, 0.2)) - 1 == 20
    assert layer_edges(4.0, 0.2)[:3] == pytest.approx([0.0, 0.2, 0.4])
    # a layer is added while its middle is below the part height
    assert len(layer_edges(4.05, 0.2)) - 1 == 20   # next middle 4.10 > 4.05
    assert len(layer_edges(4.15, 0.2)) - 1 == 21   # next middle 4.10 < 4.15
    e = layer_edges(3.0, 0.2, 0.3)
    assert e[1] == pytest.approx(0.3) and e[2] == pytest.approx(0.5)


@pytest.mark.parametrize("height,lh,first,nb,nt", [
    (10.0, 0.2, 0.2, 3, 5), (10.0, 0.2, 0.3, 4, 2), (30.0, 0.2, 0.2, 3, 5),
    (4.07, 0.2, 0.2, 2, 2), (4.13, 0.16, 0.2, 3, 4), (9.99, 0.28, 0.28, 1, 1),
    (12.345, 0.12, 0.2, 5, 7)])
def test_every_layer_goes_to_the_intended_modifier(height, lh, first, nb, nt):
    """What Bambu Studio will do: a layer belongs to a modifier if the plane at its middle is
    inside. The bottom block must get exactly layers 1..nb, the top block exactly the last nt
    (also when the last layer is thin) and the central modifier everything in between."""
    plan = plan_shells(0.0, height, nb, nt, lh, first)
    n = plan.n_layers
    margin_z = 1.0
    bottom = layer_of_modifier(plan, plan.z_min, plan.z_bottom)
    top = layer_of_modifier(plan, plan.z_top, plan.z_max + margin_z)
    middle = layer_of_modifier(plan, plan.z_bottom, plan.z_top)
    assert bottom == list(range(1, nb + 1))
    assert top == list(range(n - nt + 1, n + 1))
    assert middle == list(range(nb + 1, n - nt + 1))
    assert sorted(bottom + middle + top) == list(range(1, n + 1))  # nothing twice or lost


def test_plan_needs_a_middle_and_valid_numbers():
    with pytest.raises(ValueError, match="no middle"):
        plan_shells(0.0, 2.0, 5, 5, 0.2)   # 10 layers, 5 + 5
    with pytest.raises(ValueError, match="at least 1"):
        plan_shells(0.0, 10.0, 0, 5, 0.2)
    with pytest.raises(ValueError, match="layer height"):
        plan_shells(0.0, 10.0, 3, 5, 0.0)
    assert plan_shells(0.0, 10.0, 3, 5, 0.2).central_layers == 42


def test_block_settings_make_exactly_n_solid_layers():
    plan = plan_shells(0.0, 10.0, 3, 5, 0.2)
    s = plan.settings_for_blocks()
    assert s == {"bottom_shell_layers": "3", "top_shell_layers": "5",
                 "bottom_shell_thickness": "0", "top_shell_thickness": "0"}
    assert set(CENTRAL_SETTINGS.values()) == {"0"}  # no solid layers in the middle


def test_prismatic_deviation():
    flat = trimesh.creation.extrude_polygon(box(0, 0, 100, 20), height=10)
    from infill_sectioning.footprint import extract_footprint
    assert prismatic_deviation(flat, extract_footprint(flat, "projection"), 1, 9) < 1e-3
    up = trimesh.creation.extrude_polygon(box(0, 0, 100, 20), height=4)
    up.apply_translation([0, 0, 6])
    step = trimesh.util.concatenate([trimesh.creation.extrude_polygon(box(0, 0, 100, 40),
                                                                      height=6), up])
    part = extract_footprint(step, "projection")
    assert prismatic_deviation(step, part, 1, 9) > 0.02


# ------------------------------------------------------------------------------- CLI
def parts_of(path):
    """{volume name: (subtype, settings)} of a generated project."""
    with zipfile.ZipFile(path) as z:
        cfg = z.read("Metadata/model_settings.config").decode()
    out = {}
    for m in re.finditer(r'<part id="(\d+)" subtype="(\w+)".*?</part>', cfg, re.S):
        blk = m.group(0)
        name = re.search(r'key="name" value="([^"]+)"', blk).group(1)
        out[name] = (m.group(2), dict(re.findall(r'<metadata key="(\w+)" value="([^"]*)"/>', blk)))
    return out


def run_cli(tmp_path, *flags, height=10.0, name="bar"):
    trimesh.creation.extrude_polygon(box(0, 0, 100, 20), height=height).export(
        tmp_path / f"{name}.stl")
    out = tmp_path / f"out_{name}"
    rc = section_infill.main([str(tmp_path / f"{name}.stl"), "--out-dir", str(out),
                              "--export-3mf", "--template-3mf", str(REF), "--no-png", *flags])
    assert rc == 0
    return out, json.loads((out / f"{name}_sections.json").read_text())


def test_modifiers_only_on_the_solid_layers(tmp_path, capsys):
    out, rep = run_cli(tmp_path, "--bottom-layers", "3", "--top-layers", "5",
                       "--infill-density", "15", "--infill-pattern", "grid")
    parts = parts_of(out / "bar.3mf")
    mods = {n: v for n, v in parts.items() if v[0] == "modifier_part"}
    central = {n: v for n, v in mods.items() if n.endswith("_central.stl")}
    regions = {n: v for n, v in mods.items() if n not in central}
    assert len(central) == 1 and len(regions) == rep["summary"]["n_regions"] == 4
    for _, st in regions.values():
        assert st["bottom_shell_layers"] == "3" and st["top_shell_layers"] == "5"
        assert st["bottom_shell_thickness"] == "0" and st["top_shell_thickness"] == "0"
        assert st["infill_direction"] in ("45", "135")
        assert "sparse_infill_density" not in st  # the sparse infill lives in the middle
    (_, c), = central.values()
    assert (c["top_shell_layers"], c["bottom_shell_layers"]) == ("0", "0")
    assert (c["top_shell_thickness"], c["bottom_shell_thickness"]) == ("0", "0")
    assert c["sparse_infill_density"] == "15%" and c["sparse_infill_pattern"] == "grid"
    assert "infill_direction" not in c  # not sectioned: the global direction
    # geometry: two blocks per region, one block in the middle, no overlaps, all closed
    for f in sorted(out.glob("bar_modifier_0*.stl")):
        m = trimesh.load(f)
        zs = sorted((round(b.bounds[0, 2], 3), round(b.bounds[1, 2], 3))
                    for b in m.split(only_watertight=False))
        assert zs == [(0.0, 0.6), (9.0, 11.0)] and m.is_watertight
    mid = trimesh.load(out / "bar_modifier_central.stl")
    assert mid.is_watertight and mid.bounds[:, 2].round(3).tolist() == [0.6, 9.0]
    # report
    sh = rep["parameters"]["shell_modifiers"]
    assert (sh["bottom_layers"], sh["top_layers"], sh["n_layers"]) == (3, 5, 50)
    assert sh["layer_height_mm"] == 0.2 and sh["central_modifier"] is True
    assert sh["central_z_mm"] == [0.6, 9.0]
    assert rep["central_modifier"]["modifier_stl"] == "bar_modifier_central.stl"
    assert rep["bambu_3mf"]["central_modifier_settings"]["top_shell_layers"] == "0"
    text = capsys.readouterr().out
    assert "central modifier z 0.600..9.000" in text and "grid" in text
    assert "alternation has no effect" not in text  # not an issue: the middle is not sectioned


def test_without_density_options_the_middle_keeps_the_templates(tmp_path):
    out, rep = run_cli(tmp_path, "--bottom-layers", "3", "--top-layers", "5")
    parts = parts_of(out / "bar.3mf")
    (_, c), = [v for n, v in parts.items() if n.endswith("_central.stl")]
    assert "sparse_infill_density" not in c and "sparse_infill_pattern" not in c
    assert c["top_shell_layers"] == "0"


def test_layer_height_comes_from_the_template_or_the_option(tmp_path):
    _, rep = run_cli(tmp_path, "--bottom-layers", "2", "--top-layers", "2")
    assert rep["parameters"]["shell_modifiers"]["layer_height_mm"] == 0.2   # template: 0.2
    out2, rep2 = run_cli(tmp_path, "--bottom-layers", "2", "--top-layers", "2",
                         "--layer-height", "0.25", "--first-layer-height", "0.3", name="bar2")
    sh = rep2["parameters"]["shell_modifiers"]
    assert (sh["layer_height_mm"], sh["first_layer_height_mm"]) == (0.25, 0.3)
    assert sh["n_layers"] == 40 and sh["bottom_block_z_mm"] == [0.0, 0.55]


def test_part_with_a_step_gets_no_central_modifier_by_default(tmp_path, capsys):
    up = trimesh.creation.extrude_polygon(box(0, 0, 100, 20), height=4)
    up.apply_translation([0, 0, 6])
    trimesh.util.concatenate([trimesh.creation.extrude_polygon(box(0, 0, 100, 40), height=6),
                              up]).export(tmp_path / "step.stl")
    common = [str(tmp_path / "step.stl"), "--out-dir", str(tmp_path / "o"), "--export-3mf",
              "--template-3mf", str(REF), "--no-png", "--bottom-layers", "3", "--top-layers", "5"]
    section_infill.main(common)
    text = capsys.readouterr().out
    assert "NO central modifier" in text and "no central modifier" in text.lower()
    names = [n for n in parts_of(tmp_path / "o" / "step.3mf")]
    assert not any(n.endswith("_central.stl") for n in names)
    # asked explicitly: written, with a warning
    section_infill.main(common[:2] + [str(tmp_path / "o2")] + common[3:] +
                        ["--central-modifier", "yes"])
    text = capsys.readouterr().out
    assert "not a plain extrusion" in text and "get no solid layers" in text
    assert any(n.endswith("_central.stl") for n in parts_of(tmp_path / "o2" / "step.3mf"))


def test_ignored_density_without_central_modifier_is_reported(tmp_path, capsys):
    run_cli(tmp_path, "--bottom-layers", "3", "--top-layers", "5", "--central-modifier", "no",
            "--infill-density", "15")
    text = capsys.readouterr().out
    assert "are ignored: there is no central modifier" in text
    assert "no central modifier: the middle keeps the global settings" in text


def test_blocks_shorter_than_the_templates_shell_are_reported(tmp_path, capsys):
    # template: top 5 layers / 1 mm. 3 top layers without a central modifier -> 2 solid
    # layers below the block would get the global direction
    run_cli(tmp_path, "--bottom-layers", "3", "--top-layers", "3", "--central-modifier", "no")
    text = capsys.readouterr().out
    assert "template's top shell (5 layers" in text and "2 more layer(s)" in text
    assert "bottom shell" not in text  # 3 layers = the template's bottom_shell_layers


def test_option_errors(tmp_path):
    base = trimesh.creation.extrude_polygon(box(0, 0, 100, 20), height=10)
    base.export(tmp_path / "b.stl")

    def fails(*flags, text):
        with pytest.raises(SystemExit) as exc:
            section_infill.main([str(tmp_path / "b.stl"), "--out-dir", str(tmp_path / "o"),
                                 "--no-png", *flags])
        assert text in str(exc.value)

    fails("--bottom-layers", "3", text="go together")
    fails("--top-layers", "3", text="go together")
    fails("--bottom-layers", "0", "--top-layers", "3", text="--bottom-layers must be at least 1")
    fails("--bottom-layers", "3", "--top-layers", "3", "--layer-height", "0", text="greater than 0")
    fails("--layer-height", "0.2", text="only make sense with --bottom-layers")
    fails("--central-modifier", "no", text="only make sense with --bottom-layers")
    fails("--bottom-layers", "30", "--top-layers", "30", text="leave no middle")
    fails("--bottom-layers", "3", "--top-layers", "3", "--template-3mf",
          str(tmp_path / "nope.3mf"), text="template file not found")


# ------------------------------------------------------------------------ G-code check
def test_analyze_gcode_checks_only_the_solid_layers(tmp_path, capsys):
    from test_analyze_gcode import SECTIONS, gcode
    sections = json.loads(json.dumps(SECTIONS))
    sections["parameters"] = {"shell_modifiers": {
        "bottom_layers": 1, "top_layers": 1, "n_layers": 4,
        "layer_height_mm": 0.2, "first_layer_height_mm": 0.2}}
    good = ("Internal solid infill", [+1], [-1])
    middle = ("Sparse infill", [+1], [+1])  # not sectioned: both regions the same direction
    text = gcode([good, middle, middle, ("Top surface", [+1], [-1])])
    g, s = tmp_path / "x.gcode", tmp_path / "s.json"
    g.write_text(text)
    s.write_text(json.dumps(sections))
    assert analyze_gcode.main([str(g), str(s), "--offset", "0", "0"]) == 0
    out = capsys.readouterr().out
    assert "layers 1-2 (the middle, not sectioned) are not checked" in out
    # the same file without the shell information fails: the middle is not alternated
    sections["parameters"] = {}
    s.write_text(json.dumps(sections))
    assert analyze_gcode.main([str(g), str(s), "--offset", "0", "0"]) == 1
    # a different number of layers than planned is reported
    sections["parameters"] = {"shell_modifiers": {
        "bottom_layers": 1, "top_layers": 1, "n_layers": 40,
        "layer_height_mm": 0.2, "first_layer_height_mm": 0.2}}
    s.write_text(json.dumps(sections))
    analyze_gcode.main([str(g), str(s), "--offset", "0", "0"])
    assert "the G-code has 4 layers, the modifiers were sized for 40" in capsys.readouterr().out


# --------------------------------------------------------------------------------- GUI
def test_form_state_builds_the_shell_options():
    s = FormState(model="p.stl", use_shell_modifiers=True, bottom_layers="3", top_layers="5",
                  layer_height="0,2", central_modifier="no")
    argv = s.to_argv()
    i = argv.index("--bottom-layers")
    assert argv[i:i + 4] == ["--bottom-layers", "3", "--top-layers", "5"]
    assert argv[argv.index("--layer-height") + 1] == "0.2"
    assert argv[argv.index("--central-modifier") + 1] == "no"
    assert "--first-layer-height" not in argv
    assert s.validate(check_files=False) == []
    args = section_infill.parse_args(argv)
    assert (args.bottom_layers, args.top_layers, args.central_modifier) == (3, 5, "no")
    # off by default, and not sent when the 3mf is not written or for the preview
    assert "--bottom-layers" not in FormState(model="p.stl").to_argv()
    assert "--bottom-layers" not in s.to_argv(preview_dir="/tmp/x")
    assert "--bottom-layers" not in FormState(model="p.stl", use_shell_modifiers=True,
                                              export_3mf=False).to_argv()


def test_form_state_validates_the_shell_fields():
    bad = FormState(model="p.stl", use_shell_modifiers=True, bottom_layers="0", top_layers="a",
                    layer_height="-1", first_layer_height="0", central_modifier="maybe")
    msgs = bad.validate(check_files=False)
    assert any("Bottom layers: must be at least 1" in m for m in msgs)
    assert any("Top layers" in m and "not a number" in m for m in msgs)
    assert any("Layer height: must be greater than 0" in m for m in msgs)
    assert any("First layer height" in m for m in msgs)
    assert any("Central modifier" in m for m in msgs)
    # fields are not checked while the box is unticked
    assert FormState(model="p.stl", bottom_layers="zero").validate(check_files=False) == []
    # presets keep the new fields
    s = FormState(use_shell_modifiers=True, bottom_layers="4", layer_height="0.16")
    assert FormState.from_dict(s.to_dict()) == s


def test_footprint_union_of_the_central_modifier_covers_the_part(tmp_path):
    out, rep = run_cli(tmp_path, "--bottom-layers", "3", "--top-layers", "5")
    mid = trimesh.load(out / "bar_modifier_central.stl")
    union = unary_union([box(*e["modifier_bbox"]) for e in rep["regions"]])
    assert mid.bounds[0, :2].round(3).tolist() == [round(union.bounds[0], 3),
                                                   round(union.bounds[1], 3)]
    assert mid.bounds[1, :2].round(3).tolist() == [round(union.bounds[2], 3),
                                                   round(union.bounds[3], 3)]
