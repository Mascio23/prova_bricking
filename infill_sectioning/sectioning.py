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
    extra_level: int = 0  # 0 = one of the two main angles, 1 = first extra angle, 2 = second

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


def thickness(geom) -> float:
    """Mean width of a region: 2 * area / perimeter (the width of a long strip, ~side/2 of
    a square). Small values mean a sliver."""
    return 2.0 * geom.area / geom.length if geom.length > 0 else 0.0


def bisect_regions(part, max_area: float, max_ar: float, min_area: float = 1.0,
                   max_depth: int = 12, min_width: float = 0.0):
    """Recursive bisection of the footprint. Returns (regions, cut_segments).

    A piece thinner than `min_width` (see `thickness`) is not bisected any more: it is a
    sliver (a cut ran close to an edge), and halving it again and again would only
    produce dozens of useless regions. It is left as a region marked "sliver" for
    `absorb_slivers`."""
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
        if min_width > 0 and thickness(geom) < min_width:
            regions.append(Region(geom, m, depth, path, False, "sliver", cell=cell))
            continue
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


def _polygonal(geom):
    """Only the areas of a geometry (intersections can also give lines and points)."""
    parts = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon) and not g.is_empty]
    if not parts:
        return Polygon()
    return unary_union(parts)


def absorb_slivers(regions: list[Region], min_width: float, max_area: float, max_ar: float):
    """Give the slivers (regions stopped as "sliver") to the neighbouring regions.

    Every sliver point goes to the nearest region within `min_width`: the regions are grown
    into the slivers by small steps, so a long strip along several regions is divided
    among them in proportion to where it touches each. The partition stays exact (no area
    lost or counted twice). Parts of a sliver farther than `min_width` from any region
    (for example the far end of a thin rib) stay regions of their own, marked "sliver"."""
    slivers = [r for r in regions if r.stop_reason == "sliver"]
    solid = [r for r in regions if r.stop_reason != "sliver"]
    if not slivers or not solid:
        return regions
    remaining = _polygonal(unary_union([r.geom for r in slivers]))
    step = min_width / 10.0
    grown = {id(r): r.geom for r in solid}
    claims = {id(r): [] for r in solid}
    active = [r for r in solid if r.geom.distance(remaining) <= 1.2 * min_width]
    for _ in range(12):
        if remaining.is_empty or remaining.area < 1e-9:
            break
        for r in active:
            got = _polygonal(grown[id(r)].buffer(step, quad_segs=4).intersection(remaining))
            if got.is_empty or got.area < 1e-12:
                continue
            remaining = remaining.difference(got)
            claims[id(r)].append(got)
            grown[id(r)] = unary_union([grown[id(r)], got])

    out: list[Region] = []
    for r in regions:
        if r.stop_reason == "sliver":
            continue
        if claims.get(id(r)):
            # the claims share a boundary with the region that was computed by another
            # route: close the hairline gaps (about 1e-12 mm) the union would leave
            g = _polygonal(unary_union([r.geom] + claims[id(r)])
                           .buffer(1e-6, join_style="mitre").buffer(-1e-6, join_style="mitre"))
            if isinstance(g, MultiPolygon):  # keep the piece that holds the region
                parts = list(g.geoms)
                k = max(range(len(parts)),
                        key=lambda j: parts[j].intersection(r.geom.buffer(1e-6)).area)
                remaining = _polygonal(unary_union(
                    [remaining] + parts[:k] + parts[k + 1:]))
                g = parts[k]
            m = polygon_moments(g)
            ok = not violates(m, max_area, max_ar)
            r = Region(g, m, r.depth, r.path, ok,
                       "within thresholds" if ok else "exceeds thresholds after merging slivers",
                       cell=None)
        out.append(r)
    depth = max(r.depth for r in slivers)
    for q in getattr(remaining, "geoms", [remaining]):
        if isinstance(q, Polygon) and q.area > 1e-6:
            out.append(Region(q, polygon_moments(q), depth, "sliver", False, "sliver"))
    return out


