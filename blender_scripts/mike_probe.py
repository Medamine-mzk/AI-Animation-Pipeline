"""Probe: full shape-key list, bone names, and material/texture bindings."""

import sys
from pathlib import Path

import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx = args[0]
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=fbx)
    print(f"[mike] fbx={Path(fbx).name}", flush=True)
    for obj in bpy.context.scene.objects:
        if obj.type == "ARMATURE":
            names = [b.name for b in obj.data.bones]
            print(f"[mike] ARMATURE bones({len(names)}): {', '.join(names)}", flush=True)
        elif obj.type == "MESH":
            sk = []
            if obj.data.shape_keys is not None:
                sk = [kb.name for kb in obj.data.shape_keys.key_blocks]
            print(f"[mike] MESH {obj.name!r} verts={len(obj.data.vertices)}", flush=True)
            if sk:
                print(f"[mike]   shape_keys({len(sk)}): {', '.join(sk)}", flush=True)
            for mat in obj.data.materials:
                if mat is None:
                    continue
                imgs = []
                if mat.node_tree:
                    for node in mat.node_tree.nodes:
                        if node.type == "TEX_IMAGE" and node.image is not None:
                            imgs.append(f"{node.image.name} filepath={node.image.filepath!r}")
                print(f"[mike]   mat {mat.name!r}: {', '.join(imgs) if imgs else '(no image)'}", flush=True)
    return 0


sys.exit(main())
