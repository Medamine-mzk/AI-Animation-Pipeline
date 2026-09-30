"""Probe: deformed (depsgraph) hand bbox vs pants bbox for a given pose target.

Usage: mike_hand_deform.py -- fbx y_target [y_target ...]
"""

import sys
from mathutils import Matrix, Vector
import bpy

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import spike_render as sr


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx = args[0]
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=fbx)
    scene = bpy.context.scene
    sr.prune_export_duplicates(scene)
    objects = {o.name: o for o in scene.objects}
    body = objects.get("Body")
    pants = objects.get("Pants")
    sr.lower_arms(scene)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    def deformed_bbox(obj, group_names):
        idx = set()
        for g in obj.vertex_groups:
            if any(k in g.name.lower() for k in group_names):
                idx.update(v.index for v in obj.data.vertices
                           if g.index in {vg.group for vg in v.groups} and
                           any(vg.weight > 0.05 for vg in v.groups if vg.group == g.index))
        ev = obj.evaluated_get(dg)
        mw = ev.matrix_world
        xs, ys, zs = [], [], []
        for i in idx:
            c = mw @ ev.data.vertices[i].co
            xs.append(c.x); ys.append(c.y); zs.append(c.z)
        return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))
    hx, hy, hz = deformed_bbox(body, ["hand"])
    px, py, pz = deformed_bbox(pants, ["leg"])
    print(f"[d] hand x[{hx[0]:.3f},{hx[1]:.3f}] y[{hy[0]:.3f},{hy[1]:.3f}] z[{hz[0]:.3f},{hz[1]:.3f}]", flush=True)
    print(f"[d] pants x[{px[0]:.3f},{px[1]:.3f}] y[{py[0]:.3f},{py[1]:.3f}] z[{pz[0]:.3f},{pz[1]:.3f}]", flush=True)
    return 0


sys.exit(main())