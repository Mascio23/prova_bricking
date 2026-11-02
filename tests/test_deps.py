"""The start-up check that explains missing libraries."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from infill_sectioning import deps

ROOT = Path(__file__).resolve().parents[1]


def test_everything_required_is_installed_here():
    assert deps.missing_dependencies() == []


def test_missing_libraries_are_reported_by_their_pypi_name():
    fake = {"definitely_not_a_module_xyz": "some-package", "json": "json-not-needed"}
    assert deps.missing_dependencies(fake) == ["some-package"]


def test_message_gives_the_exact_command_for_this_python():
    msg = deps.install_message(["trimesh", "shapely"])
    assert "trimesh, shapely" in msg
    assert f'"{sys.executable}" -m pip install -r requirements.txt' in msg
    assert "Select Interpreter" in msg and "Install dependencies" in msg
    assert "python3-tk" in deps.tkinter_message()


@pytest.mark.skipif(sys.platform != "linux", reason="needs a controlled absence of display")
def test_gui_explains_a_missing_library_instead_of_a_traceback(tmp_path):
    pytest.importorskip("tkinter")
    (tmp_path / "trimesh.py").write_text("raise ImportError('simulated')\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    env.pop("DISPLAY", None)  # no window: the message goes to stderr
    env.pop("WAYLAND_DISPLAY", None)
    r = subprocess.run([sys.executable, str(ROOT / "section_infill_gui.py")], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "Missing libraries" in r.stderr and "trimesh" in r.stderr
    assert "-m pip install -r requirements.txt" in r.stderr
    assert "Traceback" not in r.stderr
