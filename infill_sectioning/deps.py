"""Check that the libraries are installed *for the Python that is running*.

Typical case: in Visual Studio Code the Run button uses the interpreter selected
with "Python: Select Interpreter", which may be a different Python from the one
where `pip install -r requirements.txt` was run. A clear message with the exact
command to type is more useful than a traceback.

This module must stay light: it is imported before the heavy libraries.
"""

from __future__ import annotations

import importlib
import sys

# import name -> name on PyPI
REQUIRED = {
    "numpy": "numpy",
    "shapely": "shapely",
    "trimesh": "trimesh",
    "cv2": "opencv-python",
    "matplotlib": "matplotlib",
    "mapbox_earcut": "mapbox-earcut",
    "scipy": "scipy",
    "networkx": "networkx",
}


def missing_dependencies(required: dict[str, str] | None = None) -> list[str]:
    """PyPI names of the libraries that cannot be imported."""
    missing = []
    for module, package in (REQUIRED if required is None else required).items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    return missing


def install_message(missing: list[str], python: str | None = None) -> str:
    python = python or sys.executable
    return (
        "The following libraries are not installed for the Python that is running:\n\n"
        f"    {', '.join(missing)}\n\n"
        f"Python in use:\n    {python}\n\n"
        "Install them with this exact command (in a terminal, from the program folder):\n\n"
        f'    "{python}" -m pip install -r requirements.txt\n\n'
        "In Visual Studio Code: Terminal > Run Task > \"Install dependencies\", and check "
        "that the interpreter shown at the bottom right is the one you mean "
        "(Ctrl+Shift+P > \"Python: Select Interpreter\")."
    )


def tkinter_message() -> str:
    return (
        "tkinter (the library that draws the window) is not available in this Python.\n\n"
        "Windows / macOS: install Python from python.org (tkinter is included; on Windows "
        "keep \"tcl/tk and IDLE\" ticked in the installer).\n"
        "Linux: sudo apt install python3-tk\n\n"
        f"Python in use: {sys.executable}"
    )


def show_error(title: str, text: str) -> None:
    """Print the message and, if a window can be opened, show it in a dialog."""
    print(f"\n{title}\n{'-' * len(title)}\n{text}\n", file=sys.stderr)
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
        root.destroy()
    except Exception:  # no tkinter, no display: the message above is enough
        pass
