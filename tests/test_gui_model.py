"""The form state of the GUI: validation and the command line it produces."""

import json

import pytest
import trimesh

import section_infill
from infill_sectioning.gui_model import FormState


def state(tmp_path, **kw):
    mesh = trimesh.creation.box(extents=[100, 20, 4])
    mesh.apply_translation([50, 10, 2])
    mesh.export(tmp_path / "provino.stl")
    return FormState(**{"model": str(tmp_path / "provino.stl"), "out_dir": str(tmp_path / "o"),
                        **kw})


def test_defaults_match_the_command_line(tmp_path):
    s = state(tmp_path)
    assert s.validate() == []
    argv = s.to_argv()
    assert argv[:2] == [s.model, "--out-dir"]
    assert argv[argv.index("--angles"):argv.index("--angles") + 3] == ["--angles", "45", "135"]
    assert "--export-3mf" in argv and "--no-png" not in argv
    assert argv[argv.index("--infill-density") + 1] == "100"
    assert argv[argv.index("--infill-pattern") + 1] == "rectilinear"
    # the parser of section_infill accepts it, and gives the same defaults
    args = section_infill.parse_args(argv)
    assert (args.max_area, args.max_ar, args.angles) == (900.0, 2.0, [45.0, 135.0])
    assert args.export_3mf and args.infill_density == 100.0 and not args.inherit_infill


def test_toggles_produce_the_matching_options(tmp_path):
    s = state(tmp_path, use_extra_angle=True, extra_angle="90", use_rotation=True,
              rotation="42,4", set_infill=True, density="15,5", pattern="grid",
              template="", extra_settings="top_shell_layers=7\n# commento\n\nwall_loops = 3",
              write_png=False, write_stl=False, mode="nsplit", max_area="1000")
    argv = s.to_argv()
    assert argv[argv.index("--extra-angle") + 1] == "90"
    assert argv[argv.index("--plate-rotation") + 1] == "42.4"  # decimal comma accepted
    assert argv[argv.index("--infill-density") + 1] == "15.5"
    assert argv[argv.index("--mode") + 1] == "nsplit"
    assert "--no-png" in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--3mf-setting"] == \
        ["top_shell_layers=7", "wall_loops = 3"]
    args = section_infill.parse_args(argv)
    assert args.plate_rotation == 42.4 and args.extra_angle == 90.0 and args.mode == "nsplit"


def test_unchecked_options_are_left_out(tmp_path):
    s = state(tmp_path, export_3mf=False, use_extra_angle=False, use_rotation=True, rotation="30",
              set_infill=False)
    argv = s.to_argv()
    for flag in ("--export-3mf", "--plate-rotation", "--extra-angle", "--infill-density",
                 "--infill-pattern", "--inherit-infill", "--template-3mf"):
        assert flag not in argv
    s = state(tmp_path, set_infill=False)
    assert "--inherit-infill" in s.to_argv() and "--infill-density" not in s.to_argv()
    assert "--plate-rotation" not in state(tmp_path, use_rotation=True, rotation="0").to_argv()
    assert "--no-stl" in state(tmp_path, write_stl=False, export_3mf=False).to_argv()
    assert "--no-stl" not in state(tmp_path, write_stl=False, export_3mf=True).to_argv()


def test_preview_arguments(tmp_path):
    argv = state(tmp_path).to_argv(preview_dir=tmp_path / "pv")
    assert str(tmp_path / "pv") in argv
    assert "--no-stl" in argv and "--png-dpi" in argv and "--export-3mf" not in argv


@pytest.mark.parametrize("field, value, expected", [
    ("max_area", "abc", "Area massima"), ("max_area", "0", "Area massima"),
    ("max_ar", "0.5", "Aspect ratio"), ("angle2", "225", "coincidono"),
    ("density", "120", "Densità"), ("pattern", "rettilineo", "Pattern"),
    ("rotation", "x", "Rotazione"), ("max_depth", "2.5", "intero"),
    ("extra_settings", "senza uguale", "CHIAVE=VALORE"), ("model", "", "Modello"),
    ("template", "/non/esiste.3mf", "Template"), ("png_dpi", "5", "PNG"),
])
def test_validation_messages(tmp_path, field, value, expected):
    kw = {field: value, **({"use_rotation": True} if field == "rotation" else {})}
    problems = state(tmp_path, **kw).validate()
    assert any(expected in p for p in problems), problems


def test_extra_angle_validation(tmp_path):
    assert any("Terzo angolo" in p for p in
               state(tmp_path, use_extra_angle=True, extra_angle="45").validate())
    assert state(tmp_path, use_extra_angle=False, extra_angle="45").validate() == []


def test_command_quotes_paths_with_spaces(tmp_path):
    s = FormState(model="C:/Users/me/OneDrive - Univ/base s.stl", out_dir="out put")
    cmd = s.command()
    assert cmd.startswith("python section_infill.py ")
    assert '"C:/Users/me/OneDrive - Univ/base s.stl"' in cmd and '"out put"' in cmd


def test_preset_roundtrip_ignores_bad_keys(tmp_path):
    s = state(tmp_path, density="20", use_rotation=True, rotation="12")
    s.save(tmp_path / "p.json")
    assert FormState.load(tmp_path / "p.json") == s
    d = json.loads((tmp_path / "p.json").read_text())
    d.update({"sconosciuto": "x", "density": 5, "write_png": "no"})  # wrong types are dropped
    (tmp_path / "bad.json").write_text(json.dumps(d))
    t = FormState.load(tmp_path / "bad.json")
    assert t.density == "100" and t.write_png is True and t.rotation == "12"


def test_the_command_really_runs(tmp_path):
    s = state(tmp_path, density="20", use_rotation=True, rotation="30")
    assert section_infill.main(s.to_argv()) == 0
    out = tmp_path / "o"
    rep = json.loads((out / "provino_sections.json").read_text())
    assert rep["parameters"]["plate_rotation_deg"] == 30.0
    assert (out / "provino.3mf").exists()
