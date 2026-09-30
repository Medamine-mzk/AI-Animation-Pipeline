"""Dump all shape-key names per mesh (full, not truncated) for mapping work."""

import sys
from pathlib import Path

import bpy


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx = argv[0]
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=fbx)
    print(f"# fbx={fbx}", flush=True)
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data.shape_keys is None:
            continue
        names = [kb.name for kb in obj.data.shape_keys.key_blocks]
        print(f"## {obj.name} ({len(names)})", flush=True)
        for n in names:
            if n == "Basis":
                continue
            if any(k in n for k in ("V_", "Mouth", "Eye", "Brow", "Lip", "Cheek", "Jaw", "Nose", "Tongue", "Teeth")):
                print(n, flush=True)
    return 0


sys.exit(main())