#!/usr/bin/env python3
"""Graphical interface for section_infill.py (tkinter, nothing to install).

    python section_infill_gui.py

Choose the model, tick the options you want, fill in the fields, look at the
regions with "Preview regions", then "Generate" writes the files (and the Bambu
Studio project). The command shown at the bottom is the equivalent terminal command.

In Visual Studio Code: open this file and press the Run button (top right), or F5.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path

from infill_sectioning import deps


def _startup_check() -> None:
    """Explain what is missing (instead of a traceback) before the heavy imports."""
    try:
        __import__("tkinter")
    except ImportError:
        print(deps.tkinter_message(), file=sys.stderr)
        sys.exit(1)
    missing = deps.missing_dependencies()
    if missing:
        deps.show_error("Missing libraries", deps.install_message(missing))
        sys.exit(1)


if __name__ == "__main__":
    _startup_check()

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

import section_infill  # noqa: E402
from infill_sectioning.bambu3mf import pattern_names  # noqa: E402
from infill_sectioning.gui_model import (BLOCK_STYLES, CENTRAL_CHOICES,  # noqa: E402
                                         FOOTPRINTS, MODES, FormState)

LAST_STATE = Path.home() / ".infill_sectioning_gui.json"
PREVIEW_WIDTH = 620  # only used if Pillow is missing (it comes with matplotlib)


class _QueueWriter:
    """Where the output of the program goes while it runs in the background."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(("log", text))
        return len(text)

    def flush(self):
        pass


class ScrollFrame(ttk.Frame):
    """A frame whose content scrolls vertically (mouse wheel or bar) when it is taller than
    the window: the fields go into ``inner``."""

    def __init__(self, parent):
        super().__init__(parent)
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, bg=bg)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas, padding=12)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.bind("<Enter>", self._grab_wheel)
        self.bind("<Leave>", self._release_wheel)
        self._bar_shown = False

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_bar()

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._update_bar()

    def _on_scroll(self, lo, hi):
        self.bar.set(lo, hi)

    def _update_bar(self):
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1
        if need and not self._bar_shown:
            self.bar.pack(side="right", fill="y", before=self.canvas)
            self._bar_shown = True
        elif not need and self._bar_shown:
            self.bar.pack_forget()
            self._bar_shown = False

    def _wheel(self, e):
        if isinstance(e.widget, tk.Text) or not self._bar_shown:
            return
        step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(step * 2, "units")

    def _grab_wheel(self, _e=None):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(seq, self._wheel)

    def _release_wheel(self, _e=None):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_all(seq)


