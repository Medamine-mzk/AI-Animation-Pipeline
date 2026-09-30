"""Probe: dump what an FBX import actually produces (objects, meshes, shape keys)."""

import sys
from pathlib import Path

import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx = args[0]
    legacy = "--legacy" in args
    bpy.ops.object.select_all(action="DESELECT")
    if legacy:
        bpy.ops.import_scene.fbx_legacy(filepath=fbx)
    else:
        bpy.ops.import_scene.fbx(filepath=fbx)
    print(f"[probe] fbx={fbx} legacy={legacy}", flush=True)
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            names = []
            if obj.data.shape_keys is not None:
                names = [kb.name for kb in obj.data.shape_keys.key_blocks]
            print(
                f"[probe] MESH {obj.name!r} verts={len(obj.data.vertices)} "
                f"shape_keys={len(names)} {names[:12]}",
                flush=True,
            )
        elif obj.type == "ARMATURE":
            bones = [b.name for b in obj.data.bones]
            print(f"[probe] ARMATURE {obj.name!r} bones={len(bones)} {bones[:6]}", flush=True)
        else:
            print(f"[probe] {obj.type} {obj.name!r}", flush=True)
    return 0


sys.exit(main())