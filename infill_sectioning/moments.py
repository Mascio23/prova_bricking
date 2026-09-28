"""2D image moments of a planar region (area, centroid, principal axes, AR).

Two independent implementations:

* ``polygon_moments``: exact, from the polygon vertices (Green's theorem).
  This is the one used by the sectioning algorithm.
* ``raster_moments``: the region is rasterised on a regular grid and
  ``cv2.moments`` is applied, as in classic image-moment analysis. Used as a
  cross-check (the two must agree to within the discretisation error).

The equivalent ellipse is the uniform ellipse with the same area, centroid
and second-order central moments. With lambda1 >= lambda2 the eigenvalues of
the covariance matrix  C = [[mu20, mu11], [mu11, mu02]] / A,
the semi-axes are a = 2*sqrt(lambda1), b = 2*sqrt(lambda2) and the aspect
ratio is AR = a / b = sqrt(lambda1 / lambda2).
For a w x h rectangle this gives exactly AR = w / h.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient


@dataclass
class Moments:
    area: float
    cx: float
    cy: float
    mu20: float  # central second moments (integral of dx^2, dy^2, dx*dy)
    mu02: float
    mu11: float

    @property
    def centroid(self) -> np.ndarray:
        return np.array([self.cx, self.cy])

    def _eig(self):
        cov = np.array([[self.mu20, self.mu11], [self.mu11, self.mu02]]) / self.area
        vals, vecs = np.linalg.eigh(cov)  # ascending
        return vals[::-1], vecs[:, ::-1]

    @property
    def major_axis(self) -> np.ndarray:
        """Unit vector of the major principal axis (sign-normalised)."""
        _, vecs = self._eig()
        v = vecs[:, 0]
        # deterministic sign: point towards +x (or +y if vertical)
        if v[0] < -1e-12 or (abs(v[0]) <= 1e-12 and v[1] < 0):
            v = -v
        return v

    @property
    def minor_axis(self) -> np.ndarray:
        v = self.major_axis
        return np.array([-v[1], v[0]])

    @property
    def major_axis_deg(self) -> float:
        """Angle of the major axis w.r.t. +X, in [0, 180)."""
        v = self.major_axis
        ang = float(np.degrees(np.arctan2(v[1], v[0])) % 180.0)
        return 0.0 if ang > 180.0 - 1e-9 else ang

    @property
    def semi_axes(self) -> tuple[float, float]:
        vals, _ = self._eig()
        vals = np.clip(vals, 0.0, None)
        return float(2 * np.sqrt(vals[0])), float(2 * np.sqrt(vals[1]))

    @property
    def aspect_ratio(self) -> float:
        a, b = self.semi_axes
        if b <= 0:
            return float("inf")
        return a / b


def _ring_terms(coords: np.ndarray):
    x0, y0 = coords[:-1, 0], coords[:-1, 1]
    x1, y1 = coords[1:, 0], coords[1:, 1]
    c = x0 * y1 - x1 * y0
    a = c.sum() / 2.0
    sx = ((x0 + x1) * c).sum() / 6.0  # integral of x dA
    sy = ((y0 + y1) * c).sum() / 6.0  # integral of y dA
    sxx = ((x0 * x0 + x0 * x1 + x1 * x1) * c).sum() / 12.0
    syy = ((y0 * y0 + y0 * y1 + y1 * y1) * c).sum() / 12.0
    sxy = ((x0 * y1 + 2 * x0 * y0 + 2 * x1 * y1 + x1 * y0) * c).sum() / 24.0
    return np.array([a, sx, sy, sxx, syy, sxy])


def _polygons(geom):
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        return [g for g in geom.geoms if isinstance(g, Polygon)]
    raise TypeError(f"unsupported geometry type {geom.geom_type}")


def polygon_moments(geom) -> Moments:
    """Exact moments of a (Multi)Polygon with holes."""
    # Work in coordinates relative to a local origin for numerical accuracy.
    minx, miny, maxx, maxy = geom.bounds
    ox, oy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    tot = np.zeros(6)
    for poly in _polygons(geom):
        poly = orient(poly, 1.0)  # exterior CCW, holes CW -> holes subtract
        rings = [poly.exterior] + list(poly.interiors)
        for ring in rings:
            xy = np.asarray(ring.coords, dtype=float) - (ox, oy)
            tot += _ring_terms(xy)
    a, sx, sy, sxx, syy, sxy = tot
    if a <= 0:
        raise ValueError("region has zero area")
    cx, cy = sx / a, sy / a
    mu20 = sxx - a * cx * cx
    mu02 = syy - a * cy * cy
    mu11 = sxy - a * cx * cy
    return Moments(a, cx + ox, cy + oy, mu20, mu02, mu11)


def rasterize(geom, pixel: float, pad: int = 2):
    """Rasterise a geometry. Returns (uint8 image, x0, y0) where pixel (row r,
    col c) has its centre at (x0 + (c + 0.5)*pixel, y0 + (r + 0.5)*pixel).

    A pixel is set when its centre lies inside the geometry (point sampling).
    Unlike cv2.fillPoly, which also sets every pixel touched by an edge, this
    has no systematic outward bias, so area and moments converge to the exact
    values as the pixel size decreases."""
    import shapely

    minx, miny, maxx, maxy = geom.bounds
    x0, y0 = minx - pad * pixel, miny - pad * pixel
    w = int(np.ceil((maxx - minx) / pixel)) + 2 * pad
    h = int(np.ceil((maxy - miny) / pixel)) + 2 * pad
    xs = x0 + (np.arange(w) + 0.5) * pixel
    ys = y0 + (np.arange(h) + 0.5) * pixel
    gx, gy = np.meshgrid(xs, ys)
    shapely.prepare(geom)
    img = shapely.contains_xy(geom, gx, gy).astype(np.uint8)
    return img, x0, y0


def raster_moments(geom, pixel: float = 0.05) -> Moments:
    """Moments via rasterisation + cv2.moments (cross-check)."""
    import cv2

    img, x0, y0 = rasterize(geom, pixel)
    m = cv2.moments(img, binaryImage=True)
    if m["m00"] == 0:
        raise ValueError("empty raster; decrease the pixel size")
    area = m["m00"] * pixel**2
    cx = x0 + (m["m10"] / m["m00"] + 0.5) * pixel
    cy = y0 + (m["m01"] / m["m00"] + 0.5) * pixel
    k = pixel**4
    return Moments(area, cx, cy, m["mu20"] * k, m["mu02"] * k, m["mu11"] * k)
