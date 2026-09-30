#!/usr/bin/env python3
"""
render_single_frame.py — render one frame from picker JSON with exact camera/spots
Matches the HTML picker's Three.js lighting and coordinate system.

Usage:
  python tools/render_single_frame.py --picker jobs/picker/living_curtains_picker.json --out C:/tmp/single.jpg --light L2
  blender -b -P tools/render_single_frame.py -- --picker jobs/picker/living_curtains_picker.json --out C:/tmp/single.jpg --light L2
"""
import argparse, json, sys, math
from pathlib import Path

# When run inside Blender, this script is executed with bpy available
try:
    import bpy
    from mathutils import Vector, Euler
    IN_BLENDER = True
except ImportError:
    IN_BLENDER = False


def parse_args_blender():
    argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--picker", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--light", default="L2")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args,_ = ap.parse_known_args(argv)
    return args


def setup_lights(scene, preset="L2"):
    """Three.js-style lighting: Hemisphere + Key + Fill + Rim (matching HTML picker)."""
    import math, bpy, json
    from pathlib import Path
    # Load single source of truth, fallback to inline
    try:
        preset_path = Path(__file__).resolve().parents[1] / "tools" / "lighting_presets.json"
        presets = json.loads(preset_path.read_text(encoding="utf-8"))
        presets = {k: v for k, v in presets.items() if k != "meta"}
    except Exception:
        presets = {
            "L1": {"hemi": 1.6, "amb": 0.5, "key": 5.0, "fill": 2.0, "rim": 1.0, "world": [0.02, 0.02, 0.025, 0.0], "exposure": 0.5, "gtao": [False, 0.0]},
            "L2": {"hemi": 6.0, "amb": 1.5, "key": 25.0, "fill": 8.0, "rim": 4.0, "world": [0.12, 0.12, 0.12, 1.0], "exposure": 0.6, "gtao": [True, 0.35]},
            "L3": {"hemi": 3.6, "amb": 1.1, "key": 20.0, "fill": 8.0, "rim": 4.0, "world": [0.12, 0.12, 0.13, 1.0], "exposure": 0.6, "gtao": [True, 0.35]},
            "L4": {"hemi": 4.4, "amb": 1.4, "key": 25.0, "fill": 10.0, "rim": 7.2, "world": [0.24, 0.24, 0.26, 1.8], "exposure": 1.0, "gtao": [True, 0.6]},
            "L5": {"hemi": 3.2, "amb": 0.9, "key": 12.0, "fill": 4.0, "rim": 2.4, "world": [0.14, 0.13, 0.12, 1.0], "exposure": 0.4, "gtao": [True, 0.35]},
            "L6": {"hemi": 3.2, "amb": 0.9, "key": 12.8, "fill": 4.8, "rim": 3.6, "world": [0.12, 0.14, 0.16, 0.9], "exposure": 0.3, "gtao": [True, 0.35]},
        }
    p = presets.get(preset, presets["L2"])

    def light(name, kind, loc, strength, size=3.0, color=(1.0, 0.98, 0.94), ang=None):
        dat = bpy.data.lights.new(name, kind)
        dat.energy = strength
        dat.color = color
        if kind == "AREA":
            dat.size = size
        obj = bpy.data.objects.new(name, dat)
        scene.collection.objects.link(obj)
        obj.location = loc
        if ang is not None:
            obj.rotation_euler = ang
        return obj

    # Three.js-style lighting: Hemisphere (simulated with two area lights) + Key + Fill + Rim
    # Simulate HemisphereLight with two AREA lights: one upward (sky) and one downward (ground bounce)
    # Sky light (from above, slightly blue)
    sky = bpy.data.lights.new("HemiSky", 'AREA')
    sky.energy = p["hemi"] * 0.5
    sky.color = (0.6, 0.7, 1.0)  # Slightly blue sky
    sky_obj = bpy.data.objects.new("HemiSky", sky)
    scene.collection.objects.link(sky_obj)
    sky_obj.location = (0, 0, 8)
    sky_obj.rotation_euler = (math.radians(180), 0, 0)  # Pointing down
    sky.size = 20

    # Ground bounce (from below, warm)
    ground = bpy.data.lights.new("HemiGround", 'AREA')
    ground.energy = p["hemi"] * 0.5
    ground.color = (1.0, 0.9, 0.7)  # Warm ground bounce
    ground_obj = bpy.data.objects.new("HemiGround", ground)
    scene.collection.objects.link(ground_obj)
    ground_obj.location = (0, 0, -2)
    ground_obj.rotation_euler = (0, 0, 0)  # Pointing up
    ground.size = 20

    # Key light (from top-right-front, like Three.js DirectionalLight at 5,8,4)
    light("Key", "AREA", (5, 8, 4), p["key"], size=3.5)

    # Fill light (from left-bottom-back, cooler)
    light("Fill", "AREA", (-5, 5, -3), p["fill"], size=3.0, color=(0.82, 0.88, 1.0))

    # Rim light (from behind)
    light("Rim", "SPOT", (0, 6, -6), p["rim"],
          ang=(math.radians(-45), 0.0, math.radians(90)))

    # World settings
    scene.world = bpy.data.worlds.new("SceneWorld")
    try:
        scene.world.use_nodes = True
        bg = scene.world.node_tree.nodes.get("Background")
        if bg:
            bg.inputs[0].default_value = (p["world"][0], p["world"][1], p["world"][2], 1.0)
            bg.inputs[1].default_value = p["world"][3]
    except: pass
    scene.world.color = (p["world"][0], p["world"][1], p["world"][2])
    
    try:
        scene.view_settings.view_transform = "Filmic"
        scene.view_settings.look = "High Contrast"
        scene.view_settings.exposure = p["exposure"]
        scene.view_settings.gamma = 1.0
    except: pass
    try:
        scene.eevee.use_gtao = bool(p["gtao"][0])
        scene.eevee.gtao_distance = float(p["gtao"][1])
    except: pass


