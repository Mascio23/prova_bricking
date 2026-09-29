"""Bambu Studio project (.3mf) with the part and the infill modifiers.

The layout mirrors a project saved by Bambu Studio 2.08 (File -> Save Project)
containing one multi-part object; see tests/data/bambu_reference_provino.3mf:

  [Content_Types].xml
  _rels/.rels
  3D/3dmodel.model               object N+1 = components -> 3D/Objects/object_1.model
  3D/_rels/3dmodel.model.rels
  3D/Objects/object_1.model      one mesh object per volume (id 1..N), each
                                 centred on its own bounding box
  Metadata/model_settings.config per-part type and settings
  Metadata/project_settings.config  (copied from a template project, optional)

Key facts, checked against the Bambu Studio sources (src/libslic3r/Model.cpp,
ModelVolume::type_from_string, and Format/bbs_3mf.cpp, _BBS_3MF_Importer):

* a volume is a modifier when its <part> has subtype="modifier_part"
  (normal_part / negative_part / modifier_part / support_enforcer /
  support_blocker);
* every <metadata key=... value=...> of a <part> other than name, matrix,
  source_* and a few internal keys is loaded into that volume's config, so
  per-modifier overrides are written as e.g. key="infill_direction";
* the file is treated as a Bambu project (and model_settings.config is honoured)
  only if the "Application" metadata starts with "BambuStudio-".
"""

from __future__ import annotations

import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

import numpy as np
import trimesh

DEFAULT_APP = "BambuStudio-02.08.02.61"
DEFAULT_PLATE_CENTER = (175.0, 160.0)  # Bambu Lab H2D, printable area 350 x 320 mm

_UUID_NS = uuid.UUID("6f1d2b1e-5a0c-4c55-9a55-2f9f3c1d2b10")

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

MODEL_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
      'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
      'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
      'requiredextensions="p"')


@dataclass
class Volume:
    name: str
    mesh: trimesh.Trimesh
    subtype: str = "normal_part"  # or "modifier_part"
    settings: dict[str, str] = field(default_factory=dict)


@dataclass
class Template:
    """What is reused from a project saved by Bambu Studio."""
    project_settings: bytes | None = None
    application: str = DEFAULT_APP
    plate_center: tuple[float, float] = DEFAULT_PLATE_CENTER
    extruder: str = "1"
    global_settings: dict = field(default_factory=dict)


def read_template(path: str | Path) -> Template:
    t = Template()
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "Metadata/project_settings.config" in names:
            t.project_settings = z.read("Metadata/project_settings.config")
            try:
                t.global_settings = json.loads(t.project_settings)
            except ValueError:
                t.global_settings = {}
        if "3D/3dmodel.model" in names:
            m = re.search(r'<metadata name="Application">([^<]*)</metadata>',
                          z.read("3D/3dmodel.model").decode("utf-8", "replace"))
            if m and m.group(1).startswith("BambuStudio-"):
                t.application = m.group(1)
        if "Metadata/model_settings.config" in names:
            m = re.search(r'<object id="\d+">.*?<metadata key="extruder" value="(\d+)"/>',
                          z.read("Metadata/model_settings.config").decode("utf-8", "replace"),
                          re.DOTALL)
            if m:
                t.extruder = m.group(1)
    area = t.global_settings.get("printable_area")
    if area:
        pts = np.array([[float(v) for v in p.split("x")] for p in area])
        t.plate_center = tuple(float(v) for v in (pts.min(0) + pts.max(0)) / 2)
    return t


def _f(v: float) -> str:
    s = f"{v:.9g}"
    return "0" if s == "-0" else s


def _mesh_xml(obj_id: int, verts: np.ndarray, faces: np.ndarray, uid: str) -> str:
    out = [f'  <object id="{obj_id}" p:UUID="{uid}" type="model">\n   <mesh>\n    <vertices>\n']
    out += [f'     <vertex x="{_f(x)}" y="{_f(y)}" z="{_f(z)}"/>\n' for x, y, z in verts]
    out.append("    </vertices>\n    <triangles>\n")
    out += [f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in faces]
    out.append("    </triangles>\n   </mesh>\n  </object>\n")
    return "".join(out)


def _t3(t) -> str:
    """3MF 3x4 transform (identity rotation + translation)."""
    return "1 0 0 0 1 0 0 0 1 " + " ".join(_f(v) for v in t)


def _m4(t) -> str:
    """Bambu 'matrix' metadata: 4x4 row-major."""
    x, y, z = (_f(v) for v in t)
    return f"1 0 0 {x} 0 1 0 {y} 0 0 1 {z} 0 0 0 1"


