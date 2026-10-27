#!/usr/bin/env python3
"""Graphical interface for section_infill.py (tkinter, nothing to install).

    python section_infill_gui.py

Choose the model, tick the options you want, fill in the fields, look at the
regions with "Anteprima", then "Genera" writes the files (and the Bambu Studio
project). The command shown at the bottom is the equivalent terminal command.
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
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import section_infill
from infill_sectioning.bambu3mf import pattern_names
from infill_sectioning.gui_model import FOOTPRINTS, MODES, FormState

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


class App(tk.Tk):
    def __init__(self, state: FormState | None = None):
        super().__init__()
        self.title("Sezionamento dell'infill (Shen, Veeramani, Qin 2026)")
        self.geometry("1260x780")
        self.minsize(1100, 720)
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
            ttk.Button(parent, text="Sfoglia…", command=lambda: browse(key)).grid(
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
        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=8)
        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=3)
        main.add(right, weight=2)

        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)
        t1, t2, t3 = (ttk.Frame(nb, padding=12) for _ in range(3))
        nb.add(t1, text=" 1. Pezzo e file ")
        nb.add(t2, text=" 2. Sezionamento ")
        nb.add(t3, text=" 3. Progetto Bambu Studio ")

        # ---- tab 1: files -----------------------------------------------
        t1.columnconfigure(1, weight=1)
        self._row(t1, 0, "Modello (STL o STEP)", "model", width=34, browse=self._pick_model)
        self._row(t1, 1, "Cartella di output", "out_dir", width=34, browse=self._pick_dir)
        self._row(t1, 2, "Prefisso dei file", "prefix", width=18,
                  hint="vuoto = nome del modello")
        ttk.Separator(t1).grid(row=3, column=0, columnspan=4, sticky="we", pady=10)
        ttk.Label(t1, text="Cosa scrivere (il report JSON c'è sempre):").grid(
            row=4, column=0, columnspan=4, sticky="w")
        self._check(t1, 5, "Immagine PNG con le regioni", "write_png")
        self._check(t1, 6, "Un file STL per ogni regione (modifier)", "write_stl")
        self._check(t1, 7, "Progetto Bambu Studio (.3mf) con pezzo e modifier già configurati",
                    "export_3mf")
        ttk.Label(t1, text="Gli STL servono anche per il .3mf: con il .3mf spuntato vengono "
                  "comunque scritti.", foreground="#666", wraplength=420).grid(
            row=8, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # ---- tab 2: sectioning -------------------------------------------
        self._row(t2, 0, "Area massima [mm²]", "max_area", 10,
                  hint="si taglia se l'area la supera…")
        self._row(t2, 1, "Aspect ratio massimo", "max_ar", 10, hint="…o se l'AR la supera")
        self._row(t2, 2, "Angolo 1 [°]", "angle1", 10)
        self._row(t2, 3, "Angolo 2 [°]", "angle2", 10, hint="alternati tra regioni vicine")
        self._check(t2, 4, "Terzo angolo, solo dove due non bastano (giunzioni a T)",
                    "use_extra_angle")
        self.extra_entry = self._row(t2, 5, "   Terzo angolo [°]", "extra_angle", 10,
                                     hint="usato sul minor numero di regioni")
        self.disable_when.append(("use_extra_angle", self.extra_entry))
        ttk.Separator(t2).grid(row=6, column=0, columnspan=4, sticky="we", pady=10)
        ttk.Label(t2, text="Avanzate", font=("TkDefaultFont", 9, "bold")).grid(
            row=7, column=0, sticky="w")
        self._row(t2, 8, "Modalità", "mode", 10, values=list(MODES),
                  hint="bisect = metodo del paper; nsplit = SPERIMENTALE")
        self._row(t2, 9, "Sagoma", "footprint", 10, values=list(FOOTPRINTS),
                  hint="projection = ombra; section = sezione a metà altezza")
        self._row(t2, 10, "Area minima [mm²]", "min_area", 10, hint="blocco di sicurezza")
        self._row(t2, 11, "Profondità massima", "max_depth", 10)
        self._row(t2, 12, "Margine modifier XY [mm]", "margin_xy", 10,
                  hint="sporgenza oltre le pareti esterne")
        self._row(t2, 13, "Margine modifier Z [mm]", "margin_z", 10, hint="sporgenza sopra il pezzo")

        # ---- tab 3: Bambu Studio -------------------------------------------
        t3.columnconfigure(1, weight=1)
        self.tpl_entry = self._row(t3, 0, "Template (.3mf)", "template",
                                   width=30, browse=self._pick_template)
        ttk.Label(t3, text="Stampante, filamento e processo vengono copiati da qui "
                  "(File → Save Project As…, non .gcode.3mf).", foreground="#666",
                  wraplength=460).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Separator(t3).grid(row=2, column=0, columnspan=4, sticky="we", pady=10)
        self._check(t3, 3, "Imposta l'infill sparso in ogni modifier (altrimenti resta quello "
                    "del template)", "set_infill")
        self.density_entry = self._row(t3, 4, "   Densità [%]", "density", 8,
                                       hint="100 = pieno. I layer solidi restano alternati")
        self.pattern_entry = self._row(t3, 5, "   Pattern", "pattern", 20, values=pattern_names(),
                                       hint="rectilinear = verificato sul G-code")
        self.disable_when += [("set_infill", self.density_entry),
                              ("set_infill", self.pattern_entry)]
        self._check(t3, 6, "Ruota il pezzo sul piatto (gli angoli restano 45°/135° rispetto "
                    "al pezzo)", "use_rotation")
        self.rot_entry = self._row(t3, 7, "   Rotazione [°]", "rotation", 8,
                                   hint="antiorario; diagonale H2D ≈ 42.4")
        self.disable_when.append(("use_rotation", self.rot_entry))
        ttk.Label(t3, text="Altre impostazioni dei modifier, una per riga (CHIAVE=VALORE):"
                  ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self.settings_text = tk.Text(t3, height=5, width=44)
        self.settings_text.grid(row=9, column=0, columnspan=4, sticky="we")
        ttk.Label(t3, text="per esempio  top_shell_layers=7   (prevalgono sulle altre opzioni)",
                  foreground="#666").grid(row=10, column=0, columnspan=4, sticky="w")
        self.bambu_widgets = [self.tpl_entry, self.density_entry, self.pattern_entry,
                              self.rot_entry, self.settings_text]

        # ---- right: preview + log ---------------------------------------------
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(right, bg="#fcfcfb", highlightthickness=1,
                                highlightbackground="#bbb", height=330)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _e: self._schedule_redraw())
        self.canvas.create_text(20, 20, anchor="nw", fill="#888", tags="hint",
                                text="Premi «Anteprima» per vedere le regioni\n"
                                     "prima di scrivere i file.")
        self.log = tk.Text(right, height=9, state="disabled", wrap="word", bg="#f7f7f5")
        self.log.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        # ---- bottom: command + buttons --------------------------------------
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(bottom, text="Comando equivalente:").pack(anchor="w")
        self.cmd = tk.Text(bottom, height=3, wrap="word", bg="#f7f7f5", state="disabled")
        self.cmd.pack(fill="x")
        bar = ttk.Frame(bottom)
        bar.pack(fill="x", pady=(6, 0))
        self.btn_prev = ttk.Button(bar, text="Anteprima regioni", command=self.preview)
        self.btn_gen = ttk.Button(bar, text="Genera", command=self.generate)
        self.btn_prev.pack(side="left")
        self.btn_gen.pack(side="left", padx=6)
        ttk.Button(bar, text="Apri cartella di output", command=self.open_out).pack(side="left")
        ttk.Button(bar, text="Copia comando", command=self.copy_cmd).pack(side="right")
        ttk.Button(bar, text="Carica preset…", command=self.load_preset).pack(side="right", padx=6)
        ttk.Button(bar, text="Salva preset…", command=self.save_preset).pack(side="right")
        self.status = ttk.Label(bottom, text="", foreground="#444")
        self.status.pack(anchor="w", pady=(4, 0))

    # ------------------------------------------------------------- callbacks
    def _pick_model(self, key):
        p = filedialog.askopenfilename(title="Modello 3D", filetypes=[
            ("Modelli 3D", "*.stl *.step *.stp *.obj *.ply *.3mf"), ("Tutti i file", "*.*")])
        if p:
            self.v[key].set(p)

    def _pick_template(self, key):
        p = filedialog.askopenfilename(title="Progetto Bambu Studio (.3mf)", filetypes=[
            ("Progetto Bambu Studio", "*.3mf"), ("Tutti i file", "*.*")])
        if p:
            self.v[key].set(p)

    def _pick_dir(self, key):
        p = filedialog.askdirectory(title="Cartella di output")
        if p:
            self.v[key].set(p)

    def _on_change(self):
        s = self.get_state()
        for var, w in self.disable_when:
            on = bool(self.v[var].get()) and (
                var in ("use_extra_angle",) or bool(self.v["export_3mf"].get()))
            self._set_enabled(w, on)
        for w in self.bambu_widgets:
            if not any(w is x for _, x in self.disable_when):
                self._set_enabled(w, bool(self.v["export_3mf"].get()))
        self.cmd.configure(state="normal")
        self.cmd.delete("1.0", "end")
        try:
            self.cmd.insert("1.0", s.command())
        except ValueError:  # a number is being typed
            self.cmd.insert("1.0", "(compila i campi numerici per vedere il comando)")
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
            messagebox.showerror("Controlla i campi", "\n".join(f"• {p}" for p in problems))
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
        self._set_busy(True, "Calcolo delle regioni…")
        self._run(s.to_argv(preview_dir=self.tmp.name), "preview")

    def generate(self):
        if self.busy or not (s := self._check_form()):
            return
        if s.export_3mf and not s.template.strip() and not messagebox.askyesno(
                "Nessun template",
                "Non hai scelto un template: il .3mf non conterrà stampante, filamento e "
                "processo, e Bambu Studio userà i preset selezionati in quel momento.\n\n"
                "Continuare?"):
            return
        self._clear_log()
        self._set_busy(True, "Scrittura dei file…")
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
        self._set_busy(False, ("Fatto: file scritti in " + out) if ok and kind == "generate"
                       else "Anteprima pronta (nessun file scritto)" if ok else
                       "Errore: vedi il messaggio nel riquadro in basso a destra")

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
        self.status.configure(text="Comando copiato negli appunti")

    def save_preset(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Preset", "*.json")],
                                         title="Salva preset")
        if p:
            self.get_state().save(p)
            self.status.configure(text=f"Preset salvato: {p}")

    def load_preset(self):
        p = filedialog.askopenfilename(filetypes=[("Preset", "*.json")], title="Carica preset")
        if p:
            try:
                self.set_state(FormState.load(p))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                messagebox.showerror("Preset non valido", str(exc))
                return
            self._on_change()
            self.status.configure(text=f"Preset caricato: {p}")

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