def _shared_length(a, b, tol: float = 1e-4) -> float:
    if a.distance(b) > tol:
        return 0.0
    return a.boundary.intersection(b.buffer(tol)).length


def merge_dust(regions: list[Region], min_width: float, max_area: float, max_ar: float):
    """Merge the leftover fragments into a neighbour, however short their contact is.

    After `absorb_slivers` a cut that grazes a thin rib can leave fragments of a few
    hundredths of a mm^2: they are smaller than a square of side `min_width`, so they hold
    no infill, yet each would get its own modifier. Every such fragment (a "sliver" region
    with area < min_width^2) is united with the neighbour it shares the longest boundary
    with. The partition stays exact. A fragment that touches nothing (an island) stays."""
    dust_area = min_width ** 2
    regions = list(regions)
    skipped: set[int] = set()
    while True:
        cand = [r for r in regions if r.stop_reason == "sliver" and r.area < dust_area
                and id(r) not in skipped]
        if not cand:
            return regions
        d = min(cand, key=lambda r: r.area)
        best, best_len = None, 1e-6
        for r in regions:
            if r is d:
                continue
            length = _shared_length(d.geom, r.geom)
            if length > best_len:
                best, best_len = r, length
        if best is None:
            skipped.add(id(d))
            continue
        g = _polygonal(unary_union([best.geom, d.geom])
                       .buffer(1e-6, join_style="mitre").buffer(-1e-6, join_style="mitre"))
        if not isinstance(g, Polygon):  # would not stay one piece: leave it alone
            skipped.add(id(d))
            continue
        m = polygon_moments(g)
        if best.stop_reason == "sliver":
            merged = Region(g, m, best.depth, best.path, False, "sliver", cell=None)
        else:
            ok = not violates(m, max_area, max_ar)
            merged = Region(g, m, best.depth, best.path, ok,
                            "within thresholds" if ok else "exceeds thresholds after merging slivers",
                            cell=None)
        regions = [merged if r is best else r for r in regions if r is not d]


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
            return best
    bad = [r for r in best[0] if not r.within_thresholds]
    worst = max(bad, key=lambda r: r.aspect_ratio)
    raise ValueError(
        f"mode nsplit cannot meet the thresholds on this shape: even with {max_pieces} slabs, "
        f"{len(bad)} of {len(best[0])} regions are out of threshold (worst: aspect ratio "
        f"{worst.aspect_ratio:.1f}, area {worst.area:.0f} mm2). nsplit only cuts across the "
        "major axis of the whole footprint, so it cannot shorten parts that run parallel to "
        "the cuts (for example the long bars of a frame). Use --mode bisect (the method of "
        "the paper), or raise --max-area / --max-ar.")


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


def _min_extra_colouring(regions: list[Region], n_extra: int = 1, node_limit: int = 200_000):
    """Proper colouring of the adjacency graph with 2 + n_extra colours that uses the extra
    colours (2, and 3 if n_extra == 2) on as few regions as possible.

    Cost, compared in this order: number of regions with an extra colour, number of those
    with the second extra colour, their total area. Branch and bound with the DSATUR rule
    (colour next the region whose neighbours already use the most colours), trying colours
    0, 1, 2, 3 in this order: the first solution comes quickly and the search improves it
    until it is complete or `node_limit` nodes have been visited.

    Returns (colours, complete); colours is None if no proper colouring was found (for
    n_extra == 1 the graph may simply need four colours)."""
    n = len(regions)
    k = 2 + n_extra
    adj = [list(r.neighbors) for r in regions]
    area = [r.area for r in regions]
    deg = [len(a) for a in adj]
    col = [-1] * n
    seen = [[0] * k for _ in range(n)]  # seen[v][c]: neighbours of v coloured c
    ndist = [0] * n                     # distinct colours among the neighbours of v
    todo = set(range(n))
    best = {"cost": (n + 1, n + 1, 0.0), "col": None}
    nodes = [0]

    def paint(v, c, sign):
        col[v] = c if sign > 0 else -1
        for u in adj[v]:
            before = seen[u][c]
            seen[u][c] += sign
            if sign > 0 and before == 0:
                ndist[u] += 1
            elif sign < 0 and before == 1:
                ndist[u] -= 1

    def rec(count, count3, extra_area):
        if (count, count3, extra_area) >= best["cost"]:
            return
        nodes[0] += 1
        if nodes[0] > node_limit:
            return
        if not todo:
            best["cost"], best["col"] = (count, count3, extra_area), col[:]
            return
        v = max(todo, key=lambda u: (ndist[u], deg[u]))
        root = ndist[v] == 0  # no coloured neighbour: first of its component (swap 0 <-> 1)
        todo.discard(v)
        for c in ((0,) if root else range(k)):
            if seen[v][c]:
                continue
            paint(v, c, +1)
            rec(count + (c >= 2), count3 + (c == 3), extra_area + (area[v] if c >= 2 else 0.0))
            paint(v, c, -1)
        todo.add(v)

    rec(0, 0, 0.0)
    return best["col"], nodes[0] <= node_limit


