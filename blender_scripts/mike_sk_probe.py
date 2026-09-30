import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import bpy
import spike_render as sr

sr.import_fbx(r"E:\PROJECTS\AI Animation Pipeline\media\3d\mike\source\MikeAlger.fbx")
scene = bpy.context.scene
mesh = sr.pick_mesh(scene)
print("pick_mesh ->", mesh.name if mesh else None)
for obj in scene.objects:
    if obj.type == "MESH":
        sk = obj.data.shape_keys
        print("obj:", obj.name, "sk is not None:", sk is not None,
              "n_keys:", len(sk.key_blocks) if sk else 0)
        if sk and mesh and obj.name == mesh.name:
            kb = sr.find_key(obj, "Jaw_Down")
            print("find_key Jaw_Down ->", kb.name if kb else None)
print("set_weights a ->", sr.set_weights(scene, {"Jaw_Down": 0.9, "MouthOpen": 0.7}, []))
