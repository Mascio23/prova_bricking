"""analyze_gcode.py on small synthetic G-code (two 25 x 20 mm regions side by side)."""

import json

import pytest

import analyze_gcode

SECTIONS = {
    "part": {"bbox": [0, 0, 50, 20]},
    "regions": [
        {"id": 0, "infill_angle_deg": 45.0, "neighbors": [1],
         "polygon": [{"exterior": [[0, 0], [25, 0], [25, 20], [0, 20], [0, 0]], "holes": []}]},
        {"id": 1, "infill_angle_deg": 135.0, "neighbors": [0],
         "polygon": [{"exterior": [[25, 0], [50, 0], [50, 20], [25, 20], [25, 0]], "holes": []}]},
    ],
}


def diagonal(x0, y0, direction, n=6, step=3.0, length=8.0):
    """n parallel extrusion lines at 45 (direction=+1) or 135 (-1) deg starting at (x0, y0)."""
    out = []
    for i in range(n):
        xs = x0 + i * step
        ys = y0 if direction > 0 else y0 + length
        out.append(f"G1 X{xs:.3f} Y{ys:.3f}")
        out.append(f"G1 X{xs + length:.3f} Y{ys + direction * length:.3f} E1.0")
    return out


def gcode(per_layer):
    """per_layer: list of (feature, dir_R0, dir_R1), directions are lists of +1/-1."""
    lines = []
    for feat, d0, d1 in per_layer:
        lines += ["; CHANGE_LAYER", f"; FEATURE: {feat}"]
        for d in d0:
            lines += diagonal(2, 2, d)
        for d in d1:
            lines += diagonal(28, 2, d)
    return "\n".join(lines) + "\n"


@pytest.fixture
def sections(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SECTIONS))
    return p


def run(tmp_path, sections, text):
    g = tmp_path / "p.gcode"
    g.write_text(text)
    return analyze_gcode.main([str(g), str(sections), "--offset", "0", "0"])


def test_alternating_regions_pass(tmp_path, sections, capsys):
    layers = [("Bottom surface", [+1], [-1]), ("Sparse infill", [-1], [+1]),
              ("Sparse infill", [+1], [-1])]
    assert run(tmp_path, sections, gcode(layers)) == 0
    out = capsys.readouterr().out
    assert "OK: adjacent regions" in out and "45" in out and "135" in out


def test_grid_like_layers_fail(tmp_path, sections, capsys):
    layers = [("Sparse infill", [+1, -1], [+1, -1])]  # both diagonals in both regions
    assert run(tmp_path, sections, gcode(layers)) == 1
    assert "NOT ORTHOGONAL" in capsys.readouterr().out


def test_same_direction_regions_fail(tmp_path, sections):
    assert run(tmp_path, sections, gcode([("Top surface", [+1], [+1])])) == 1


def test_bridge_layers_are_not_checked(tmp_path, sections, capsys):
    layers = [("Sparse infill", [+1], [-1]), ("Bridge", [-1], [-1])]
    assert run(tmp_path, sections, gcode(layers)) == 0
    assert "[1]" in capsys.readouterr().out  # layer 1 listed as automatic
