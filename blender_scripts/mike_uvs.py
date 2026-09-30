"""Probe: UV layers, vertex colors, and UV bounds per mesh."""

import sys

import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=args[0])
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        uvs = [(u.name, len(u.data)) for u in obj.data.uv_layers]
        vcols = [c.name for c in obj.data.color_attributes]
        print(f"[u] mesh {obj.name!r} uv_layers={uvs} color_attrs={vcols}", flush=True)
        if uvs:
            uv = obj.data.uv_layers[0].data
            uvals = [c.uv.x for c in uv] + [c.uv.y for c in uv]
            print(f"[u]   uv min={min(uvals):.3f} max={max(uvals):.3f}", flush=True)
    return 0


sys.exit(main())
