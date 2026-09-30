"""Probe: where do the hands end up after lower_arms (armature-space positions)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bpy
from mathutils import Vector

import spike_render as sr


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    scene = bpy.context.scene
    sr.import_fbx(args[0])
    sr.lower_arms(scene)
    bpy.context.view_layer.update()
    obj = next(o for o in scene.objects if o.type == "ARMATURE")
    awm = obj.matrix_world
    for name in ("mixamorig_LeftArm", "mixamorig_LeftForeArm", "mixamorig_LeftHand",
                 "mixamorig_RightArm", "mixamorig_RightForeArm", "mixamorig_RightHand"):
        pb = obj.pose.bones[name]
        head = awm @ pb.matrix @ Vector((0, 0, 0))
        tail = awm @ pb.matrix @ Vector((0, 1, 0))
        print(f"[p] {name}: head={tuple(round(c, 3) for c in head)} tail={tuple(round(c, 3) for c in tail)}", flush=True)
    for obj in scene.objects:
        if obj.type == "MESH" and obj.data.shape_keys is not None:
            body = obj
    print(f"[p] body world y-span from bind: {tuple(round(c,3) for c in body.matrix_world.translation)}", flush=True)
    return 0


sys.exit(main())