def build_3mf(volumes: list[Volume], object_name: str,
              template: Template | None = None) -> dict[str, bytes]:
    """Return the archive content {path: bytes}. volumes[0] should be the part."""
    t = template or Template()
    n = len(volumes)
    obj_id = n + 1

    all_b = np.array([v.mesh.bounds for v in volumes])
    lo, hi = all_b[:, 0].min(0), all_b[:, 1].max(0)
    origin = (lo + hi) / 2  # object origin: centre of all volumes (as Bambu does)
    parts_zmin = min(v.mesh.bounds[0, 2] for v in volumes if v.subtype == "normal_part")

    # 3D/Objects/object_1.model: one centred mesh per volume
    meshes, comps, offsets = [], [], []
    for i, v in enumerate(volumes, start=1):
        c = v.mesh.bounds.mean(0)
        offsets.append(c)
        verts = np.asarray(v.mesh.vertices) - c
        meshes.append(_mesh_xml(i, verts, np.asarray(v.mesh.faces),
                                f"000{i:05x}-81cb-4c03-9d28-80fed5dfa1dc"))
        comps.append((i, c - origin))
    object_model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" {NS}>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        " <resources>\n" + "".join(meshes) + " </resources>\n <build/>\n</model>\n")

    # 3D/3dmodel.model: the assembly and the build item
    item_t = (t.plate_center[0], t.plate_center[1], origin[2] - parts_zmin)
    comp_xml = "".join(
        f'    <component p:path="/3D/Objects/object_1.model" objectid="{i}" '
        f'p:UUID="000{i - 1:05x}-b206-40ff-9872-83e8017abed1" transform="{_t3(tr)}"/>\n'
        for i, tr in comps)
    meta = "".join(f' <metadata name="{k}">{v}</metadata>\n' for k, v in [
        ("Application", t.application), ("BambuStudio:3mfVersion", "1"),
        ("Copyright", ""), ("Description", ""), ("Designer", ""), ("License", ""),
        ("Title", object_name)])
    main_model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" {NS}>\n' + meta +
        " <resources>\n"
        f'  <object id="{obj_id}" p:UUID="{obj_id:08x}-61cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
        "   <components>\n" + comp_xml + "   </components>\n  </object>\n </resources>\n"
        ' <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n'
        f'  <item objectid="{obj_id}" p:UUID="{obj_id:08x}-b1ec-4553-aec9-835e5b724bb4" '
        f'transform="{_t3(item_t)}" printable="1"/>\n </build>\n</model>\n')

    # Metadata/model_settings.config: part types and per-part settings
    total_faces = sum(len(v.mesh.faces) for v in volumes)
    parts = []
    for (i, tr), v, c in zip(comps, volumes, offsets):
        uid = uuid.uuid5(_UUID_NS, f"{object_name}/{i}/{v.name}")
        md = [("name", v.name), ("matrix", _m4(tr)), ("source_file", v.name),
              ("source_object_id", "0"), ("source_volume_id", "0"),
              ("source_offset_x", _f(c[0])), ("source_offset_y", _f(c[1])),
              ("source_offset_z", _f(c[2])), ("extruder", t.extruder)]
        md += [(k, str(val)) for k, val in v.settings.items()]
        parts.append(
            f'    <part id="{i}" subtype="{v.subtype}" uuid="{uid}">\n'
            + "".join(f"      <metadata key={quoteattr(k)} value={quoteattr(val)}/>\n"
                      for k, val in md)
            + f'      <mesh_stat face_count="{len(v.mesh.faces)}" edges_fixed="0" '
              'degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
            "    </part>\n")
    assemble = "".join(
        f'   <assemble_item object_id="{obj_id}" volume_id="{i - 1}" transform="{_t3(tr)}" />\n'
        for i, tr in comps)
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        f'  <object id="{obj_id}">\n'
        f"    <metadata key=\"name\" value={quoteattr(object_name)}/>\n"
        f'    <metadata key="extruder" value="{t.extruder}"/>\n'
        f'    <metadata face_count="{total_faces}"/>\n'
        + "".join(parts) + "  </object>\n"
        "  <plate>\n"
        '    <metadata key="plater_id" value="1"/>\n'
        '    <metadata key="plater_name" value=""/>\n'
        '    <metadata key="locked" value="false"/>\n'
        "    <model_instance>\n"
        f'      <metadata key="object_id" value="{obj_id}"/>\n'
        '      <metadata key="instance_id" value="0"/>\n'
        '      <metadata key="identify_id" value="1000"/>\n'
        "    </model_instance>\n  </plate>\n"
        "  <assemble>\n"
        f'   <assemble_item object_id="{obj_id}" instance_id="0" '
        f'transform="{_t3((0, 0, item_t[2]))}" offset="0 0 0" />\n'
        + assemble + "  </assemble>\n</config>\n")

    files = {
        "[Content_Types].xml": CONTENT_TYPES.encode(),
        "_rels/.rels": ROOT_RELS.encode(),
        "3D/3dmodel.model": main_model.encode(),
        "3D/_rels/3dmodel.model.rels": MODEL_RELS.encode(),
        "3D/Objects/object_1.model": object_model.encode(),
        "Metadata/model_settings.config": model_settings.encode(),
    }
    if t.project_settings is not None:
        files["Metadata/project_settings.config"] = t.project_settings
    return files


def write_3mf(path: str | Path, files: dict[str, bytes]):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)


# Sparse infill patterns for which "infill direction" does not give one line
# direction per layer (several directions, curves, or no direction at all):
# alternating 45/135 between regions then has little or no effect.
NON_DIRECTIONAL_PATTERNS = {"grid", "triangles", "tri-hexagon", "cubic", "adaptivecubic",
                            "supportcubic", "quartercubic", "honeycomb", "gyroid",
                            "lightning", "concentric", "hilbertcurve", "archimedeanchords",
                            "octagramspiral"}


def pattern_warning(template: Template | None, modifier_settings: dict) -> str | None:
    pattern = modifier_settings.get("sparse_infill_pattern")
    if pattern is None and template is not None:
        pattern = template.global_settings.get("sparse_infill_pattern")
    if pattern and pattern.lower() in NON_DIRECTIONAL_PATTERNS:
        return (f"sparse infill pattern is '{pattern}': it does not print a single line "
                "direction per layer, so the 45/135 alternation will have little or no "
                "effect on sparse layers. Consider --3mf-setting sparse_infill_pattern=zig-zag")
    return None