def build_room(scene, repo, bg_path):
    """Import background GLB and fit it to the stage (like scene_automator.py)."""
    bpy.ops.import_scene.gltf(filepath=str(bg_path))
    
    # Remove default objects
    for o in list(bpy.data.objects):
        if o.name in ("Cube", "Light", "Camera"):
            for coll in list(o.users_collection):
                coll.objects.unlink(o)
            bpy.data.objects.remove(o, do_unlink=True)

    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        print("WARNING: backdrop import produced no meshes", flush=True)
        return
    
    bpy.context.view_layer.update()
    
    def world_bounds():
        xs, ys, zs = [], [], []
        for o in meshes:
            for c in o.bound_box:
                p = o.matrix_world @ Vector(c)
                xs.append(p.x); ys.append(p.y); zs.append(p.z)
        return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)

    xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
    print(f"[single] room bounds (before scale): x[{xmin:.2f},{xmax:.2f}] y[{ymin:.2f},{ymax:.2f}] z[{zmin:.2f},{zmax:.2f}]", flush=True)
    
    # If fitted GLB already baked to 2.35m, skip scale (shared artifact T1)
    is_fitted = "_fitted" in str(bg_path)
    if is_fitted:
        print(f"[single] fitted GLB detected, skipping scale", flush=True)
        scale = 1.0
    else:
        ROOM_CEILING = 2.35
        scale = 2.35 / max(zmax, 1e-6)
        print(f"[single] scale factor: {scale:.4f}", flush=True)
        for o in meshes:
            o.scale = (o.scale[0] * scale, o.scale[1] * scale, o.scale[2] * scale)
        bpy.context.view_layer.update()
        xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
        print(f"[single] room bounds (after scale): x[{xmin:.2f},{xmax:.2f}] y[{ymin:.2f},{ymax:.2f}] z[{zmin:.2f},{zmax:.2f}]", flush=True)
    
    # Center the room on origin (X, Y), floor at Z=0
    center_x = -(xmin + xmax) / 2.0
    center_y = -(ymin + ymax) / 2.0
    
    for o in meshes:
        o.location.x += center_x
        o.location.y += center_y
        o.location.z += -zmin  # Floor at Z=0
    
    bpy.context.view_layer.update()
    
    print(f"[single] backdrop fitted: x[{xmin*scale+center_x:.2f},{xmax*scale+center_x:.2f}] "
          f"y[{ymin*scale+center_y:.2f},{ymax*scale+center_y:.2f}] "
          f"z[0.00,{zmax*scale:.2f}] height={zmax*scale:.2f}m meshes={len(meshes)}", flush=True)
    
    # Make background materials double-sided (no backface culling)
    for o in meshes:
        for m in o.data.materials:
            if m:
                m.use_backface_culling = False
                # Disable shadows from background for performance
                try:
                    m.shadow_method = 'NONE'
                except: pass
    
    return meshes


