"""Warpage mitigation through infill sectioning (Shen, Veeramani, Qin, 2026).

Pipeline: 3D model (STL/STEP) -> XY footprint -> image moments ->
recursive bisection along the minor axis -> alternating infill angles ->
JSON report, PNG preview and one modifier STL per sub-region.
"""

__version__ = "0.1.0"