def _extra_list(extra_angle) -> list[float]:
    """None, one angle or a sequence of up to two angles -> list of floats."""
    if extra_angle is None:
        return []
    if isinstance(extra_angle, (int, float)):
        return [float(extra_angle)]
    out = [float(a) for a in extra_angle]
    if len(out) > 2:
        raise ValueError("at most two extra angles are supported")
    return out


def assign_angles(regions: list[Region], angles=(45.0, 135.0), extra_angle=None):
    """Give every region one of two angles so that neighbours differ (2-colouring of the
    adjacency graph); returns the list of conflicting edges (same angle on a shared edge).

    With T-junctions the graph can have odd cycles and two angles are not enough. If
    `extra_angle` is given, a third angle is then used on as few regions as possible
    (`Region.extra`), and only on those, so that no two neighbours share an angle. With
    `extra_angle=None` the assignment with the least shared edge length is kept and its
    conflicts are returned."""
    n = len(regions)
    extras = _extra_list(extra_angle)
    for r in regions:
        r.extra, r.extra_level = False, 0
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
    if conflicts and extras:
        # as few distinct angles as possible: the second extra angle only if three are
        # not enough, and then the fewest regions with an extra angle
        for m in range(1, len(extras) + 1):
            col_x, _ = _min_extra_colouring(regions, m)
            if col_x is not None:
                col, conflicts = col_x, []
                break
    palette = [float(angles[0]), float(angles[1])] + extras
    for r in regions:
        r.angle = palette[col[r.id]]
        r.extra, r.extra_level = col[r.id] >= 2, max(0, col[r.id] - 1)
    return conflicts


def section(part, max_area: float = 900.0, max_ar: float = 2.0, mode: str = "bisect",
            min_area: float = 1.0, max_depth: int = 12, angles=(45.0, 135.0),
            max_pieces: int = 64, extra_angle=None,
            min_width: float = 2.0, min_contact: float = 1.0) -> SectioningResult:
    extras = _extra_list(extra_angle)
    every = [float(a) for a in angles] + extras
    for i, a in enumerate(every):
        for b in every[:i]:
            if abs(((a - b + 90) % 180) - 90) < 1e-6:
                raise ValueError(f"the angles {b:g} and {a:g} coincide: the two main angles and "
                                 "the extra angles must all differ (angles are taken modulo 180)")
    part_m = polygon_moments(part)
    if mode == "bisect":
        regions, cuts = bisect_regions(part, max_area, max_ar, min_area, max_depth, min_width)
        if min_width > 0:
            regions = absorb_slivers(regions, min_width, max_area, max_ar)
            regions = merge_dust(regions, min_width, max_area, max_ar)
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
    build_adjacency(regions, min_len=min_contact)
    conflicts = assign_angles(regions, angles, extras)
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
