"""Quick test: render X_open from FRONT (+Y) vs BACK (-Y) and compare skin."""

import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bpy
from mathutils import Vector

import spike_render as sr


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx, out_y1, out_y2 = args[0], args[1], args[2]
    for out in (out_y1, out_y2):
        Path(out).mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    sr.import_fbx(fbx)
    sr.lower_arms(scene)
    sr.apply_textures(scene, r"E:\PROJECTS\AI Animation Pipeline\media\3d\mike\textures")
    sr.fit_camera(scene)
    sr.setup_camera(scene)
    sr.setup_lights(scene)
    sr.setup_render(scene)
    for obj in scene.objects:
        if obj.type == "CAMERA" and obj.name == "SpikeCam":
            if args[0].endswith("back"):
                pass
            break
    cam = scene.objects["SpikeCam"]
    cam.location = (0.0, -4.0, sr.CAMS["center_z"])
    cam.rotation_euler = (math.pi / 2, 0.0, 0.0)
    scene.render.filepath = out_y1 + r"\X_front.png"
    bpy.ops.render.render(write_still=True)
    cam.location = (0.0, 4.0, sr.CAMS["center_z"])
    cam.rotation_euler = (math.pi / 2, 0.0, math.pi)
    scene.render.filepath = out_y2 + r"\X_back.png"
    bpy.ops.render.render(write_still=True)
    print("[test] done", flush=True)
    return 0


sys.exit(main())