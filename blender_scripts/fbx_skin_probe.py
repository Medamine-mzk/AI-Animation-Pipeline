"""Check armature modifiers + vertex groups on imported CC3 FBX, and test a pose."""

import math
import sys
from pathlib import Path

import bpy


def main() -> int:
    fbx = sys.argv[sys.argv.index("--") + 1]
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=fbx)
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            mods = [(m.type, m.name) for m in obj.modifiers]
            vgroups = len(obj.vertex_groups)
            sk = 0 if obj.data.shape_keys is None else len(obj.data.shape_keys.key_blocks)
            print(f"MESH {obj.name} vgroups={vgroups} shapekeys={sk} modifiers={mods}", flush=True)
            if obj.parent:
                print(f"  parent={obj.parent.name}", flush=True)
    for obj in bpy.context.scene.objects:
        if obj.type == "ARMATURE":
            print(f"ARMATURE {obj.name} bones={len(obj.data.bones)}", flush=True)
            for pb in obj.pose.bones:
                if "Upperarm" in pb.name and "L_" in pb.name:
                    pb.rotation_mode = "XYZ"
                    pb.rotation_euler = (0.0, math.radians(90), 0.0)
                    print(f"  set pose {pb.name}", flush=True)
            bpy.context.view_layer.update()
            for pb in obj.pose.bones:
                if "Upperarm" in pb.name and "L_" in pb.name:
                    m = pb.matrix
                    print(f"  {pb.name} matrix col1={tuple(round(v,2) for v in m[1])}", flush=True)
    return 0


sys.exit(main())