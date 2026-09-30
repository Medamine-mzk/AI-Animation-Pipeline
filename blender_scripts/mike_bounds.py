"""Probe: per-mesh world-space bounds + transforms + armature scale."""

import sys
from pathlib import Path

import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=args[0])
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            xs, ys, zs = [], [], []
            for v in obj.data.vertices:
                w = obj.matrix_world @ v.co
                xs.append(w.x); ys.append(w.y); zs.append(w.z)
            sk = len(obj.data.shape_keys.key_blocks) if obj.data.shape_keys else 0
            print(
                f"[b] {obj.name!r} sk={sk} "
                f"x[{min(xs):9.2f},{max(xs):9.2f}] y[{min(ys):9.2f},{max(ys):9.2f}] "
                f"z[{min(zs):9.2f},{max(zs):9.2f}] loc={tuple(round(a,2) for a in obj.matrix_world.translation)}",
                flush=True,
            )
        elif obj.type == "ARMATURE":
            print(f"[b] ARMATURE {obj.name!r} loc={tuple(round(a,2) for a in obj.matrix_world.translation)}", flush=True)
    return 0


sys.exit(main())
