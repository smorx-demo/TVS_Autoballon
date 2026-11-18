"""FreeCAD batch: STEP → DXF (edge geometry). Args: input.step output.dxf"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import FreeCAD as App
import Import
import Part


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: freecad_step_to_dxf.py <step> <dxf>", file=sys.stderr)
        return 2
    step_path = Path(sys.argv[1]).resolve()
    dxf_path = Path(sys.argv[2]).resolve()
    if not step_path.is_file():
        return 1
    doc = App.newDocument("StepImport")
    try:
        Import.insert(str(step_path), doc.Name)
        doc.recompute()
        objs = [o for o in doc.Objects if hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()]
        if not objs:
            print("No geometry", file=sys.stderr)
            return 1
        dxf_path.parent.mkdir(parents=True, exist_ok=True)
        Part.export(objs, str(dxf_path))
        bb = objs[0].Shape.BoundBox
        manifest = {
            "source": step_path.name,
            "solids": len(objs),
            "bbox_mm": [round(bb.XLength, 3), round(bb.YLength, 3), round(bb.ZLength, 3)],
        }
        manifest_path = dxf_path.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(manifest))
        return 0
    finally:
        App.closeDocument(doc.Name)


if __name__ == "__main__":
    raise SystemExit(main())