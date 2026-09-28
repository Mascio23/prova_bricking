"""Outputs: JSON report, PNG preview, modifier STL files."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import LineString, MultiPolygon

from . import __version__
from .sectioning import SectioningResult, coverage_error, modifier_footprints

# Two-slot categorical palette (validated reference palette, slots 1-2);
# the hatch direction is a second, colour-independent encoding of the angle.
ANGLE_COLORS = ["#2a78d6", "#eb6834"]
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"


def _polys(geom):
    return list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]


def _poly_json(geom, nd=4):
    def ring(r):
        return [[round(x, nd), round(y, nd)] for x, y in r.coords]

    return [{"exterior": ring(p.exterior), "holes": [ring(h) for h in p.interiors]}
            for p in _polys(geom)]


def _bbox(geom, nd=4):
    return [round(v, nd) for v in geom.bounds]


def _r(v, nd=4):
    return round(float(v), nd)


# --------------------------------------------------------------------------- #
# STL
# --------------------------------------------------------------------------- #
def extrude(geom, z0: float, z1: float) -> trimesh.Trimesh:
    parts = []
    for p in _polys(geom):
        m = trimesh.creation.extrude_polygon(p, height=z1 - z0)
        m.apply_translation([0, 0, z0])
        parts.append(m)
    return trimesh.util.concatenate(parts)


def write_modifier_stls(result: SectioningResult, out_dir: Path, z_min: float,
                        z_max: float, margin_xy: float, margin_z: float,
                        prefix: str = "modifier"):
    """One STL per region, in the SAME coordinate frame as the input model.

    Z range: [z_min, z_max + margin_z]. The modifier is not extended below the
    part: nothing is printed there, and a modifier below the bed could shift
    the object when Bambu Studio drops it onto the plate.
    """
    files = []
    for r, fp in zip(result.regions, modifier_footprints(result, margin_xy)):
        name = f"{prefix}_{r.id:02d}_{int(round(r.angle))}deg.stl"
        mesh = extrude(fp, z_min, z_max + margin_z)
        mesh.export(out_dir / name)
        files.append((name, fp))
    return files


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def build_report(result: SectioningResult, params: dict, input_info: dict,
                 modifier_files=None, moments_check: dict | None = None) -> dict:
    pm = result.part_moments
    cov = coverage_error(result)
    regions = []
    for r in result.regions:
        m = r.moments
        a, b = m.semi_axes
        entry = {
            "id": r.id,
            "infill_angle_deg": r.angle,
            "area_mm2": _r(r.area, 3),
            "aspect_ratio": _r(r.aspect_ratio),
            "centroid": [_r(m.cx), _r(m.cy)],
            "major_axis_deg": _r(m.major_axis_deg, 3),
            "equiv_ellipse_semi_axes_mm": [_r(a), _r(b)],
            "bbox": _bbox(r.geom),
            "depth": r.depth,
            "split_path": r.path,
            "within_thresholds": r.within_thresholds,
            "stop_reason": r.stop_reason,
            "neighbors": sorted(r.neighbors),
            "polygon": _poly_json(r.geom),
        }
        if modifier_files:
            name, fp = modifier_files[r.id]
            entry["modifier_stl"] = name
            entry["modifier_bbox"] = _bbox(fp)
        regions.append(entry)

    a, b = pm.semi_axes
    report = {
        "tool": "section_infill",
        "version": __version__,
        "method": ("Shen, Veeramani, Qin (2026) recursive bisection along the minor axis"
                   if result.mode == "bisect" else
                   "EXPERIMENTAL equal-width N-split (not the method of Shen et al.)"),
        "input": input_info,
        "parameters": params,
        "part": {
            "area_mm2": _r(pm.area, 3),
            "centroid": [_r(pm.cx), _r(pm.cy)],
            "aspect_ratio": _r(pm.aspect_ratio),
            "major_axis_deg": _r(pm.major_axis_deg, 3),
            "equiv_ellipse_semi_axes_mm": [_r(a), _r(b)],
            "bbox": _bbox(result.part),
        },
        "summary": {
            "n_regions": len(result.regions),
            "all_within_thresholds": all(r.within_thresholds for r in result.regions),
            "angle_counts": {str(ang): sum(1 for r in result.regions if r.angle == ang)
                             for ang in sorted({r.angle for r in result.regions})},
            "adjacency_conflicts": [
                {"regions": [i, j], "shared_length_mm": _r(w, 3)}
                for i, j, w in result.conflicts],
            "area_check": {k: _r(v, 6) for k, v in cov.items()},
        },
        "regions": regions,
    }
    if moments_check:
        report["part"]["moments_check"] = moments_check
    return report


def write_json(report: dict, path: Path):
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #
def _hatch_lines(geom, angle_deg, spacing):
    """Parallel lines at ``angle_deg`` clipped to geom."""
    minx, miny, maxx, maxy = geom.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    L = np.hypot(maxx - minx, maxy - miny)
    d = np.array([np.cos(np.radians(angle_deg)), np.sin(np.radians(angle_deg))])
    nrm = np.array([-d[1], d[0]])
    segs = []
    for s in np.arange(-L / 2, L / 2 + spacing, spacing):
        p = np.array([cx, cy]) + nrm * s
        clipped = geom.intersection(LineString([p - d * L, p + d * L]))
        for g in getattr(clipped, "geoms", [clipped]):
            if isinstance(g, LineString) and not g.is_empty:
                segs.append(np.asarray(g.coords))
    return segs


def write_png(result: SectioningResult, path: Path, title: str = "",
              modifier_files=None, dpi: int = 200):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.lines import Line2D
    from matplotlib.patches import Ellipse, Patch
    from matplotlib.path import Path as MPath
    from matplotlib.patches import PathPatch

    def patch(poly, **kw):
        verts, codes = [], []
        for ring in [poly.exterior, *poly.interiors]:
            xy = np.asarray(ring.coords)
            verts.extend(xy)
            codes.extend([MPath.MOVETO] + [MPath.LINETO] * (len(xy) - 2) + [MPath.CLOSEPOLY])
        return PathPatch(MPath(verts, codes), **kw)

    minx, miny, maxx, maxy = result.part.bounds
    w, h = maxx - minx, maxy - miny
    fig_w = 10.0
    fig_h = max(3.0, min(10.0, fig_w * (h + 0.3 * w) / max(w, 1e-9) * 0.9))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")

    angles = sorted({r.angle for r in result.regions})
    color_of = {a: ANGLE_COLORS[i % 2] for i, a in enumerate(angles)}
    spacing = max(w, h) / 60

    # modifier outlines (dashed, muted)
    if modifier_files:
        for _, fp in modifier_files:
            for p in _polys(fp):
                ax.add_patch(patch(p, facecolor="none", edgecolor=MUTED, lw=0.6,
                                   ls=(0, (3, 2)), zorder=1))

    for r in result.regions:
        c = color_of[r.angle]
        for p in _polys(r.geom):
            ax.add_patch(patch(p, facecolor=c, alpha=0.28, edgecolor="none", zorder=2))
        segs = _hatch_lines(r.geom, r.angle, spacing)
        ax.add_collection(LineCollection(segs, colors=c, linewidths=0.9, zorder=3))
        for p in _polys(r.geom):
            ax.add_patch(patch(p, facecolor="none", edgecolor="#fcfcfb", lw=2.0, zorder=4))
        # equivalent ellipse of the region (dashed)
        a, b = r.moments.semi_axes
        ax.add_patch(Ellipse((r.moments.cx, r.moments.cy), 2 * a, 2 * b,
                             angle=r.moments.major_axis_deg, fill=False,
                             edgecolor=INK_2, lw=0.7, ls=(0, (1, 1.5)), zorder=5))
        rp = r.geom.representative_point() if not r.geom.contains(
            r.geom.centroid) else r.geom.centroid
        flag = "" if r.within_thresholds else " (!)"
        ax.text(rp.x, rp.y,
                f"R{r.id}{flag}\n{r.angle:g}°\nA={r.area:.0f} mm²\nAR={r.aspect_ratio:.2f}",
                ha="center", va="center", fontsize=7, color=INK, zorder=6,
                bbox=dict(boxstyle="round,pad=0.25", fc="#fcfcfb", ec="none", alpha=0.85))

    for p in _polys(result.part):
        ax.add_patch(patch(p, facecolor="none", edgecolor=INK, lw=1.2, zorder=7))
    for a_, b_ in result.cuts:
        ax.plot([a_[0], b_[0]], [a_[1], b_[1]], color=INK, lw=0.8, ls="--", zorder=8)
    ax.plot(*result.part_moments.centroid, marker="+", color=INK, ms=8, zorder=9)

    handles = [Patch(facecolor=color_of[a], alpha=0.5, edgecolor=color_of[a],
                     hatch="//" if a < 90 else "\\\\", label=f"infill {a:g}°")
               for a in angles]
    handles += [Line2D([], [], color=INK, ls="--", lw=0.8, label="cut (minor axis)"),
                Line2D([], [], color=INK_2, ls=":", lw=0.8, label="equivalent ellipse")]
    if modifier_files:
        handles.append(Line2D([], [], color=MUTED, ls="--", lw=0.6, label="modifier outline"))
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.12),
              ncol=len(handles), frameon=False, fontsize=8, labelcolor=INK_2)

    pad = 0.08 * max(w, h)
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal")
    ax.set_xlabel("X [mm]", color=INK_2, fontsize=9)
    ax.set_ylabel("Y [mm]", color=INK_2, fontsize=9)
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ax.spines.values():
        s.set_color(MUTED)
        s.set_linewidth(0.6)
    ax.grid(True, color="#e8e7e3", lw=0.5, zorder=0)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
