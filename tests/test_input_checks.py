"""The CLI turns bad input into a one-line "error: ..." and warns about suspicious input."""

import json

import numpy as np
import pytest
import trimesh
from shapely.geometry import box

import section_infill


def stl(tmp_path, name="p.stl", w=100.0, h=20.0, z=4.0):
    path = tmp_path / name
    trimesh.creation.extrude_polygon(box(0, 0, w, h), height=z).export(path)
    return path


def call(tmp_path, model, *extra):
    return section_infill.main([str(model), "--out-dir", str(tmp_path / "o"), "--no-png",
                                "--no-stl", *extra])


def fails(tmp_path, model, capsys, *extra):
    with pytest.raises(SystemExit) as exc:
        call(tmp_path, model, *extra)
    msg = str(exc.value)
    assert msg.startswith("error: ") and "\n" not in msg
    return msg


def test_missing_file(tmp_path, capsys):
    assert "not found" in fails(tmp_path, tmp_path / "nope.stl", capsys)


def test_file_that_is_not_a_model(tmp_path, capsys):
    bad = tmp_path / "text.stl"
    bad.write_text("ciao")
    assert "cannot use text.stl" in fails(tmp_path, bad, capsys)
    empty = tmp_path / "empty.stl"
    empty.write_bytes(b"")
    assert "cannot use empty.stl" in fails(tmp_path, empty, capsys)


def test_single_wall_has_no_footprint(tmp_path, capsys):
    wall = trimesh.Trimesh(vertices=[[0, 0, 0], [50, 0, 0], [50, 0, 10], [0, 0, 10]],
                           faces=[[0, 1, 2], [0, 2, 3]])
    wall.export(tmp_path / "wall.stl")
    assert "no footprint area" in fails(tmp_path, tmp_path / "wall.stl", capsys)


def test_flat_model(tmp_path, capsys):
    flat = trimesh.Trimesh(vertices=[[0, 0, 0], [10, 0, 0], [10, 10, 0]], faces=[[0, 1, 2]])
    flat.export(tmp_path / "flat.stl")
    assert "is flat" in fails(tmp_path, tmp_path / "flat.stl", capsys)


@pytest.mark.parametrize("flags,text", [
    (("--max-area", "0"), "--max-area must be greater than 0"),
    (("--max-area", "-5"), "--max-area must be greater than 0"),
    (("--max-ar", "0.5"), "--max-ar must be at least 1"),
    (("--min-width", "-1"), "--min-width cannot be negative"),
    (("--min-contact", "-1"), "--min-contact cannot be negative"),
    (("--max-depth", "0"), "--max-depth must be at least 1"),
])
def test_impossible_parameters(tmp_path, capsys, flags, text):
    assert text in fails(tmp_path, stl(tmp_path), capsys, *flags)


def test_parameters_are_checked_before_the_model_is_read(tmp_path, capsys):
    assert "--max-area" in fails(tmp_path, tmp_path / "nope.stl", capsys, "--max-area", "0")


def test_part_in_inches_is_reported(tmp_path, capsys):
    call(tmp_path, stl(tmp_path, w=3.937, h=0.787, z=0.157))
    out = capsys.readouterr().out
    assert "WARNING: the part is only 3.94 x 0.79 mm" in out and "inches" in out
    rep = json.loads((tmp_path / "o" / "p_sections.json").read_text())
    assert any("inches" in w for w in rep["warnings"])


def test_oversized_part_is_reported(tmp_path, capsys):
    call(tmp_path, stl(tmp_path, w=1500, h=20), "--max-area", "1e9", "--max-ar", "100")
    assert "micrometres" in capsys.readouterr().out


def test_open_mesh_is_reported_but_still_sectioned(tmp_path, capsys):
    m = trimesh.creation.extrude_polygon(box(0, 0, 100, 20), height=4.0)
    m.update_faces(np.arange(len(m.faces))[:-3])
    m.export(tmp_path / "open.stl")
    call(tmp_path, tmp_path / "open.stl")
    out = capsys.readouterr().out
    assert "not watertight" in out and "4 regions" in out


def test_normal_part_has_no_warnings(tmp_path, capsys):
    call(tmp_path, stl(tmp_path))
    assert "WARNING" not in capsys.readouterr().out
    rep = json.loads((tmp_path / "o" / "p_sections.json").read_text())
    assert rep["warnings"] == []


def test_thin_rib_is_explained(tmp_path, capsys):
    from shapely.ops import unary_union
    part = unary_union([box(0, 0, 60, 60), box(60, 29.6, 210, 30.4)])
    trimesh.creation.extrude_polygon(part, height=4.0).export(tmp_path / "rib.stl")
    call(tmp_path, tmp_path / "rib.stl", "--extra-angle", "90")
    out = capsys.readouterr().out
    assert "thinner than --min-width" in out and "hold no infill" in out
