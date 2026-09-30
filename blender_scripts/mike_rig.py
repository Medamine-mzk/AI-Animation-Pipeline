"""Probe: armature rest-pose bone head positions (world) + Body armature modifier."""

import sys
from mathutils import Vector
import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=args[0])
    for obj in bpy.context.scene.objects:
        if obj.type != "ARMATURE":
            continue
        awm = obj.matrix_world
        for name in ("mixamorig_Hips", "mixamorig_Neck", "mixamorig_Head",
                     "mixamorig_LeftArm", "mixamorig_RightArm",
                     "mixamorig_LeftUpLeg", "mixamorig_LeftToe_End"):
            b = obj.data.bones.get(name)
            if b is None:
                print(f"[r] bone {name}: MISSING", flush=True)
                continue
            head = awm @ b.head_local
            tail = awm @ b.tail_local
            print(f"[r] bone {name}: head={tuple(round(c, 3) for c in head)} "
                  f"tail={tuple(round(c, 3) for c in tail)}", flush=True)
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        mods = [(m.type, m.object.name if m.object else None) for m in obj.modifiers]
        print(f"[r] mesh {obj.name!r} modifiers={mods}", flush=True)
    return 0


sys.exit(main())
