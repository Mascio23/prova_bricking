"""Modifiers that cover only the bottom and top solid layers.

With a sparse infill (grid, 15 %, ...) the sectioning cuts the infill at every boundary
between two regions: the lines of one region do not join those of the next. To keep the
infill continuous the modifiers of the regions are limited to the layers where the solid
infill is printed (the bottom and the top shell), and the middle of the part is left to
one modifier that has no top and no bottom layers.

How Bambu Studio decides (checked in its source, PrintObject.cpp / Slicing.cpp):

* layers: the first one has the "initial layer height", the others the layer height;
  a modifier is assigned to a layer if it contains the plane at the MIDDLE of the layer;
* a layer is made solid if a top (bottom) surface lies within the next ``top_shell_layers``
  (previous ``bottom_shell_layers``) layers, or within ``top_shell_thickness`` (mm); the
  numbers used are those of the region (the set of settings) of the layer being converted,
  and only that region becomes solid. So a block of N layers with ``top_shell_layers = N``
  and thickness 0 is solid in all its N layers, and the layers below it, which belong to
  the central modifier with 0 layers, stay sparse. Without the central modifier the global
  value (5 for the default process) would turn the layers under a shorter block solid too;
* top and bottom surfaces are found against the union of all regions (``interface_shells``
  off, the default), so the boundaries between modifiers create no surface of their own.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bambu3mf import infill_settings

# settings that make a modifier print no solid layers (the middle of the part)
CENTRAL_SETTINGS = {"top_shell_layers": "0", "bottom_shell_layers": "0",
                    "top_shell_thickness": "0", "bottom_shell_thickness": "0"}


@dataclass
class ShellPlan:
    bottom_layers: int
    top_layers: int
    layer_height: float
    first_layer_height: float
    n_layers: int          # layers of the part, as Bambu Studio will slice it
    z_min: float
    z_max: float
    z_bottom: float        # top of the bottom block (model Z)
    z_top: float           # bottom of the top block (model Z)
    central: bool = True   # also a modifier for the middle, with no top / bottom layers
    style: str = "infill"  # how the blocks make their layers: "infill" or "shells"
    block_wall_loops: int = 1

    @property
    def central_layers(self) -> int:
        return self.n_layers - self.bottom_layers - self.top_layers

    def settings_for_blocks(self) -> dict[str, str]:
        """Settings of the region modifiers (``infill_direction`` is added per region).

        style "infill" (default): every layer of the block is a plain 100 % rectilinear
        infill in the direction of its region, with ``block_wall_loops`` wall(s) around the
        zone and no top/bottom shell: each block is an autonomous zone, as if drawn by hand.
        This is what works with many walls: the wall count of the part then applies to the
        middle only, and the zones are not filled with concentric wall loops.

        style "shells": the layers are made solid by the shell logic: exactly N solid layers
        at each end (``bottom/top_shell_layers = N``, thickness 0) and the walls of the part.
        Verified with 2 walls; with many walls the zones fill up with loops around them."""
        if self.style == "shells":
            return {"bottom_shell_layers": str(self.bottom_layers),
                    "top_shell_layers": str(self.top_layers),
                    "bottom_shell_thickness": "0", "top_shell_thickness": "0"}
        return {"wall_loops": str(self.block_wall_loops),
                "bottom_shell_layers": "0", "top_shell_layers": "0",
                "bottom_shell_thickness": "0", "top_shell_thickness": "0",
                **infill_settings(100.0, "rectilinear")}

    def as_dict(self) -> dict:
        def r(v):
            return round(float(v), 6)

        return {"bottom_layers": self.bottom_layers, "top_layers": self.top_layers,
                "layer_height_mm": r(self.layer_height),
                "first_layer_height_mm": r(self.first_layer_height),
                "n_layers": self.n_layers,
                "bottom_block_z_mm": [r(self.z_min), r(self.z_bottom)],
                "top_block_z_mm": [r(self.z_top), r(self.z_max)],
                "blocks_as": self.style, "block_wall_loops": self.block_wall_loops,
                "central_modifier": self.central,
                "central_z_mm": [r(self.z_bottom), r(self.z_top)]}


def layer_edges(height: float, layer_height: float, first_layer_height: float | None = None):
    """Heights (from the bottom of the part) of the layer boundaries, as Bambu Studio
    generates them: a layer is added while its middle is still below the part height."""
    first = layer_height if first_layer_height is None else first_layer_height
    edges = [0.0, first]
    while edges[-1] + 0.5 * layer_height < height - 1e-9:
        edges.append(edges[-1] + layer_height)
    return edges


def plan_shells(z_min: float, z_max: float, bottom_layers: int, top_layers: int,
                layer_height: float, first_layer_height: float | None = None,
                central: bool = True, style: str = "infill",
                block_wall_loops: int = 1) -> ShellPlan:
    if style not in ("infill", "shells"):
        raise ValueError(f"unknown block style {style!r}: use infill or shells")
    if block_wall_loops < 1:
        raise ValueError("the walls of the blocks must be at least 1")
    if bottom_layers < 1 or top_layers < 1:
        raise ValueError("the number of bottom and top layers must be at least 1")
    if not layer_height > 0:
        raise ValueError("the layer height must be greater than 0")
    first = layer_height if first_layer_height is None else first_layer_height
    if not first > 0:
        raise ValueError("the first layer height must be greater than 0")
    edges = layer_edges(z_max - z_min, layer_height, first)
    n = len(edges) - 1
    if bottom_layers + top_layers >= n:
        raise ValueError(
            f"the part has {n} layers ({z_max - z_min:g} mm at {layer_height:g} mm) and "
            f"{bottom_layers} bottom + {top_layers} top layers leave no middle: the "
            "modifiers would cover the whole part. Leave out --bottom-layers / "
            "--top-layers (modifiers over the full height) or use fewer layers")
    return ShellPlan(bottom_layers, top_layers, layer_height, first, n, z_min, z_max,
                     z_min + edges[bottom_layers], z_min + edges[n - top_layers], central,
                     style, block_wall_loops)


def layer_of_modifier(plan: ShellPlan, z0: float, z1: float) -> list[int]:
    """Layers (1-based) whose middle plane lies inside [z0, z1] (used by the tests and the
    report): what Bambu Studio will assign to a modifier of that height."""
    edges = layer_edges(plan.z_max - plan.z_min, plan.layer_height, plan.first_layer_height)
    mids = [plan.z_min + 0.5 * (a + b) for a, b in zip(edges, edges[1:])]
    return [i + 1 for i, m in enumerate(mids) if z0 < m < z1]


def prismatic_deviation(mesh, part, z0: float, z1: float, samples: int = 5) -> float:
    """Largest fraction of the footprint area that differs from a horizontal slice of the
    part inside (z0, z1). About 0 for an extruded (2.5D) part; large if the part has steps,
    closed pockets or other horizontal surfaces in the middle."""
    from .footprint import footprint_section

    worst = 0.0
    for z in np.linspace(z0, z1, samples + 2)[1:-1]:
        try:
            cut = footprint_section(mesh, float(z))
        except ValueError:
            return 1.0
        diff = part.symmetric_difference(cut).area / max(part.area, 1e-9)
        worst = max(worst, diff)
    return worst
