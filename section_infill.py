#!/usr/bin/env python3
"""Infill sectioning for FFF warpage mitigation (Shen, Veeramani, Qin, 2026).

Example:
    python section_infill.py provino.stl --max-area 900 --max-ar 2.0 --out-dir ./output
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from infill_sectioning import __version__
from infill_sectioning.bambu3mf import Volume, build_3mf, pattern_warning, read_template, write_3mf
from infill_sectioning.export import build_report, write_json, write_modifier_stls, write_png
from infill_sectioning.footprint import extract_footprint, load_mesh
from infill_sectioning.moments import polygon_moments, raster_moments
from infill_sectioning.sectioning import section


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Split the XY footprint of a 2.5D part into sub-regions with "
                    "alternating infill direction (Shen et al., 2026).")
    p.add_argument("model", type=Path, help="input model (.stl, .step/.stp, .obj, .3mf ...)")
    p.add_argument("--out-dir", type=Path, default=Path("output"))
    p.add_argument("--max-area", type=float, default=900.0,
                   help="max sub-region area [mm^2] (default 900)")
    p.add_argument("--max-ar", type=float, default=2.0,
                   help="max equivalent-ellipse aspect ratio (default 2.0)")
    p.add_argument("--mode", choices=["bisect", "nsplit"], default="bisect",
                   help="bisect = method of the paper (default); "
                        "nsplit = EXPERIMENTAL equal-width N slabs, for comparison only")
    p.add_argument("--angles", type=float, nargs=2, default=[45.0, 135.0],
                   metavar=("A1", "A2"), help="the two infill angles [deg] (default 45 135)")
    p.add_argument("--min-area", type=float, default=1.0,
                   help="never create regions smaller than this [mm^2] (safety stop)")
    p.add_argument("--max-depth", type=int, default=12, help="max recursion depth")
    p.add_argument("--footprint", choices=["projection", "section"], default="projection",
                   help="footprint extraction method (default projection)")
    p.add_argument("--margin-xy", type=float, default=2.0,
                   help="modifier overhang beyond the part's outer walls [mm]")
    p.add_argument("--margin-z", type=float, default=1.0,
                   help="modifier overhang above the part's top [mm]")
    p.add_argument("--step-tolerance", type=float, default=0.05,
                   help="STEP tessellation linear deflection [mm]")
    p.add_argument("--raster-pixel", type=float, default=0.1,
                   help="pixel size [mm] for the cv2.moments cross-check (0 = skip)")
    p.add_argument("--prefix", default=None, help="file name prefix (default: model name)")
    p.add_argument("--export-3mf", action="store_true",
                   help="also write a Bambu Studio project (.3mf) with the part and the "
                        "modifiers, infill_direction already set on each modifier")
    p.add_argument("--template-3mf", type=Path, default=None,
                   help="Bambu Studio project (.3mf, File > Save Project) whose printer/"
                        "filament/process settings are copied into the exported .3mf")
    p.add_argument("--3mf-setting", dest="mod_settings", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="extra per-modifier override written into the .3mf, repeatable "
                        "(e.g. sparse_infill_pattern=zig-zag, sparse_infill_density=100%%)")
    p.add_argument("--no-stl", action="store_true", help="do not write modifier STLs")
    p.add_argument("--no-png", action="store_true", help="do not write the PNG preview")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def run(args) -> dict:
    mesh = load_mesh(args.model, step_tolerance=args.step_tolerance)
    part = extract_footprint(mesh, args.footprint)
    z_min, z_max = float(mesh.bounds[0, 2]), float(mesh.bounds[1, 2])

    moments_check = None
    if args.raster_pixel > 0:
        vm, rm = polygon_moments(part), raster_moments(part, args.raster_pixel)
        moments_check = {
            "raster_pixel_mm": args.raster_pixel,
            "vector": {"area_mm2": round(vm.area, 3), "aspect_ratio": round(vm.aspect_ratio, 4),
                       "major_axis_deg": round(vm.major_axis_deg, 3)},
            "raster_cv2": {"area_mm2": round(rm.area, 3), "aspect_ratio": round(rm.aspect_ratio, 4),
                           "major_axis_deg": round(rm.major_axis_deg, 3)},
            "area_rel_diff": round(abs(rm.area - vm.area) / vm.area, 6),
            "ar_rel_diff": round(abs(rm.aspect_ratio - vm.aspect_ratio) / vm.aspect_ratio, 6),
        }

    result = section(part, args.max_area, args.max_ar, mode=args.mode,
                     min_area=args.min_area, max_depth=args.max_depth,
                     angles=tuple(args.angles))

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    prefix = args.prefix or args.model.stem

    mod_files = None
    if not args.no_stl or args.export_3mf:
        mod_files = write_modifier_stls(result, out, z_min, z_max, args.margin_xy,
                                        args.margin_z, prefix=f"{prefix}_modifier")

    params = {
        "mode": args.mode, "max_area_mm2": args.max_area, "max_ar": args.max_ar,
        "angles_deg": list(args.angles), "min_area_mm2": args.min_area,
        "max_depth": args.max_depth, "footprint_method": args.footprint,
        "margin_xy_mm": args.margin_xy, "margin_z_mm": args.margin_z,
    }
    input_info = {
        "file": str(args.model), "n_triangles": int(len(mesh.faces)),
        "bounds_mm": [[round(float(v), 4) for v in row] for row in mesh.bounds],
        "z_range_mm": [round(z_min, 4), round(z_max, 4)],
    }
    report = build_report(result, params, input_info, mod_files, moments_check)
    if mod_files:
        # Helps placing a modifier by hand (Bambu Studio "Add modifier -> Load"):
        # offset of each modifier's bounding-box centre from the part's one.
        part_c = mesh.bounds.mean(axis=0)
        for entry in report["regions"]:
            x0, y0, x1, y1 = entry["modifier_bbox"]
            c = [(x0 + x1) / 2, (y0 + y1) / 2, (z_min + z_max + args.margin_z) / 2]
            entry["modifier_center_offset_from_part_center_mm"] = [
                round(float(c[k] - part_c[k]), 4) for k in range(3)]
    write_json(report, out / f"{prefix}_sections.json")

    if args.export_3mf:
        report["bambu_3mf"] = export_project(args, mesh, result, mod_files, out, prefix)
        write_json(report, out / f"{prefix}_sections.json")

    if not args.no_png:
        title = (f"{args.model.name} — {len(result.regions)} regions  "
                 f"(mode={args.mode}, max area={args.max_area:g} mm², max AR={args.max_ar:g})")
        write_png(result, out / f"{prefix}_sections.png", title, mod_files)
    return report


def parse_settings(items) -> dict:
    settings = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--3mf-setting expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        settings[k.strip()] = v.strip()
    return settings


def export_project(args, mesh, result, mod_files, out, prefix) -> dict:
    extra = parse_settings(args.mod_settings)
    template = read_template(args.template_3mf) if args.template_3mf else None
    part_name = args.model.with_suffix(".stl").name
    volumes = [Volume(part_name, mesh, "normal_part")]
    for r, (name, _, mmesh) in zip(result.regions, mod_files):
        settings = {"infill_direction": f"{r.angle:g}", **extra}
        volumes.append(Volume(name, mmesh, "modifier_part", settings))
    path = out / f"{prefix}.3mf"
    write_3mf(path, build_3mf(volumes, prefix, template))
    warning = pattern_warning(template, extra)
    if warning:
        print("WARNING:", warning)
    if template is None:
        print("NOTE: no --template-3mf given: the .3mf has no printer/filament/process "
              "settings, Bambu Studio will use the presets currently selected.")
    return {"file": path.name, "template": str(args.template_3mf) if template else None,
            "modifier_settings": extra, "warning": warning}


def main(argv=None) -> int:
    args = parse_args(argv)
    report = run(args)
    s = report["summary"]
    print(f"{s['n_regions']} regions, all within thresholds: {s['all_within_thresholds']}")
    for r in report["regions"]:
        print(f"  R{r['id']:<2d} angle={r['infill_angle_deg']:>5g}°  "
              f"area={r['area_mm2']:9.2f} mm²  AR={r['aspect_ratio']:5.2f}  "
              f"bbox={r['bbox']}")
    if s["adjacency_conflicts"]:
        print("WARNING: adjacent regions with the same angle:", s["adjacency_conflicts"])
    if report["parameters"]["mode"] == "nsplit":
        print("NOTE: nsplit is EXPERIMENTAL and is not the method of Shen et al.")
    print(f"output written to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
