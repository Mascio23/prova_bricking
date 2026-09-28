"""Loading STL/STEP models and extracting the XY footprint.

* STL (and anything else trimesh can read: OBJ, PLY, 3MF, ...) is loaded with
  trimesh.
* STEP is read with OpenCascade through ``cadquery-ocp`` (``pip install
  cadquery-ocp``), tessellated, and then goes through the same mesh pipeline.
  The import is lazy, so the STL workflow does not need OCP installed.

Two footprint methods are available:

* ``projection`` (default): union of all non-vertical triangles projected on
  XY, i.e. the true "shadow" of the part on the build plate.
* ``section``: planar cross-section of the mesh at a given height (default
  mid-height). For a 2.5D part the two are identical; comparing them is a
  quick check that the part really is 2.5D.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import shapely
import trimesh
from shapely.geometry import MultiPolygon, Polygon

STEP_SUFFIXES = {".step", ".stp"}


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_mesh(path: str | Path, step_tolerance: float = 0.05) -> trimesh.Trimesh:
    path = Path(path)
    if path.suffix.lower() in STEP_SUFFIXES:
        return load_step_mesh(path, linear_deflection=step_tolerance)
    mesh = trimesh.load(path, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError(f"no triangles found in {path}")
    return mesh


def load_step_mesh(path: str | Path, linear_deflection: float = 0.05,
                   angular_deflection: float = 0.2) -> trimesh.Trimesh:
    """Read a STEP file with OpenCascade (cadquery-ocp) and tessellate it."""
    try:
        from OCP.BRep import BRep_Tool
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "Reading STEP files requires OpenCascade bindings: "
            "pip install cadquery-ocp   (or export the part as STL)"
        ) from exc

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot read STEP file {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection, True)

    to_face = getattr(TopoDS, "Face_s", None) or TopoDS.Face  # OCP < 8 / OCP >= 8
    verts, faces = [], []
    offset = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                verts.append((p.X(), p.Y(), p.Z()))
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                faces.append((offset + a - 1, offset + b - 1, offset + c - 1))
            offset += tri.NbNodes()
        exp.Next()
    if not faces:
        raise ValueError(f"STEP file {path} produced no triangles")
    mesh = trimesh.Trimesh(np.asarray(verts), np.asarray(faces), process=True)
    mesh.merge_vertices()
    return mesh


# --------------------------------------------------------------------------- #
# footprint
# --------------------------------------------------------------------------- #
def clean(geom, eps: float = 1e-4):
    """Close numerical micro-gaps and drop degenerate bits."""
    geom = shapely.make_valid(geom)
    geom = geom.buffer(eps, join_style="mitre").buffer(-eps, join_style="mitre")
    geom = geom.simplify(eps / 10, preserve_topology=True)
    return only_polygons(geom)


def only_polygons(geom):
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    return MultiPolygon(polys) if len(polys) > 1 else polys[0]


def footprint_projection(mesh: trimesh.Trimesh):
    """Union of all non-vertical triangles projected on the XY plane."""
    tri = mesh.triangles[:, :, :2]
    # 2D signed area; |area| ~ 0 means the triangle is vertical (a wall)
    d1 = tri[:, 1] - tri[:, 0]
    d2 = tri[:, 2] - tri[:, 0]
    area2 = np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0])
    keep = area2 > 1e-10 * max(area2.max(), 1e-30)
    tri = tri[keep]
    closed = np.concatenate([tri, tri[:, :1]], axis=1)
    polys = shapely.polygons(closed)
    return clean(shapely.union_all(polys))


def footprint_section(mesh: trimesh.Trimesh, z: float | None = None):
    """Cross-section of the mesh with the plane Z = z (default: mid-height)."""
    if z is None:
        z = float(mesh.bounds[:, 2].mean())
    sec = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    if sec is None:
        raise ValueError(f"no cross-section at z={z}")
    # Keep the original XY frame (identity transform)
    to_2d = getattr(sec, "to_2D", None) or sec.to_planar
    path2d, _ = to_2d(to_2D=np.eye(4))
    polys = list(path2d.polygons_full)
    if not polys:
        raise ValueError(f"cross-section at z={z} has no closed loops")
    return clean(shapely.union_all(polys))


def extract_footprint(mesh: trimesh.Trimesh, method: str = "projection"):
    if method == "projection":
        return footprint_projection(mesh)
    if method == "section":
        return footprint_section(mesh)
    raise ValueError(f"unknown footprint method {method!r}")
