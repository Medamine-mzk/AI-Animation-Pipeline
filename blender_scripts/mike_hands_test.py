"""Debug: render X_open with NO arm pose (rest T-pose) and with pose, for hand comparison."""

import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bpy

import spike_render as sr


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx, out = args[0], args[1]
    scene = bpy.context.scene
    sr.import_fbx(fbx)
    sr.apply_textures(scene, r"E:\PROJECTS\AI Animation Pipeline\media\3d\mike\textures")
    sr.fit_camera(scene)
    sr.setup_camera(scene)
    sr.setup_lights(scene)
    sr.setup_render(scene)
    Path(out).mkdir(parents=True, exist_ok=True)
    scene.render.filepath = out + r"\X_tpose.png"
    bpy.ops.render.render(write_still=True)
    sr.lower_arms(scene)
    scene.render.filepath = out + r"\X_posed.png"
    bpy.ops.render.render(write_still=True)
    print("[test] done", flush=True)
    return 0


sys.exit(main())