class App(tk.Tk):
    def __init__(self, state: FormState | None = None):
        super().__init__()
        self.title("Infill sectioning (Shen, Veeramani, Qin 2026)")
        # fit the screen (a laptop is not 1260 x 780 high), keep the bottom bar reachable
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1260, sw - 40)}x{min(780, sh - 120)}")
        self.minsize(min(960, sw - 40), min(520, sh - 120))
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.preview_img = None   # what is on the canvas (PhotoImage)
        self.preview_src = None   # the full size PIL image, rescaled when the window resizes
        self._resize_job = None
        self.tmp = tempfile.TemporaryDirectory(prefix="infill_preview_")
        self.v: dict[str, tk.Variable] = {}
        self.disable_when: list[tuple[str, tk.Widget]] = []  # (checkbox var, widget)
        self._build()
        self.set_state(state or self._initial_state())
        for var in self.v.values():
            var.trace_add("write", lambda *_: self._on_change())
        self.settings_text.bind("<KeyRelease>", lambda _e: self._on_change())
        self._on_change()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)

    # ------------------------------------------------------------------ state
    @staticmethod
    def _initial_state() -> FormState:
        try:
            return FormState.load(LAST_STATE)
        except Exception:
            return FormState()

    def get_state(self) -> FormState:
        d = {k: var.get() for k, var in self.v.items()}
        d["extra_settings"] = self.settings_text.get("1.0", "end").strip("\n")
        return FormState.from_dict(d)

    def set_state(self, s: FormState):
        for k, var in self.v.items():
            var.set(getattr(s, k))
        self.settings_text.delete("1.0", "end")
        self.settings_text.insert("1.0", s.extra_settings)

    # ---------------------------------------------------------------- widgets
    def _var(self, key, kind="str"):
        var = tk.BooleanVar() if kind == "bool" else tk.StringVar()
        self.v[key] = var
        return var

    def _row(self, parent, r, label, key, width=12, hint="", browse=None, values=None):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
        if values:
            w = ttk.Combobox(parent, textvariable=self._var(key), values=values, width=width,
                             state="readonly")
        else:
            w = ttk.Entry(parent, textvariable=self._var(key), width=width)
        w.grid(row=r, column=1, sticky="we" if browse else "w", pady=3)
        if browse:
            ttk.Button(parent, text="Browse…", command=lambda: browse(key)).grid(
                row=r, column=2, padx=(6, 0))
        if hint:
            ttk.Label(parent, text=hint, foreground="#666", wraplength=230,
                      justify="left").grid(row=r, column=3 if browse else 2, sticky="w", padx=8)
        return w

    def _check(self, parent, r, label, key, span=4, hint=""):
        c = ttk.Checkbutton(parent, text=label, variable=self._var(key, "bool"))
        c.grid(row=r, column=0, columnspan=span, sticky="w", pady=3)
        return c

    def _build(self):
        # The bar with the command and the buttons is packed FIRST (at the bottom), so it is
        # always visible whatever the height of the window; the tabs scroll instead.
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        ttk.Label(bottom, text="Equivalent command:").pack(anchor="w")
        cmdbox = ttk.Frame(bottom)
        cmdbox.pack(fill="x")
        cmdbar = ttk.Scrollbar(cmdbox, orient="vertical")
        self.cmd = tk.Text(cmdbox, height=3, wrap="word", bg="#f7f7f5", state="disabled",
                           yscrollcommand=cmdbar.set)
        cmdbar.configure(command=self.cmd.yview)
        cmdbar.pack(side="right", fill="y")
        self.cmd.pack(side="left", fill="x", expand=True)
        bar = ttk.Frame(bottom)
        bar.pack(fill="x", pady=(6, 0))
        self.btn_prev = ttk.Button(bar, text="Preview regions", command=self.preview)
        self.btn_gen = ttk.Button(bar, text="Generate", command=self.generate)
        self.btn_prev.pack(side="left")
        self.btn_gen.pack(side="left", padx=6)
        ttk.Button(bar, text="Open output folder", command=self.open_out).pack(side="left")
        ttk.Button(bar, text="Copy command", command=self.copy_cmd).pack(side="right")
        ttk.Button(bar, text="Load preset…", command=self.load_preset).pack(side="right", padx=6)
        ttk.Button(bar, text="Save preset…", command=self.save_preset).pack(side="right")
        self.status = ttk.Label(bottom, text="", foreground="#444")
        self.status.pack(anchor="w", pady=(4, 0))

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=3)
        main.add(right, weight=2)

        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)
        tabs = [ScrollFrame(nb) for _ in range(3)]
        nb.add(tabs[0], text=" 1. Part and files ")
        nb.add(tabs[1], text=" 2. Sectioning ")
        nb.add(tabs[2], text=" 3. Bambu Studio project ")
        t1, t2, t3 = (t.inner for t in tabs)   # the fields go in the scrollable part
        self.notebook = nb

        # ---- tab 1: files -----------------------------------------------
        t1.columnconfigure(1, weight=1)
        self._row(t1, 0, "Model (STL or STEP)", "model", width=34, browse=self._pick_model)
        self._row(t1, 1, "Output folder", "out_dir", width=34, browse=self._pick_dir)
        self._row(t1, 2, "File prefix", "prefix", width=18,
                  hint="empty = name of the model")
        ttk.Separator(t1).grid(row=3, column=0, columnspan=4, sticky="we", pady=10)
        ttk.Label(t1, text="What to write (the JSON report is always written):").grid(
            row=4, column=0, columnspan=4, sticky="w")
        self._check(t1, 5, "PNG image with the regions (clean: region numbers only)", "write_png")
        self._check(t1, 6, "PNG image with details (angle, area, aspect ratio, ellipses)",
                    "write_png_details")
        self._check(t1, 7, "One STL file per region (modifiers)", "write_stl")
        self._check(t1, 8, "Bambu Studio project (.3mf) with part and modifiers already set up",
                    "export_3mf")
        ttk.Label(t1, text="The STLs are also needed for the .3mf: they are written anyway "
                  "when the .3mf is ticked.", foreground="#666", wraplength=420).grid(
            row=9, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # ---- tab 2: sectioning -------------------------------------------
        self._row(t2, 0, "Maximum area [mm²]", "max_area", 10,
                  hint="a region is cut if its area exceeds this…")
        self._row(t2, 1, "Maximum aspect ratio", "max_ar", 10, hint="…or if its aspect ratio exceeds this")
        self._row(t2, 2, "Angle 1 [°]", "angle1", 10)
        self._row(t2, 3, "Angle 2 [°]", "angle2", 10, hint="alternated between neighbouring regions")
        self._check(t2, 4, "Third angle, only where two are not enough (T-junctions)",
                    "use_extra_angle")
        self.extra_entry = self._row(t2, 5, "   Third angle [°]", "extra_angle", 10,
                                     hint="used on as few regions as possible")
        self.disable_when.append(("use_extra_angle", self.extra_entry))
        self._check(t2, 6, "Fourth angle, only if three are not enough", "use_extra_angle2")
        self.extra_entry2 = self._row(t2, 7, "   Fourth angle [°]", "extra_angle2", 10,
                                      hint="a region ringed by an odd number of neighbours "
                                           "needs it")
        self.disable_when.append(("use_extra_angle2", self.extra_entry2))
        ttk.Separator(t2).grid(row=8, column=0, columnspan=4, sticky="we", pady=10)
        ttk.Label(t2, text="Advanced", font=("TkDefaultFont", 9, "bold")).grid(
            row=9, column=0, sticky="w")
        self._row(t2, 10, "Mode", "mode", 10, values=list(MODES),
                  hint="bisect = method of the paper; nsplit = EXPERIMENTAL, simple elongated shapes only")
        self._row(t2, 11, "Footprint", "footprint", 10, values=list(FOOTPRINTS),
                  hint="projection = shadow; section = cross-section at mid height")
        self._row(t2, 12, "Minimum area [mm²]", "min_area", 10, hint="safety stop")
        self._row(t2, 13, "Minimum region width [mm]", "min_width", 10,
                  hint="narrower slivers are merged into the neighbours; 0 = off")
        self._row(t2, 14, "Minimum shared edge [mm]", "min_contact", 10,
                  hint="shorter contacts are corners: not neighbours when assigning angles")
        self._row(t2, 15, "Maximum depth", "max_depth", 10)
        self._row(t2, 16, "Modifier margin XY [mm]", "margin_xy", 10,
                  hint="overhang beyond the outer walls")
        self._row(t2, 17, "Modifier margin Z [mm]", "margin_z", 10, hint="overhang above the part")

        # ---- tab 3: Bambu Studio -------------------------------------------
        t3.columnconfigure(1, weight=1)
        self.tpl_entry = self._row(t3, 0, "Template (.3mf)", "template",
                                   width=30, browse=self._pick_template)
        ttk.Label(t3, text="Printer, filament and process are copied from here "
                  "(File → Save Project As…, not .gcode.3mf).", foreground="#666",
                  wraplength=460).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Separator(t3).grid(row=2, column=0, columnspan=4, sticky="we", pady=10)
        self._check(t3, 3, "Set the sparse infill in every modifier (otherwise the "
                    "template's stays)", "set_infill")
        self.density_entry = self._row(t3, 4, "   Density [%]", "density", 8,
                                       hint="100 = solid. The solid layers stay alternated. With "
                                            "\"only top and bottom\" below: the infill of the middle")
        self.pattern_entry = self._row(t3, 5, "   Pattern", "pattern", 20, values=pattern_names(),
                                       hint="rectilinear = verified on the G-code")
        self.disable_when += [("set_infill", self.density_entry),
                              ("set_infill", self.pattern_entry)]
        self._check(t3, 6, "Rotate the part on the plate (angles stay 45°/135° relative "
                    "to the part)", "use_rotation")
        self.rot_entry = self._row(t3, 7, "   Rotation [°]", "rotation", 8,
                                   hint="counter-clockwise; H2D diagonal ≈ 42.4")
        self.disable_when.append(("use_rotation", self.rot_entry))
        self.wall_entry = self._row(t3, 8, "Wall loops", "wall_loops", 8,
                                    hint="number of walls of the specimen; empty = the "
                                         "template's")
        ttk.Separator(t3).grid(row=9, column=0, columnspan=4, sticky="we", pady=8)
        self._check(t3, 10, "Modifiers only on the top and bottom layers (the middle keeps a "
                    "continuous infill)", "use_shell_modifiers")
        self.bottom_entry = self._row(t3, 11, "   Bottom layers", "bottom_layers", 8,
                                      hint="solid layers at the bottom; the modifier is this many layers tall")
        self.top_entry = self._row(t3, 12, "   Top layers", "top_layers", 8,
                                   hint="solid layers at the top; the modifier is this many layers tall")
        self.lh_entry = self._row(t3, 13, "   Layer height [mm]", "layer_height", 8,
                                  hint="empty = the template's (else 0.2). Must be the one you slice with")
        self.flh_entry = self._row(t3, 14, "   First layer height [mm]", "first_layer_height", 8,
                                   hint="empty = the template's (else the layer height)")
        self.central_entry = self._row(t3, 15, "   Central modifier", "central_modifier", 8,
                                       values=list(CENTRAL_CHOICES),
                                       hint="auto = yes for a plain extrusion, no if the part has "
                                            "steps or pockets inside. Its density and pattern are "
                                            "the ones above")
        self.disable_when += [("use_shell_modifiers", w) for w in (
            self.bottom_entry, self.top_entry, self.lh_entry, self.flh_entry,
            self.central_entry)]
        self.style_entry = self._row(t3, 16, "   Blocks as", "blocks_as", 8,
                                     values=list(BLOCK_STYLES),
                                     hint="infill = autonomous zones (100% rectilinear, "
                                          "no top/bottom shell): works with any number of "
                                          "walls. shells = solid layers made by Bambu "
                                          "(OK with 2 walls)")
        self.blockwall_entry = self._row(t3, 17, "   Walls of the blocks", "block_wall_loops", 8,
                                         hint="walls around each zone (infill mode); "
                                              "\"Wall loops\" above applies to the middle")
        self.disable_when += [("use_shell_modifiers", self.style_entry),
                              ("use_shell_modifiers", self.blockwall_entry)]
        ttk.Label(t3, text="Other modifier settings, one per line (KEY=VALUE):"
                  ).grid(row=18, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self.settings_text = tk.Text(t3, height=4, width=44)
        self.settings_text.grid(row=19, column=0, columnspan=4, sticky="we")
        ttk.Label(t3, text="for example  top_shell_layers=7   (they override the other options)",
                  foreground="#666").grid(row=20, column=0, columnspan=4, sticky="w")
        self.bambu_widgets = [self.tpl_entry, self.density_entry, self.pattern_entry,
                              self.rot_entry, self.wall_entry, self.settings_text]

        # ---- right: preview + log ---------------------------------------------
        # preview above, log below: drag the bar between them to give room to either
        split = ttk.PanedWindow(right, orient="vertical")
        split.pack(fill="both", expand=True)
        top = ttk.Frame(split)
        low = ttk.Frame(split)
        split.add(top, weight=3)
        split.add(low, weight=2)
        self.split = split
        self.canvas = tk.Canvas(top, bg="#fcfcfb", highlightthickness=1,
                                highlightbackground="#bbb", height=160)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._schedule_redraw())
        self.canvas.create_text(20, 20, anchor="nw", fill="#888", tags="hint",
                                text="Press \"Preview regions\" to see the regions\n"
                                     "before any file is written.")
        logbar = ttk.Scrollbar(low, orient="vertical")
        self.log = tk.Text(low, height=5, state="disabled", wrap="word", bg="#f7f7f5",
                           yscrollcommand=logbar.set)
        logbar.configure(command=self.log.yview)
        logbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------- callbacks
    def _pick_model(self, key):
        p = filedialog.askopenfilename(title="3D model", filetypes=[
            ("3D models", "*.stl *.step *.stp *.obj *.ply *.3mf"), ("All files", "*.*")])
        if p:
            self.v[key].set(p)

    def _pick_template(self, key):
        p = filedialog.askopenfilename(title="Bambu Studio project (.3mf)", filetypes=[
            ("Bambu Studio project", "*.3mf"), ("All files", "*.*")])
        if p:
            self.v[key].set(p)

    def _pick_dir(self, key):
        p = filedialog.askdirectory(title="Output folder")
        if p:
            self.v[key].set(p)

    def _on_change(self):
        s = self.get_state()
        for var, w in self.disable_when:
            on = bool(self.v[var].get()) and (
                var in ("use_extra_angle", "use_extra_angle2") or bool(self.v["export_3mf"].get()))
            self._set_enabled(w, on)
        for w in self.bambu_widgets:
            if not any(w is x for _, x in self.disable_when):
                self._set_enabled(w, bool(self.v["export_3mf"].get()))
        self.cmd.configure(state="normal")
        self.cmd.delete("1.0", "end")
        try:
            self.cmd.insert("1.0", s.command())
        except ValueError:  # a number is being typed
            self.cmd.insert("1.0", "(fill in the numeric fields to see the command)")
        self.cmd.configure(state="disabled")

    @staticmethod
    def _set_enabled(w, on):
        if isinstance(w, ttk.Combobox):
            w.configure(state="readonly" if on else "disabled")
        elif isinstance(w, tk.Text):
            w.configure(state="normal" if on else "disabled")
        else:
            w.configure(state="normal" if on else "disabled")

    def _check_form(self) -> FormState | None:
        s = self.get_state()
        problems = s.validate()
        if problems:
            messagebox.showerror("Check the fields", "\n".join(f"• {p}" for p in problems))
            return None
        return s

    def _write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy, msg=""):
        self.busy = busy
        for b in (self.btn_prev, self.btn_gen):
            b.configure(state="disabled" if busy else "normal")
        self.status.configure(text=msg)

    def _run(self, argv, kind):
        """Run section_infill.main in a thread, send its output to the log."""
        def work():
            w = _QueueWriter(self.q)
            try:
                with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
                    rc = section_infill.main(argv)
                self.q.put(("done", kind, rc, ""))
            except SystemExit as e:
                self.q.put(("done", kind, 1, str(e.code) if e.code not in (None, 0) else ""))
            except Exception:
                self.q.put(("done", kind, 1, traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def preview(self):
        if self.busy or not (s := self._check_form()):
            return
        self._clear_log()
        self._set_busy(True, "Computing the regions…")
        self._run(s.to_argv(preview_dir=self.tmp.name), "preview")

    def generate(self):
        if self.busy or not (s := self._check_form()):
            return
        if s.export_3mf and not s.template.strip() and not messagebox.askyesno(
                "No template",
                "No template chosen: the .3mf will not contain printer, filament and "
                "process, and Bambu Studio will use the presets selected at that moment.\n\n"
                "Continue?"):
            return
        self._clear_log()
        self._set_busy(True, "Writing the files…")
        self._run(s.to_argv(), "generate")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    self._write_log(msg[1])
                else:
                    self._finished(*msg[1:])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finished(self, kind, rc, err):
        if err:
            self._write_log(("\n" if not err.startswith("error") else "") + err + "\n")
        ok = rc == 0 and not (err and "Traceback" in err)
        if kind == "preview" and ok:
            self._show_preview()
        out = self.get_state().out_dir
        self._set_busy(False, ("Done: files written to " + out) if ok and kind == "generate"
                       else "Preview ready (no file written)" if ok else
                       "Error: see the message in the box at the bottom right")

    def _show_preview(self):
        pngs = sorted(Path(self.tmp.name).glob("*_sections.png"), key=os.path.getmtime)
        if not pngs:
            return
        try:
            from PIL import Image
            self.preview_src = Image.open(pngs[-1]).convert("RGB")
        except ImportError:  # no Pillow: Tk can read the PNG itself, only coarsely scaled
            img = tk.PhotoImage(file=str(pngs[-1]))
            f = max(1, -(-img.width() // PREVIEW_WIDTH))
            self.preview_src = None
            self.preview_img = img.subsample(f, f) if f > 1 else img
            self.canvas.delete("all")
            self.canvas.create_image(4, 4, anchor="nw", image=self.preview_img)
            return
        self._redraw_preview()

    def _schedule_redraw(self):
        if self.preview_src is not None:
            if self._resize_job:
                self.after_cancel(self._resize_job)
            self._resize_job = self.after(120, self._redraw_preview)

    def _redraw_preview(self):
        """Fit the preview to the width of the canvas."""
        self._resize_job = None
        if self.preview_src is None:
            return
        from PIL import Image, ImageTk
        w = max(240, self.canvas.winfo_width() - 8)
        h = round(self.preview_src.height * w / self.preview_src.width)
        img = self.preview_src.resize((w, h), Image.LANCZOS)
        self.preview_img = ImageTk.PhotoImage(img, master=self)
        self.canvas.delete("all")
        self.canvas.create_image(4, 4, anchor="nw", image=self.preview_img)
        self.canvas.configure(scrollregion=(0, 0, w + 8, h + 8))

    def open_out(self):
        path = Path(self.get_state().out_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(self.get_state().command())
        self.status.configure(text="Command copied to the clipboard")

    def save_preset(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Preset", "*.json")],
                                         title="Save preset")
        if p:
            self.get_state().save(p)
            self.status.configure(text=f"Preset saved: {p}")

    def load_preset(self):
        p = filedialog.askopenfilename(filetypes=[("Preset", "*.json")], title="Load preset")
        if p:
            try:
                self.set_state(FormState.load(p))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                messagebox.showerror("Invalid preset", str(exc))
                return
            self._on_change()
            self.status.configure(text=f"Preset loaded: {p}")

    def _close(self):
        with contextlib.suppress(Exception):
            self.get_state().save(LAST_STATE)
        self.tmp.cleanup()
        self.destroy()


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
