#!/usr/bin/env python3
"""
bake_room.py — bake fitted GLB for shared HTML+Blender use (WYSIWYG).
Uses same logic as render_single_frame.py build_room: scale to 2.35m, center, floor at 0.

Usage:
  python tools/bake_room.py --in assets/background/living_room_with_curtains.glb --out assets/background/living_room_with_curtains_fitted.glb
"""
import argparse, sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
ROOM_CEILING = 2.35

def parse_args():
    argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    return ap.parse_known_args(argv)[0]

def main_blender():
    import bpy
    from mathutils import Vector, Matrix
    args = parse_args()
    inp = (REPO / args.inp) if not Path(args.inp).is_absolute() else Path(args.inp)
    out = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    if not inp.exists():
        print(f"input not found: {inp}"); sys.exit(2)
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    print(f"[bake] importing {inp}")
    bpy.ops.import_scene.gltf(filepath=str(inp))
    debug_dir = out.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    def debug_out(name):
        return str(debug_dir / name)
    for o in list(bpy.data.objects):
        if o.name in ("Cube","Light","Camera"):
            for coll in list(o.users_collection):
                coll.objects.unlink(o)
            bpy.data.objects.remove(o, do_unlink=True)
    meshes = [o for o in bpy.context.scene.objects if o.type=="MESH"]
    if not meshes:
        print("no meshes"); sys.exit(2)
    print(f"[bake] found {len(meshes)} meshes: {[o.name for o in meshes[:3]]} ...")
    for o in meshes:
        print(f"  {o.name}: location={tuple(o.location)}, scale={tuple(o.scale)}, parent={o.parent.name if o.parent else None}")
    # Capture all world matrices before touching hierarchy
    orig = {o.name: o.matrix_world.copy() for o in meshes}
    # Make sure nothing is silently sharing mesh data
    for o in meshes:
        if o.data and o.data.users > 1:
            o.data = o.data.copy()
    # Unparent using the pre-captured matrices (no interleaved reads)
    for o in meshes:
        if o.parent is not None:
            o.parent = None
            o.matrix_world = orig[o.name]
    bpy.context.view_layer.update()
    try:
        bpy.ops.export_scene.gltf(filepath=debug_out("01_after_detach.glb"), export_format='GLB', export_apply=True)
        print("[debug] exported 01_after_detach.glb")
    except Exception as e:
        print(f"[debug] failed 01: {e}")
    # Apply transforms ONCE for the whole selection
    bpy.ops.object.select_all(action='DESELECT')
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True, isolate_users=True)
    bpy.context.view_layer.update()
    try:
        bpy.ops.export_scene.gltf(filepath=debug_out("02_after_apply.glb"), export_format='GLB', export_apply=True)
        print("[debug] exported 02_after_apply.glb")
    except Exception as e:
        print(f"[debug] failed 02: {e}")
    for o in meshes:
        for m in o.data.materials:
            if m:
                m.use_backface_culling = False
                try: m.shadow_method = 'NONE'
                except: pass
    def world_bounds():
        xs, ys, zs = [], [], []
        for o in meshes:
            for c in o.bound_box:
                p = o.matrix_world @ Vector(c)
                xs.append(p.x); ys.append(p.y); zs.append(p.z)
        return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)
    xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
    print(f"[bake] before: x[{xmin:.3f},{xmax:.3f}] y[{ymin:.3f},{ymax:.3f}] z[{zmin:.3f},{zmax:.3f}]")
    scale = ROOM_CEILING / max(zmax - zmin, 1e-6)
    print(f"[bake] scale {scale:.4f}")
    if not (scale == scale):
        print("[ERROR] scale is NaN!"); sys.exit(2)
    scale_mat = Matrix.Scale(scale, 4)
    for o in meshes:
        o.matrix_world = o.matrix_world @ scale_mat
        for i in range(4):
            for j in range(4):
                val = o.matrix_world[i][j]
                if val != val or abs(val) > 1e10:
                    print(f"[ERROR] {o.name} matrix invalid at [{i}][{j}] = {val}")
    bpy.context.view_layer.update()
    try:
        bpy.ops.export_scene.gltf(filepath=debug_out("03_after_scale.glb"), export_format='GLB', export_apply=True)
        print("[debug] exported 03_after_scale.glb")
    except Exception as e:
        print(f"[debug] failed 03: {e}")
    xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
    print(f"[bake] after scale: x[{xmin:.3f},{xmax:.3f}] y[{ymin:.3f},{ymax:.3f}] z[{zmin:.3f},{zmax:.3f}]")
    cx = -(xmin + xmax) / 2.0
    cy = -(ymin + ymax) / 2.0
    cz = -zmin
    print(f"[bake] translate by cx={cx:.3f} cy={cy:.3f} cz={cz:.3f}")
    trans_mat = Matrix.Translation((cx, cy, cz))
    for o in meshes:
        o.matrix_world = trans_mat @ o.matrix_world
    bpy.context.view_layer.update()
    try:
        bpy.ops.export_scene.gltf(filepath=debug_out("04_after_translate.glb"), export_format='GLB', export_apply=True)
        print("[debug] exported 04_after_translate.glb")
    except Exception as e:
        print(f"[debug] failed 04: {e}")
    xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
    print(f"[bake] fitted: x[{xmin:.3f},{xmax:.3f}] y[{ymin:.3f},{ymax:.3f}] z[{zmin:.3f},{zmax:.3f}] height={zmax-zmin:.3f} meshes={len(meshes)}")
    if abs((zmax - zmin) - ROOM_CEILING) > 0.05:
        print(f"[bake] WARNING height {(zmax-zmin):.3f} != {ROOM_CEILING}")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        bpy.ops.export_scene.gltf(filepath=str(out), export_format='GLB', export_apply=True, export_yup=True)
        print(f"[bake] wrote {out} height={zmax-zmin:.3f}")
    except TypeError:
        print(f"[bake] WARNING: export_yup not supported")
        bpy.ops.export_scene.gltf(filepath=str(out), export_format='GLB', export_apply=True)
    print(f"\n[bake] Debug files in: {debug_dir}")
    print(f"  01_after_detach.glb — raw import")
    print(f"  02_after_apply.glb — after transform_apply()")
    print(f"  03_after_scale.glb — after scaling")
    print(f"  04_after_translate.glb — after translation")

try:
    import bpy
    IN_BLENDER=True
except ImportError:
    IN_BLENDER=False

if IN_BLENDER:
    main_blender()
else:
    import subprocess
    ap=parse_args()
    bl = REPO / "tools/blender/blender-5.2.0-windows-x64/blender.exe"
    if not bl.exists():
        bl = next((REPO/"tools/blender").rglob("blender.exe"))
    cmd=[str(bl),"-b","-P",str(Path(__file__).resolve()),"--","--in",ap.inp,"--out",ap.out]
    print("launching blender:", " ".join(cmd))
    sys.exit(subprocess.call(cmd))
