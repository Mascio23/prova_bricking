"""Form state of the graphical interface, and its translation into the command line.

No tkinter here: the window (section_infill_gui.py) only edits a FormState, so that
everything that decides *what is run* can be tested without a display. The command
line built here is exactly what section_infill.py receives, and it is shown in the
window so that a run can be repeated (and documented) from the terminal.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .bambu3mf import pattern_key

MODES = ("bisect", "nsplit")
FOOTPRINTS = ("projection", "section")


@dataclass
class FormState:
    # input / output
    model: str = ""
    out_dir: str = "output"
    prefix: str = ""
    write_stl: bool = True
    write_png: bool = True
    export_3mf: bool = True
    # sectioning
    max_area: str = "900"
    max_ar: str = "2"
    angle1: str = "45"
    angle2: str = "135"
    use_extra_angle: bool = False
    extra_angle: str = "90"
    mode: str = "bisect"
    footprint: str = "projection"
    min_area: str = "1"
    max_depth: str = "12"
    margin_xy: str = "2"
    margin_z: str = "1"
    # Bambu Studio project
    template: str = ""
    set_infill: bool = True        # write density + pattern in every modifier
    density: str = "100"
    pattern: str = "rectilinear"
    use_rotation: bool = False
    rotation: str = "0"
    extra_settings: str = ""       # one KEY=VALUE per line
    # advanced
    step_tolerance: str = "0.05"
    raster_pixel: str = "0.1"
    png_dpi: str = "200"

    # ---- presets -------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FormState":
        """Fields of a preset; unknown keys and values of the wrong type are ignored."""
        defaults = cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items()
                      if k in known and isinstance(v, type(getattr(defaults, k)))})

    def save(self, path: str | Path):
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "FormState":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- numbers ---------------------------------------------------------
    @staticmethod
    def num(text: str) -> float:
        """float() that accepts the decimal comma."""
        return float(text.strip().replace(",", "."))

    @staticmethod
    def _txt(text: str) -> str:
        """Number as typed, with the decimal point, for the command line."""
        return text.strip().replace(",", ".")

    def extra_setting_lines(self) -> list[str]:
        return [ln.strip() for ln in self.extra_settings.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    # ---- validation ------------------------------------------------------
    def validate(self, check_files: bool = True) -> list[str]:
        """Human readable problems (Italian); empty list if the form is fine."""
        err = []

        def number(label, text, lo=None, hi=None, lo_open=False, integer=False):
            try:
                v = self.num(text)
            except ValueError:
                err.append(f"{label}: «{text}» non è un numero")
                return None
            if not math.isfinite(v):
                err.append(f"{label}: valore non valido")
                return None
            if integer and v != int(v):
                err.append(f"{label}: deve essere un numero intero")
            if lo is not None and (v <= lo if lo_open else v < lo):
                err.append(f"{label}: deve essere {'maggiore di' if lo_open else 'almeno'} {lo:g}")
            if hi is not None and v > hi:
                err.append(f"{label}: al massimo {hi:g}")
            return v

        if not self.model.strip():
            err.append("Modello: scegli il file STL o STEP")
        elif check_files and not Path(self.model).is_file():
            err.append(f"Modello: il file non esiste ({self.model})")
        if not self.out_dir.strip():
            err.append("Cartella di output: è vuota")
        number("Area massima", self.max_area, 0, lo_open=True)
        number("Aspect ratio massimo", self.max_ar, 1)
        a1 = number("Angolo 1", self.angle1)
        a2 = number("Angolo 2", self.angle2)
        if a1 is not None and a2 is not None and abs(((a1 - a2 + 90) % 180) - 90) < 1e-6:
            err.append("Angolo 1 e angolo 2 coincidono (gli angoli valgono modulo 180°)")
        if self.use_extra_angle:
            e = number("Terzo angolo", self.extra_angle)
            for name, a in (("angolo 1", a1), ("angolo 2", a2)):
                if e is not None and a is not None and abs(((e - a + 90) % 180) - 90) < 1e-6:
                    err.append(f"Terzo angolo: coincide con l'{name}")
        number("Area minima", self.min_area, 0)
        number("Profondità massima", self.max_depth, 1, integer=True)
        number("Margine XY", self.margin_xy, 0)
        number("Margine Z", self.margin_z, 0)
        number("Tolleranza STEP", self.step_tolerance, 0, lo_open=True)
        number("Pixel del controllo raster", self.raster_pixel, 0)
        number("Risoluzione PNG", self.png_dpi, 20, integer=True)
        if self.mode not in MODES:
            err.append(f"Modalità: «{self.mode}» non valida")
        if self.footprint not in FOOTPRINTS:
            err.append(f"Sagoma: «{self.footprint}» non valida")

        if self.export_3mf:
            if self.template.strip() and check_files and not Path(self.template).is_file():
                err.append(f"Template: il file non esiste ({self.template})")
            if self.set_infill:
                number("Densità infill sparso", self.density, 0, 100)
                try:
                    pattern_key(self.pattern)
                except ValueError as exc:
                    err.append(f"Pattern: {exc}")
            if self.use_rotation:
                number("Rotazione sul piatto", self.rotation)
            for ln in self.extra_setting_lines():
                if "=" not in ln or not ln.split("=", 1)[0].strip():
                    err.append(f"Impostazioni extra: «{ln}» non è nella forma CHIAVE=VALORE")
        return err

    # ---- command line ------------------------------------------------------
    def to_argv(self, preview_dir: str | Path | None = None) -> list[str]:
        """Arguments for section_infill.main(). With preview_dir: only the sectioning and a
        small PNG in that folder (no STL, no .3mf), to look at the regions."""
        t = self._txt
        argv = [self.model.strip(), "--out-dir",
                str(preview_dir) if preview_dir else self.out_dir.strip()]
        if self.prefix.strip():
            argv += ["--prefix", self.prefix.strip()]
        argv += ["--max-area", t(self.max_area), "--max-ar", t(self.max_ar),
                 "--angles", t(self.angle1), t(self.angle2)]
        if self.use_extra_angle:
            argv += ["--extra-angle", t(self.extra_angle)]
        defaults = FormState()
        for flag, name in (("--mode", "mode"), ("--footprint", "footprint"),
                           ("--min-area", "min_area"), ("--max-depth", "max_depth"),
                           ("--margin-xy", "margin_xy"), ("--margin-z", "margin_z"),
                           ("--step-tolerance", "step_tolerance"),
                           ("--raster-pixel", "raster_pixel")):
            if t(getattr(self, name)) != t(getattr(defaults, name)):
                argv += [flag, t(getattr(self, name))]
        if preview_dir:
            return argv + ["--no-stl", "--png-dpi", "110"]
        if t(self.png_dpi) != t(defaults.png_dpi):
            argv += ["--png-dpi", t(self.png_dpi)]
        if not self.write_png:
            argv.append("--no-png")
        if not self.write_stl and not self.export_3mf:
            argv.append("--no-stl")
        if self.export_3mf:
            argv.append("--export-3mf")
            if self.template.strip():
                argv += ["--template-3mf", self.template.strip()]
            if self.use_rotation and self.num(self.rotation) != 0:
                argv += ["--plate-rotation", t(self.rotation)]
            if self.set_infill:
                argv += ["--infill-density", t(self.density), "--infill-pattern", self.pattern]
            else:
                argv.append("--inherit-infill")
            for ln in self.extra_setting_lines():
                argv += ["--3mf-setting", ln]
        return argv

    def command(self) -> str:
        """The same run as a terminal command (paths with spaces in double quotes)."""
        def q(a: str) -> str:
            return f'"{a}"' if re.search(r"""[\s'"&;()]""", a) else a
        return "python section_infill.py " + " ".join(q(a) for a in self.to_argv())
