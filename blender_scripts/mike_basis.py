"""Probe: default pose-bone matrix_basis for arm bones (is a pose baked in?)."""

import sys

import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=args[0])
    arm = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
    for pb in arm.pose.bones:
        n = pb.name.lower()
        if not any(k in n for k in ("leftarm", "rightarm", "leftforearm", "rightforearm", "leftupleg", "head")):
            continue
        mb = pb.matrix_basis
        eul = mb.to_euler("XYZ") if mb else None
        loc = mb.to_translation() if mb else None
        print(f"[b] {pb.name}: mode={pb.rotation_mode} basis_trans={tuple(round(c,4) for c in loc)} "
              f"basis_euler={tuple(round(c,4) for c in eul)}", flush=True)
    return 0


sys.exit(main())