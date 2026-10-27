"""Smoke test of the window itself. Skipped without tkinter or a display
(on Linux: xvfb-run python -m pytest tests/test_gui_smoke.py)."""

import json
import time

import pytest
import trimesh

tk = pytest.importorskip("tkinter")

import section_infill_gui  # noqa: E402
from infill_sectioning.gui_model import FormState  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(section_infill_gui, "LAST_STATE", tmp_path / "last.json")
    monkeypatch.setattr(section_infill_gui.messagebox, "showerror",
                        lambda *a, **k: errors.append(a[1]))
    monkeypatch.setattr(section_infill_gui.messagebox, "askyesno", lambda *a, **k: True)
    errors.clear()
    try:
        a = section_infill_gui.App(FormState())
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    yield a
    try:
        a.destroy()
    except tk.TclError:  # the test already closed the window
        pass


errors: list = []


def wait(app, seconds=60):
    end = time.time() + seconds
    while app.busy and time.time() < end:
        app.update()
        time.sleep(0.05)
    app.update()
    assert not app.busy, "the run did not finish"


def make_model(tmp_path):
    mesh = trimesh.creation.box(extents=[100, 20, 4])
    mesh.apply_translation([50, 10, 2])
    mesh.export(tmp_path / "provino.stl")
    return str(tmp_path / "provino.stl")


def test_command_follows_the_form(app, tmp_path):
    s = app.get_state()
    s.model, s.use_rotation, s.rotation, s.density = make_model(tmp_path), True, "42,4", "20"
    app.set_state(s)
    app.update()
    shown = app.cmd.get("1.0", "end")
    assert "--plate-rotation 42.4" in shown and "--infill-density 20" in shown
    app.v["use_rotation"].set(False)
    app.update()
    assert "--plate-rotation" not in app.cmd.get("1.0", "end")


def test_dependent_fields_are_enabled_by_their_checkbox(app):
    app.v["use_extra_angle"].set(False)
    app.v["use_rotation"].set(True)
    app.v["export_3mf"].set(True)
    app.update()
    assert str(app.extra_entry.cget("state")) == "disabled"
    assert str(app.rot_entry.cget("state")) == "normal"
    app.v["export_3mf"].set(False)  # no .3mf: its options are greyed out
    app.update()
    assert str(app.rot_entry.cget("state")) == "disabled"
    assert str(app.pattern_entry.cget("state")) == "disabled"
    assert str(app.tpl_entry.cget("state")) == "disabled"


def test_invalid_form_shows_errors_and_runs_nothing(app, tmp_path):
    s = FormState(model=make_model(tmp_path), out_dir=str(tmp_path / "o"), max_area="abc")
    app.set_state(s)
    app.generate()
    assert errors and "Area massima" in errors[-1]
    assert not (tmp_path / "o").exists() and not app.busy


def test_preview_then_generate(app, tmp_path):
    s = FormState(model=make_model(tmp_path), out_dir=str(tmp_path / "o"), density="20",
                  use_rotation=True, rotation="30", png_dpi="80")
    app.set_state(s)
    app.preview()
    assert app.busy
    wait(app)
    assert app.preview_img is not None and app.preview_img.width() > 100
    assert not (tmp_path / "o").exists()  # the preview writes nothing in the output folder
    assert "4 regions" in app.log.get("1.0", "end")

    app.generate()
    wait(app)
    out = tmp_path / "o"
    assert (out / "provino.3mf").exists() and (out / "provino_sections.json").exists()
    rep = json.loads((out / "provino_sections.json").read_text())
    assert rep["parameters"]["plate_rotation_deg"] == 30.0
    assert "Fatto" in app.status.cget("text")


def test_error_of_the_program_is_shown_in_the_log(app, tmp_path):
    s = FormState(model=str(tmp_path / "manca.stl"), out_dir=str(tmp_path / "o"))
    app.set_state(s)
    s.validate = lambda *a, **k: []  # let the program itself fail
    app._check_form = lambda: s
    app.generate()
    wait(app)
    assert "Errore" in app.status.cget("text")
    assert app.log.get("1.0", "end").strip()


def test_presets_and_last_state(app, tmp_path):
    app.v["density"].set("35")
    app.get_state().save(tmp_path / "p.json")
    app.v["density"].set("100")
    app.set_state(FormState.load(tmp_path / "p.json"))
    assert app.v["density"].get() == "35"
    app._close()
    assert FormState.load(tmp_path / "last.json").density == "35"
