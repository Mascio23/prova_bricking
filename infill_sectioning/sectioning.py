"""Recursive infill sectioning (Shen, Veeramani, Qin, 2026) and angle assignment.

``bisect`` mode (default, the method of the paper)
    A region is bisected along its minor principal axis (i.e. by a line
    perpendicular to the major axis) passing through its centroid whenever
        area > max_area   OR   AR > max_ar
    and the procedure is repeated on both halves. For a region that stays in
    one piece, the result is always a power of two per branch (e.g. a
    100 x 20 mm rectangle -> 4 x (25 x 20 mm)).

    If a cut splits one half into disconnected pieces (e.g. cutting a U), each
    connected piece becomes its own region and is processed independently.

``nsplit`` mode (EXPERIMENTAL, not part of the paper)
    The footprint is cut into N slabs of equal width along its global major
    axis, with the smallest N such that every slab satisfies the thresholds
    (e.g. 100 x 20 mm -> 3 x (33.3 x 20 mm)). Meant only for DOE comparisons.

Angles are assigned by 2-colouring the adjacency graph (regions sharing an
edge of non-zero length). If the graph is not bipartite (odd cycles can occur
with T-junctions), the assignment minimising the total length of edges shared
by same-angle neighbours is chosen and the conflicts are reported.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union

from .moments import Moments, polygon_moments


@dataclass
class Region:
    geom: Polygon
    moments: Moments
    depth: int
    path: str  # branch codes from the root, e.g. "01" (bisect) or "s2" (nsplit)
    within_thresholds: bool = True
    stop_reason: str = ""
    id: int = -1
    angle: float | None = None
    neighbors: dict[int, float] = field(default_factory=dict)  # id -> shared length
    # Intersection of the cutting half-planes that produced this region (None
    # if the region is one of several disconnected pieces of the same cell).
    cell: Polygon | None = None
    # True if this region got the optional third angle (see assign_angles)
    extra: bool = False

    @property
    def area(self) -> float:
        return self.moments.area

    @property
    def aspect_ratio(self) -> float:
        return self.moments.aspect_ratio


@dataclass
class SectioningResult:
    part: object  # (Multi)Polygon of the whole footprint
    part_moments: Moments
    regions: list[Region]
    cuts: list[tuple[np.ndarray, np.ndarray]]  # cut segments, for plotting
    conflicts: list[tuple[int, int, float]]
    mode: str


def _components(geom, min_area: float) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        polys = [geom]
    else:
        polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    return [p for p in polys if p.area > min_area]


def violates(m: Moments, max_area: float, max_ar: float, rel_tol: float = 1e-9) -> bool:
    return m.area > max_area * (1 + rel_tol) or m.aspect_ratio > max_ar * (1 + rel_tol)


def _halfplane(center, normal, size):
    """Polygon approximating the half-plane {p : (p - center) . normal <= 0}."""
    n = normal / np.linalg.norm(normal)
    t = np.array([-n[1], n[0]])
    c = np.asarray(center)
    pts = [c + t * size, c + t * size - n * size, c - t * size - n * size, c - t * size]
    return Polygon(pts)


def _world(geom, pad: float = 1000.0) -> Polygon:
    minx, miny, maxx, maxy = geom.bounds
    return box(minx - pad, miny - pad, maxx + pad, maxy + pad)


def _extent_size(geom) -> float:
    minx, miny, maxx, maxy = geom.bounds
    return 4.0 * max(maxx - minx, maxy - miny) + 100.0


def bisect_regions(part, max_area: float, max_ar: float, min_area: float = 1.0,
                   max_depth: int = 12):
    """Recursive bisection of the footprint. Returns (regions, cut_segments)."""
    sliver = 1e-9 * part.area
    regions: list[Region] = []
    cuts = []
    root = _components(part, sliver)
    world = _world(part)
    stack = [(p, 0, str(i) if len(root) > 1 else "", world if len(root) == 1 else None)
             for i, p in enumerate(root)]
    while stack:
        geom, depth, path, cell = stack.pop(0)
        m = polygon_moments(geom)
        if not violates(m, max_area, max_ar):
            regions.append(Region(geom, m, depth, path, True, "within thresholds", cell=cell))
            continue
        if depth >= max_depth or m.area / 2 < min_area:
            reason = "max depth reached" if depth >= max_depth else "min area reached"
            regions.append(Region(geom, m, depth, path, False, reason, cell=cell))
            continue
        c, n = m.centroid, m.major_axis
        size = _extent_size(geom)
        planes = [_halfplane(c, n, size), _halfplane(c, -n, size)]
        halves = [geom.intersection(hp) for hp in planes]
        # cut segment for plotting: minor-axis line clipped to the region
        t = m.minor_axis
        line = geom.intersection(
            Polygon([c - t * size - n * 1e-6, c + t * size - n * 1e-6,
                     c + t * size + n * 1e-6, c - t * size + n * 1e-6]))
        for g in getattr(line, "geoms", [line]):
            if not g.is_empty and g.area > 0:
                q = (np.asarray(g.exterior.coords) - c) @ t
                cuts.append((c + t * q.min(), c + t * q.max()))
        for side, half in enumerate(halves):
            comps = _components(half, sliver)
            sub = cell.intersection(planes[side]) if cell is not None and len(comps) == 1 else None
            for k, comp in enumerate(comps):
                suffix = str(side) + (chr(ord("a") + k) if len(comps) > 1 else "")
                stack.append((comp, depth + 1, path + suffix, sub))
    return regions, cuts


def nsplit_regions(part, max_area: float, max_ar: float, max_pieces: int = 64):
    """EXPERIMENTAL: N equal-width slabs along the global major axis."""
    sliver = 1e-9 * part.area
    pm = polygon_moments(part)
    n_ax, t_ax = pm.major_axis, pm.minor_axis
    coords = np.vstack([np.asarray(p.exterior.coords)
                        for p in _components(part, 0.0)])
    s = (coords - pm.centroid) @ n_ax
    smin, smax = s.min(), s.max()
    size = _extent_size(part)
    best = None
    for n in range(1, max_pieces + 1):
        edges = np.linspace(smin, smax, n + 1)
        regions, cuts = [], []
        for i in range(n):
            lo = pm.centroid + n_ax * edges[i]
            hi = pm.centroid + n_ax * edges[i + 1]
            slab = _halfplane(lo, -n_ax, size).intersection(_halfplane(hi, n_ax, size))
            comps = _components(part.intersection(slab), sliver)
            for k, comp in enumerate(comps):
                m = polygon_moments(comp)
                ok = not violates(m, max_area, max_ar)
                regions.append(Region(comp, m, 1, f"s{i}" + (chr(97 + k) if k else ""),
                                      ok, "within thresholds" if ok else "N limit",
                                      cell=slab if len(comps) == 1 else None))
            if i > 0:
                line = part.intersection(
                    Polygon([lo - t_ax * size - n_ax * 1e-6, lo + t_ax * size - n_ax * 1e-6,
                             lo + t_ax * size + n_ax * 1e-6, lo - t_ax * size + n_ax * 1e-6]))
                for g in getattr(line, "geoms", [line]):
                    if not g.is_empty and g.area > 0:
                        q = (np.asarray(g.exterior.coords) - lo) @ t_ax
                        cuts.append((lo + t_ax * q.min(), lo + t_ax * q.max()))
        best = (regions, cuts)
        if all(r.within_thresholds for r in regions):
            break
    return best


# --------------------------------------------------------------------------- #
# adjacency and angle assignment
# --------------------------------------------------------------------------- #
def build_adjacency(regions: list[Region], tol: float = 1e-4, min_len: float = 1e-2):
    """Two regions are neighbours if they share a boundary of length > min_len."""
    for r in regions:
        r.neighbors = {}
    for a, b in itertools.combinations(regions, 2):
        if a.geom.distance(b.geom) > tol:
            continue
        shared = a.geom.boundary.intersection(b.geom.buffer(tol)).length
        if shared > min_len:
            a.neighbors[b.id] = shared
            b.neighbors[a.id] = shared


def _min_extra_colouring(regions: list[Region], node_limit: int = 400_000):
    """Proper 3-colouring of the adjacency graph that uses colour 2 on as few regions as
    possible (ties: smallest total area). Colours 0/1 are the two main angles.

    Branch and bound over a BFS order, with the first region of every component fixed
    to colour 0 (the swap symmetry). Returns (colours, proven_minimal) or (None, False)
    when no proper 3-colouring was found within `node_limit` search nodes."""
    n = len(regions)
    adj = [set(r.neighbors) for r in regions]
    area = [r.area for r in regions]
    order, seen = [], set()
    for start in sorted(range(n), key=lambda i: -len(adj[i])):
        if start in seen:
            continue
        queue = [start]
        seen.add(start)
        while queue:
            i = queue.pop(0)
            order.append(i)
            for j in sorted(adj[i], key=lambda j: -len(adj[j])):
                if j not in seen:
                    seen.add(j)
                    queue.append(j)
    is_root = {order[0]} | {i for k, i in enumerate(order)
                            if not any(j in adj[i] for j in order[:k])}
    col = [-1] * n
    best = {"cost": (n + 1, 0.0), "col": None}
    nodes = [0]

    def rec(k, count, extra_area):
        if (count, extra_area) >= best["cost"]:
            return
        nodes[0] += 1
        if nodes[0] > node_limit:
            return
        if k == n:
            best["cost"], best["col"] = (count, extra_area), col[:]
            return
        i = order[k]
        used = {col[j] for j in adj[i] if col[j] >= 0}
        for c in ((0,) if i in is_root else (0, 1, 2)):
            if c in used:
                continue
            col[i] = c
            rec(k + 1, count + (c == 2), extra_area + (area[i] if c == 2 else 0.0))
            col[i] = -1

    rec(0, 0, 0.0)
    return best["col"], nodes[0] <= node_limit


def assign_angles(regions: list[Region], angles=(45.0, 135.0), extra_angle: float | None = None):
    """Give every region one of two angles so that neighbours differ (2-colouring of the
    adjacency graph); returns the list of conflicting edges (same angle on a shared edge).

    With T-junctions the graph can have odd cycles and two angles are not enough. If
    `extra_angle` is given, a third angle is then used on as few regions as possible
    (`Region.extra`), and only on those, so that no two neighbours share an angle. With
    `extra_angle=None` the assignment with the least shared edge length is kept and its
    conflicts are returned."""
    n = len(regions)
    for r in regions:
        r.extra = False
    edges = [(a.id, b_id, w) for a in regions for b_id, w in a.neighbors.items()
             if a.id < b_id]

    def cost(col):
        return sum(w for i, j, w in edges if col[i] == col[j])

    if n <= 16:
        # exhaustive search, region 0 fixed to the first angle
        best_col, best_cost = None, None
        for bits in range(1 << max(n - 1, 0)):
            col = [0] + [(bits >> k) & 1 for k in range(n - 1)]
            cst = cost(col)
            if best_cost is None or cst < best_cost - 1e-12:
                best_col, best_cost = col, cst
                if cst == 0:
                    break
        col = best_col
    else:
        # BFS 2-colouring followed by greedy single-flip improvement
        col = [-1] * n
        for start in range(n):
            if col[start] != -1:
                continue
            col[start] = 0
            queue = [start]
            while queue:
                i = queue.pop(0)
                for j in regions[i].neighbors:
                    if col[j] == -1:
                        col[j] = 1 - col[i]
                        queue.append(j)
        improved = True
        while improved:
            improved = False
            for i in range(n):
                cur = cost(col)
                col[i] = 1 - col[i]
                if cost(col) < cur - 1e-12:
                    improved = True
                else:
                    col[i] = 1 - col[i]
    conflicts = [(i, j, w) for i, j, w in edges if col[i] == col[j]]
    if conflicts and extra_angle is not None:
        col3, _ = _min_extra_colouring(regions)
        if col3 is not None:
            col, conflicts = col3, []
    palette = (float(angles[0]), float(angles[1]), None if extra_angle is None else float(extra_angle))
    for r in regions:
        r.angle = palette[col[r.id]]
        r.extra = col[r.id] == 2
    return conflicts


def section(part, max_area: float = 900.0, max_ar: float = 2.0, mode: str = "bisect",
            min_area: float = 1.0, max_depth: int = 12, angles=(45.0, 135.0),
            max_pieces: int = 64, extra_angle: float | None = None) -> SectioningResult:
    part_m = polygon_moments(part)
    if mode == "bisect":
        regions, cuts = bisect_regions(part, max_area, max_ar, min_area, max_depth)
    elif mode == "nsplit":
        regions, cuts = nsplit_regions(part, max_area, max_ar, max_pieces)
    else:
        raise ValueError(f"unknown mode {mode!r}")

    # Stable ordering: along the part's major axis, then along its minor axis
    ax, ay = part_m.major_axis, part_m.minor_axis
    regions.sort(key=lambda r: (round(float((r.moments.centroid - part_m.centroid) @ ax), 6),
                                round(float((r.moments.centroid - part_m.centroid) @ ay), 6)))
    for i, r in enumerate(regions):
        r.id = i
    build_adjacency(regions)
    if extra_angle is not None and any(
            abs(((extra_angle - a + 90) % 180) - 90) < 1e-6 for a in angles):
        raise ValueError(f"the extra angle ({extra_angle:g}) must differ from {angles[0]:g} "
                         f"and {angles[1]:g} (angles are taken modulo 180)")
    conflicts = assign_angles(regions, angles, extra_angle)
    return SectioningResult(part, part_m, regions, cuts, conflicts, mode)


def coverage_error(result: SectioningResult) -> dict:
    """Area bookkeeping: union of regions vs part, and pairwise overlaps."""
    union = unary_union([r.geom for r in result.regions])
    overlap = sum(a.geom.intersection(b.geom).area
                  for a, b in itertools.combinations(result.regions, 2))
    return {
        "part_area": result.part.area,
        "sum_region_area": sum(r.area for r in result.regions),
        "uncovered_area": result.part.difference(union).area,
        "overlap_area": overlap,
    }


def modifier_footprints(result: SectioningResult, margin: float = 2.0):
    """XY outline of each modifier: the region grown by ``margin`` on its
    outer (free) sides only.

    The growth is clipped to the region's cutting cell (the half-planes that
    produced it), so neighbouring modifiers meet exactly on the cut lines,
    also in the overhang outside the part. Where no cell is available
    (disconnected pieces) the growth into other regions is subtracted, so
    modifiers can only overlap where there is no material. In both cases the
    union of all modifiers covers the whole part."""
    out = []
    for r in result.regions:
        grown = r.geom.buffer(margin, join_style="mitre", mitre_limit=2.0)
        if r.cell is not None:
            grown = grown.intersection(r.cell)
        others = unary_union([o.geom for o in result.regions if o.id != r.id])
        g = grown.difference(others) if not others.is_empty else grown
        g = unary_union([g, r.geom])
        polys = _components(g, 1e-9 * r.area)
        # keep only pieces attached to the region itself (drop far islands)
        polys = [p for p in polys if p.intersects(r.geom)]
        out.append(MultiPolygon(polys) if len(polys) > 1 else polys[0])
    return out
