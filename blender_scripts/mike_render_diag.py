"""Diagnose: what mesh content renders vs what fit_camera measures.

Renders X_open twice: ortho 2.16 (old wide framing) and auto-fit; prints the
evaluated world bbox of every non-hidden mesh so we can match content.
"""

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
    sr.lower_arms(scene)
    sr.apply_textures(scene, r"E:\PROJECTS\AI Animation Pipeline\media\3d\mike\textures")
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        if obj.hide_render:
            print(f"[v] {obj.name}: HIDDEN", flush=True)
            continue
        ev = obj.evaluated_get(dg)
        mw = ev.matrix_world
        xs, ys, zs = [], [], []
        for v in ev.data.vertices:
            c = mw @ v.co
            xs.append(c.x); ys.append(c.y); zs.append(c.z)
        print(f"[v] {obj.name}: x[{min(xs):.3f},{max(xs):.3f}] y[{min(ys):.3f},{max(ys):.3f}] z[{min(zs):.3f},{max(zs):.3f}]", flush=True)
    Path(out).mkdir(parents=True, exist_ok=True)
    sr.CAMS["ortho_scale"] = 2.16
    sr.CAMS["center_z"] = 0.9
    sr.setup_camera(scene)
    sr.setup_lights(scene)
    sr.setup_render(scene)
    scene.render.filepath = out + r"\X_forced216.png"
    bpy.ops.render.render(write_still=True)
    print("[v] rendered forced ortho 2.16", flush=True)
    return 0


sys.exit(main())