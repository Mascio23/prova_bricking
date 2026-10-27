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


# --------------------------------------------------------------------------- #
# part rotated on the plate
# --------------------------------------------------------------------------- #
import math  # noqa: E402


def rotated_gcode(per_layer, to_plate, rotation, n=5, step=3.0, length=10.0):
    """G-code in the PLATE frame. per_layer: [(feature, dir_R0, dir_R1)] with the region
    angles relative to the part (45/135) turned by `rotation`; dir = 0 or 90 extra degrees."""
    centres = [to_plate((12.5, 10.0)), to_plate((37.5, 10.0))]
    angles = [45.0, 135.0]
    lines = []
    for feat, extra0, extra1 in per_layer:
        lines += ["; CHANGE_LAYER", f"; FEATURE: {feat}"]
        for centre, a, extra in zip(centres, angles, (extra0, extra1)):
            th = math.radians(a + extra + rotation)
            d = (math.cos(th), math.sin(th))
            nrm = (-d[1], d[0])
            for k in range(n):
                o = (k - (n - 1) / 2) * step
                p0 = (centre[0] + nrm[0] * o - d[0] * length / 2,
                      centre[1] + nrm[1] * o - d[1] * length / 2)
                lines.append(f"G1 X{p0[0]:.3f} Y{p0[1]:.3f}")
                lines.append(f"G1 X{p0[0] + d[0] * length:.3f} Y{p0[1] + d[1] * length:.3f} E1.0")
    return "\n".join(lines) + "\n"


def with_placement(tmp_path, rotation):
    sec = json.loads(json.dumps(SECTIONS))
    sec["bambu_3mf"] = {"placement": {"object_origin_xy": [25.0, 10.0],
                                      "plate_position_xy": [100.0, 80.0],
                                      "plate_rotation_deg": rotation}}
    p = tmp_path / "s_rot.json"
    p.write_text(json.dumps(sec))

    def to_plate(pt):
        c, s_ = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
        x, y = pt[0] - 25.0, pt[1] - 10.0
        return (c * x - s_ * y + 100.0, s_ * x + c * y + 80.0)
    return p, to_plate


LAYERS = [("Bottom surface", 0, 0), ("Sparse infill", 90, 90), ("Sparse infill", 0, 0),
          ("Top surface", 90, 90)]


@pytest.mark.parametrize("rotation", [30.0, 42.4, 100.0])
def test_rotated_part_with_compensated_angles_passes(tmp_path, rotation, capsys):
    sec, to_plate = with_placement(tmp_path, rotation)
    g = tmp_path / "r.gcode"
    g.write_text(rotated_gcode(LAYERS, to_plate, rotation))
    assert analyze_gcode.main([str(g), str(sec)]) == 0
    out = capsys.readouterr().out
    assert f"{rotation:g} deg" in out


def test_uncompensated_angles_on_a_rotated_part_fail(tmp_path, capsys):
    """Plate angles left at 45/135 although the part is rotated by 30 deg: the regions
    are still orthogonal to each other, but not at 45/135 relative to the part."""
    sec, to_plate = with_placement(tmp_path, 30.0)
    g = tmp_path / "r.gcode"
    g.write_text(rotated_gcode(LAYERS, to_plate, rotation=0.0))  # lines NOT turned by 30
    assert analyze_gcode.main([str(g), str(sec)]) == 1
    out = capsys.readouterr().out
    assert "WRONG DIRECTION RELATIVE TO THE PART" in out
    assert "NOT ORTHOGONAL" not in out


def test_manual_rotation_and_offset(tmp_path):
    rotation, offset = 30.0, (120.0, 60.0)

    def to_plate(pt):  # --rotation about the model origin, then --offset
        c, s_ = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
        return (c * pt[0] - s_ * pt[1] + offset[0], s_ * pt[0] + c * pt[1] + offset[1])

    sec = tmp_path / "plain.json"
    sec.write_text(json.dumps(SECTIONS))  # no placement recorded
    g = tmp_path / "m.gcode"
    g.write_text(rotated_gcode(LAYERS, to_plate, rotation))
    assert analyze_gcode.main([str(g), str(sec), "--rotation", "30",
                               "--offset", "120", "60"]) == 0


def test_direction_clusters():
    segs = [(87.3, 50.0), (87.5, 40.0), (86.9, 30.0), (1.0, 2.0)]  # + a short connector
    cl = analyze_gcode.clusters(segs)
    assert len(cl) == 1 and cl[0][0] == pytest.approx(87.3, abs=0.3)
    both = [(45.0, 100.0), (135.0, 90.0)]
    assert len(analyze_gcode.clusters(both)) == 2
    assert analyze_gcode.circ_dist(179.0, 1.0) == pytest.approx(2.0)


def lines_at(centre, angle, n=5, step=3.0, length=10.0):
    th = math.radians(angle)
    d, nrm = (math.cos(th), math.sin(th)), (-math.sin(th), math.cos(th))
    out = []
    for k in range(n):
        o = (k - (n - 1) / 2) * step
        p0 = (centre[0] + nrm[0] * o - d[0] * length / 2, centre[1] + nrm[1] * o - d[1] * length / 2)
        out += [f"G1 X{p0[0]:.3f} Y{p0[1]:.3f}",
                f"G1 X{p0[0] + d[0] * length:.3f} Y{p0[1] + d[1] * length:.3f} E1.0"]
    return out


def two_region_gcode(a0, a1):
    return "\n".join(["; CHANGE_LAYER", "; FEATURE: Sparse infill"]
                     + lines_at((12.5, 10.0), a0) + lines_at((37.5, 10.0), a1)) + "\n"


def test_neighbours_at_a_configured_non_orthogonal_difference(tmp_path, capsys):
    """Regions with the optional third angle: 45 next to 90 is the configured difference."""
    sec = json.loads(json.dumps(SECTIONS))
    sec["regions"][1]["infill_angle_deg"] = 90.0
    s = tmp_path / "s.json"
    s.write_text(json.dumps(sec))
    g = tmp_path / "x.gcode"
    g.write_text(two_region_gcode(45.0, 90.0))
    assert analyze_gcode.main([str(g), str(s), "--offset", "0", "0"]) == 0
    g.write_text(two_region_gcode(45.0, 135.0))  # orthogonal, but not what was configured
    assert analyze_gcode.main([str(g), str(s), "--offset", "0", "0"]) == 1
    assert "WRONG ANGLE BETWEEN NEIGHBOURS" in capsys.readouterr().out