def main_blender():
    import bpy, math
    from mathutils import Vector
    args = parse_args_blender()
    picker = json.loads(Path(args.picker).read_text(encoding="utf-8"))
    bg = picker.get("background", "/assets/background/living_room_with_curtains.glb").lstrip("/")
    out = Path(args.out)
    
    # Clean scene
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    
    repo = Path(__file__).resolve().parents[1]
    bg = picker.get("background", "/assets/background/living_room_with_curtains.glb").lstrip("/")
    bg_path = repo / bg
    if not bg_path.exists():
        bg_path = Path(bg)
    print(f"[single] background {bg_path}", flush=True)
    
    # Build room (import, scale, center, make double-sided)
    meshes = build_room(bpy.context.scene, repo, bg_path)
    
    scene = bpy.context.scene
    
    # Lights (Three.js style: Hemisphere + Key + Fill + Rim)
    setup_lights(scene, preset=args.light)
    
    # Setup render
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except: 
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 92
    scene.render.film_transparent = False
    if hasattr(scene.eevee, "render_samples"):
        scene.eevee.render_samples = 16
    
    # Room bounds for validation
    def world_bounds():
        xs, ys, zs = [], [], []
        for o in bpy.context.scene.objects:
            if o.type == "MESH":
                for c in o.bound_box:
                    p = o.matrix_world @ Vector(c)
                    xs.append(p.x); ys.append(p.y); zs.append(p.z)
        return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)
    
    xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
    print(f"[single] room bounds: x[{xmin:.2f},{xmax:.2f}] y[{ymin:.2f},{ymax:.2f}] z[{zmin:.2f},{zmax:.2f}]", flush=True)
    
    # Room bounds with margin for character placement
    MARGIN = 0.35
    room_min_x = xmin + 0.35
    room_max_x = xmax - 0.35
    room_min_y = ymin + 0.35
    room_max_y = ymax - 0.35
    
    # Setup render
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except: 
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 92
    scene.render.film_transparent = False
    if hasattr(scene.eevee, "render_samples"):
        scene.eevee.render_samples = 16
    
    # Import characters at spots
    model_map = {"man": "assets/man.glb", "woman": "assets/woman.glb", "boy": "assets/man1.glb", "girl": "assets/woman1.glb"}
    spots = picker.get("spots", [])
    
    for s in spots:
        role = s.get("role", "man")
        glb = model_map.get(role, "assets/man.glb")
        glb_path = repo / glb
        before = {o for o in bpy.context.scene.objects if o.type == "ARMATURE"}
        bpy.ops.import_scene.gltf(filepath=str(glb_path))
        arm = next((o for o in bpy.context.scene.objects if o.type == "ARMATURE" and o not in before), None)
        if not arm:
            print(f"no armature for {role}", flush=True)
            continue
        
        # Viewer spot (x, z) -> Blender (x, y) with y=0 floor -> Blender z=0
        bx = float(s["x"])
        by = float(s["z"])  # viewer z -> blender y
        
        # Validate spot is inside room bounds
        if bx < -3.55 or bx > 3.55 or by < -2.46 or by > 2.46:
            print(f"WARNING: spot {role} at ({bx:.2f},{by:.2f}) outside room bounds, clamping", flush=True)
            bx = max(-3.55, min(3.55, bx))
            by = max(-2.46, min(2.46, by))
        
        yaw = float(s.get("yaw", 0))
        arm.location = (bx, by, 0)
        
        # Find floor offset: ensure feet at 0
        bpy.context.view_layer.update()
        try:
            meshes_arm = [o for o in bpy.context.scene.objects if o.type == "MESH" and o.parent == arm]
            if meshes_arm:
                zvals = []
                for o in meshes_arm:
                    for v in o.data.vertices:
                        zvals.append((o.matrix_world @ v.co).z)
                if zvals:
                    zmin = min(zvals)
                    arm.location = (bx, by, -zmin)
        except: pass
        
        arm.rotation_mode = "XYZ"
        # viewer yaw 0 = +X (east), Blender default facing -Y (south), offset -90 to align arrow with nose
        arm.rotation_euler = (0, 0, math.radians(yaw - 90))
        arm.scale = (0.5, 0.5, 0.5)
        print(f"placed {role} at {bx:.2f},{by:.2f} viewer_yaw {yaw} -> blender_yaw {yaw-90:.1f}", flush=True)
    
    # Camera from picker: viewer (x,y,z) y up -> blender (x, z, y)
    camP = picker["camera"]["position"]
    camT = picker["camera"]["target"]
    fov = picker.get("fov", 45)
    
    # viewer y up -> blender z
    cam_loc = (float(camP["x"]), float(camP["z"]), float(camP["y"]))
    cam_target = (float(camT["x"]), float(camT["z"]), float(camT["y"]))
    print(f"camera viewer pos {camP} -> blender {cam_loc}", flush=True)
    print(f"target viewer {camT} -> blender {cam_target}", flush=True)
    
    # Create camera
    cam_data = bpy.data.cameras.new("PickerCam")
    sensor = 36.0
    cam_data.lens = sensor / (2 * math.tan(math.radians(fov) / 2))
    cam_data.sensor_width = sensor
    cam_obj = bpy.data.objects.new("PickerCam", cam_data)
    scene.collection.objects.link(cam_obj)
    cam_obj.location = cam_loc
    
    # Aim at target
    direction = Vector(cam_target) - Vector(cam_loc)
    cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam_obj
    
    # Render
    scene.render.filepath = str(out)
    bpy.ops.render.render(write_still=True)
    print(f"rendered {out}", flush=True)




if IN_BLENDER:
    main_blender()
else:
    # When run via python (not blender), just delegate to blender
    import subprocess, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--picker", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--light", default="L2")
    args,_ = ap.parse_known_args()
    bl = str((Path(__file__).resolve().parents[1] / "tools/blender/blender-5.2.0-windows-x64/blender.exe"))
    if not Path(bl).exists():
        bl = str(next((Path("tools/blender").rglob("blender.exe"))))
    cmd = [bl, "-b", "-P", str(Path(__file__).resolve()), "--", "--picker", args.picker, "--out", args.out, "--light", args.light]
    print("launching blender:", " ".join(cmd))
    sys.exit(subprocess.call(cmd))