"""Phase 1 Step 1.2: drive a cast of rigged characters with lip-sync + emotion clips.

Two modes:

1) Single character (Phase 0 path): imports one FBX and animates face
   blendshapes from a viseme slice, blinks, breathing, a camera cut.
       blender -b -P scene_automator.py -- <fbx> <outdir> [options]

2) Cast (Phase 1): reads jobs/golden/cast.json (characters + emotion clips)
   and script.json (lines with emotions + camera shots), imports every
   character, generates ARKit shape keys via the face morph factory, retargets
   the emotion clip per character onto NLA tracks, drives per-speaker lip-sync
   from visemes.json, and switches cameras per script.json shots.
       blender -b -P scene_automator.py --cast <cast.json> <outdir> \
           --start 32.0 --end 45.0 [--fps 24] [--script <script.json>]

All paths absolute - Blender CWD is unreliable.
"""

import json
import math
import os
import sys
import time
from pathlib import Path

import bpy
from mathutils import Vector, Euler

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spike_render  # reuses lower_arms / import_fbx / prune helpers
import face_morph_factory  # build_morph_keys
import animation_bridge  # retarget_clip

VISEMES = "ABCDEFGHX"
WEIGHTS_NAME = "viseme_weights_mike.json"
WEIGHTS_RPM_NAME = "viseme_weights_rpm.json"
OUT_W, OUT_H = 1920, 1080
SENSOR_W = 36.0
FOCAL = 20.0
MAX_CAM_DIST = 6.0  # backdrop wall is now at y=-20, so the camera can pull back freely
HEAD_DIST = 0.95  # talking-head close-up distance (head + shoulders fill the frame)
DIALOGUE_FOCAL = 12.0  # wider lens so two partners fit at the room's shallow pull-back
DIALOGUE_DIST_MIN = 0.42  # camera must stay north of the front lamp rail (y>=-2.92)
DIALOGUE_DIST_MAX = 0.58
BACKDROP_GLB = "assets/big_room.glb"
ROOM_CEILING = 2.35  # target interior height in meters after scaling the room


def log(msg: str) -> None:
    print(f"[scene] {msg}", flush=True)


def parse_args(argv: list[str]) -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {
        "fbx": None, "out": None, "visemes": None, "speaker": "SPEAKER_00",
        "start": 32.0, "end": 37.0, "fps": 24, "cut_frame": None,
        "cast": None, "script": None, "only": None, "frame": None, "group": False,
        "quick": False, "resume": None, "backdrop": None, "stride": 1,
        "line_emotions": None, "listener": True, "light": None,
    }
    pos = [a for a in argv if not a.startswith("-")]
    for a in argv:
        if a.startswith("--visemes="):
            opts["visemes"] = a.split("=", 1)[1]
        elif a.startswith("--speaker="):
            opts["speaker"] = a.split("=", 1)[1]
        elif a.startswith("--start="):
            opts["start"] = float(a.split("=", 1)[1])
        elif a.startswith("--end="):
            opts["end"] = float(a.split("=", 1)[1])
        elif a.startswith("--fps="):
            opts["fps"] = int(a.split("=", 1)[1])
        elif a.startswith("--cut-frame="):
            opts["cut_frame"] = int(a.split("=", 1)[1])
        elif a.startswith("--cast="):
            opts["cast"] = a.split("=", 1)[1]
        elif a.startswith("--script="):
            opts["script"] = a.split("=", 1)[1]
        elif a.startswith("--only="):
            opts["only"] = a.split("=", 1)[1]
        elif a.startswith("--frame="):
            opts["frame"] = int(a.split("=", 1)[1])
        elif a.startswith("--group"):
            opts["group"] = True
        elif a.startswith("--quick"):
            opts["quick"] = True
        elif a.startswith("--resume="):
            opts["resume"] = int(a.split("=", 1)[1])
        elif a.startswith("--backdrop="):
            opts["backdrop"] = a.split("=", 1)[1]
        elif a.startswith("--stride="):
            opts["stride"] = int(a.split("=", 1)[1])
        elif a.startswith("--line-emotions="):
            opts["line_emotions"] = a.split("=", 1)[1]
        elif a.startswith("--no-listener-reactions"):
            opts["listener"] = False
        elif a.startswith("--light="):
            opts["light"] = a.split("=", 1)[1]
    if opts["cast"] is None and pos:
        opts["fbx"] = pos[0]
        opts["out"] = pos[1] if len(pos) > 1 else None
    elif opts["cast"] is not None:
        opts["out"] = pos[0] if pos else None
    return opts


def build_room(scene: bpy.types.Scene, repo=None, backdrop: str | None = None) -> None:
    """Import the Big Room GLB backdrop and fit it to our stage.

    - Scales the model (authored in cm) so the interior ceiling reaches
      ``ROOM_CEILING`` meters.
    - The floor surface sits at z=0 in the raw model, so a uniform scale about
      the origin keeps it at z=0; we then re-center the room on the origin.
    - Drops the Sketchfab preview ``Cube``, ``Light`` and ``Camera`` objects.
    """
    model = (repo if repo is not None else Path(__file__).resolve().parents[1]) / (backdrop or BACKDROP_GLB)
    if not model.exists():
        log(f"WARNING: backdrop not found: {model}; falling back to studio room")
        return
    bpy.ops.import_scene.gltf(filepath=str(model))
    for o in list(bpy.data.objects):
        if o.name in ("Cube", "Light", "Camera"):
            for coll in list(o.users_collection):
                coll.objects.unlink(o)
            bpy.data.objects.remove(o, do_unlink=True)

    meshes = [o for o in scene.objects if o.type == "MESH"]
    if not meshes:
        log("WARNING: backdrop import produced no meshes")
        return
    bpy.context.view_layer.update()
    root = next((o for o in scene.objects
                 if o.name == "Sketchfab_model" and o.type == "EMPTY"), None)

    def world_bounds():
        xs, ys, zs = [], [], []
        for o in meshes:
            for c in o.bound_box:
                p = o.matrix_world @ Vector(c)
                xs.append(p.x); ys.append(p.y); zs.append(p.z)
        return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)

    xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
    is_fitted = "_fitted" in str(model)
    if is_fitted:
        log(f"fitted GLB detected, skipping scale (shared artifact)")
        scale = 1.0
    else:
        scale = ROOM_CEILING / zmax
        if root is not None:
            root.scale = (root.scale[0] * scale, root.scale[1] * scale, root.scale[2] * scale)
        else:
            for o in meshes:
                o.scale = (o.scale[0] * scale, o.scale[1] * scale, o.scale[2] * scale)
        bpy.context.view_layer.update()
        xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
        if root is not None:
            root.location = (-(xmin + xmax) / 2.0, -(ymin + ymax) / 2.0, 0.0)
        bpy.context.view_layer.update()
        xmin, xmax, ymin, ymax, zmin, zmax = world_bounds()
    # ensure double-sided for fitted as well
    for o in meshes:
        for m in o.data.materials:
            if m:
                m.use_backface_culling = False
                try: m.shadow_method = 'NONE'
                except: pass
    log(f"backdrop fitted: x[{xmin:.2f},{xmax:.2f}] y[{ymin:.2f},{ymax:.2f}] "
        f"z[{zmin:.2f},{zmax:.2f}] height={zmax - zmin:.2f}m meshes={len(meshes)} fitted={is_fitted}")


def setup_lights(scene: bpy.types.Scene, preset: str | None = None) -> None:
    def light(name: str, kind: str, loc, strength, size=3.0,
              color=(1.0, 0.98, 0.94), ang=None) -> None:
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

    # lighting presets L1-L6 — single source tools/lighting_presets.json (fallback inline)
    try:
        import json
        preset_path = Path(__file__).resolve().parents[1] / "tools" / "lighting_presets.json"
        j = json.loads(preset_path.read_text(encoding="utf-8"))
        presets = {k: v for k, v in j.items() if k != "meta"}
        # adapt render_single_frame scale (key 25) to automator scale (key 900) if needed
        for k, v in presets.items():
            if "key" in v and v["key"] < 100:
                v["key"] = v["key"] * 36
                v["fill"] = v.get("fill", 0) * 60
                v["rim"] = v.get("rim", 0) * 75
                if "hemi" in v:
                    v["sun"] = v.get("hemi", 0) * 0.25
                v.setdefault("fill2", v.get("fill", 0) * 0.5)
                if isinstance(v.get("world"), list):
                    v["world"] = tuple(v["world"])
                if isinstance(v.get("gtao"), list):
                    v["gtao"] = tuple(v["gtao"])
        p = presets.get(str(preset or "L2"), presets["L2"])
    except Exception as e:
        presets = {
            "L1": {"key": 500, "fill": 150, "fill2": 0, "rim": 180, "sun": 0, "world": (0.02, 0.02, 0.025, 0.0), "exposure": 0.0, "gtao": (False, 0.0)},
            "L2": {"key": 900, "fill": 500, "fill2": 400, "rim": 300, "sun": 1.6, "world": (0.06, 0.06, 0.07, 0.45), "exposure": 0.15, "gtao": (True, 0.35)},
            "L3": {"key": 1200, "fill": 700, "fill2": 500, "rim": 350, "sun": 2.0, "world": (0.08, 0.08, 0.09, 0.55), "exposure": 0.25, "gtao": (True, 0.35)},
            "L4": {"key": 1800, "fill": 900, "fill2": 700, "rim": 450, "sun": 4.5, "world": (0.12, 0.12, 0.13, 0.9), "exposure": 0.7, "gtao": (True, 0.6)},
            "L5": {"key": 1000, "fill": 600, "fill2": 450, "rim": 300, "sun": 1.2, "world": (0.07, 0.065, 0.06, 0.5), "exposure": 0.2, "gtao": (True, 0.35)},
            "L6": {"key": 1100, "fill": 600, "fill2": 500, "rim": 320, "sun": 1.8, "world": (0.06, 0.07, 0.08, 0.45), "exposure": 0.15, "gtao": (True, 0.35)},
        }
        p = presets.get(str(preset or "L2"), presets["L2"])
    # default now L1 per user choice (was L2)
    log(f"lighting preset={preset or 'L1'} key={p['key']} world={p['world'][:3]} exp={p['exposure']}")

    light("Key", "AREA", (0.8, -1.2, 2.05), p["key"], size=3.5)
    if p["fill"] > 0:
        light("Fill", "AREA", (-1.2, -0.9, 1.9), p["fill"], size=3.0, color=(1.0, 0.95, 0.88))
    if p["fill2"] > 0:
        light("Fill2", "AREA", (1.4, 0.8, 1.85), p["fill2"], size=3.0, color=(0.82, 0.88, 1.0))
    if p["rim"] > 0:
        light("Rim", "SPOT", (0.4, 2.0, 2.6), p["rim"],
              ang=(math.radians(-45), 0.0, math.radians(90)))
    if p["sun"] > 0:
        light("Sun", "SUN", (0.0, 0.0, 8.0), p["sun"], color=(1.0, 0.99, 0.95))

    scene.world = bpy.data.worlds.new("SceneWorld")
    try:
        scene.world.use_nodes = True
        bg = scene.world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs[0].default_value = (p["world"][0], p["world"][1], p["world"][2], 1.0)
            bg.inputs[1].default_value = p["world"][3]
    except Exception:
        pass
    scene.world.color = (p["world"][0], p["world"][1], p["world"][2])
    try:
        scene.view_settings.view_transform = "Filmic"
        scene.view_settings.look = "High Contrast"
        scene.view_settings.exposure = p["exposure"]
        scene.view_settings.gamma = 1.0
    except Exception:
        pass
    try:
        scene.eevee.use_gtao = bool(p["gtao"][0])
        scene.eevee.gtao_distance = float(p["gtao"][1])
        scene.eevee.use_bloom = False
    except Exception:
        pass


def setup_render(scene: bpy.types.Scene, quick: bool = False) -> None:
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    if quick:
        scene.render.resolution_x, scene.render.resolution_y = 1280, 720
        scene.render.image_settings.quality = 80
    else:
        scene.render.resolution_x = OUT_W
        scene.render.resolution_y = OUT_H
        scene.render.image_settings.quality = 92
    scene.render.image_settings.file_format = "JPEG"
    scene.render.fps = 24
    eevee = scene.eevee
    samples = 8 if quick else 24
    if hasattr(eevee, "render_samples"):
        eevee.render_samples = samples
    elif hasattr(eevee, "taa_render_samples"):
        eevee.taa_render_samples = samples
    if quick:
        log(f"quick mode: {scene.render.resolution_x}x{scene.render.resolution_y} "
            f"@{samples} samples, jpeg {scene.render.image_settings.quality}")


def find_armature(scene: bpy.types.Scene):
    return next((o for o in scene.objects if o.type == "ARMATURE"), None)


def find_face_mesh(scene: bpy.types.Scene):
    """Mesh with the most shape keys (the character body)."""
    best, best_n = None, -1
    for o in scene.objects:
        if o.type != "MESH" or o.hide_render:
            continue
        n = len(o.data.shape_keys.key_blocks) if o.data.shape_keys is not None else 0
        if n > best_n:
            best, best_n = o, n
    return best


def bone_stem(name: str) -> str:
    """'mixamorig5:Head' -> 'head' (handles 'mixamorig:Head' and 'mixamorig_Head')."""
    return name.split(":")[-1].strip().lower()


def head_bone_name(arm) -> str | None:
    for b in arm.data.bones:
        if bone_stem(b.name) == "head":
            return b.name
    return None


def head_world_for(arm, mesh: bpy.types.Object | None) -> Vector:
    """World position of the head (via Head bone, else top of mesh bounds)."""
    if arm is not None:
        hb = head_bone_name(arm)
        if hb is not None:
            return arm.matrix_world @ arm.data.bones[hb].head_local
    if mesh is not None:
        mw = mesh.matrix_world
        xs = [mw @ v.co for v in mesh.data.vertices]
        xs.sort(key=lambda p: p.z)
        top = xs[-int(len(xs) * 0.02):]
        cx = sum(p.x for p in top) / len(top)
        cz = sum(p.z for p in top) / len(top)
        cy = sum(p.y for p in top) / len(top)
        return Vector((cx, cy, cz))
    return Vector((0.0, 0.0, 1.6))


def aim_at(obj: bpy.types.Object, target: Vector) -> None:
    d = (target - obj.location).normalized()
    obj.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


def make_camera(scene: bpy.types.Scene, name: str, head: Vector,
                dist: float, lateral: float, height: float,
                focal: float | None = None) -> bpy.types.Object:
    dat = bpy.data.cameras.new(name)
    dat.lens = FOCAL if focal is None else focal
    dat.sensor_width = SENSOR_W
    dat.dof.use_dof = True
    dat.dof.aperture_fstop = 2.8
    dat.dof.focus_distance = dist
    obj = bpy.data.objects.new(name, dat)
    scene.collection.objects.link(obj)
    obj.location = (head.x + lateral, head.y - dist, head.z + height)
    aim_at(obj, head)
    return obj


def fullbody_dist(height: float, margin: float = 1.7) -> float:
    """Distance so a character of ``height`` fills the vertical frame."""
    sens_h = SENSOR_W * OUT_H / OUT_W
    half = math.atan(sens_h / (2.0 * FOCAL))
    return (height * margin) / (2.0 * math.tan(half))


def body_world_for(member: dict) -> tuple[Vector, float]:
    """World-space center + height of a cast member in its current pose."""
    arm = member["arm"]
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    verts = []
    for o in bpy.context.scene.objects:
        if o.type != "MESH" or o.parent is not arm or o.hide_render:
            continue
        ev = o.evaluated_get(dg)
        mw = o.matrix_world
        for v in ev.data.vertices:
            verts.append(mw @ v.co)
    if not verts:
        return Vector((0.0, 0.0, 0.8)), 1.6
    zmin = min(v.z for v in verts)
    zmax = max(v.z for v in verts)
    cx = sum(v.x for v in verts) / len(verts)
    cy = sum(v.y for v in verts) / len(verts)
    return Vector((cx, cy, (zmin + zmax) / 2.0)), zmax - zmin


def group_world_for(members: list[dict]) -> tuple[Vector, float]:
    """World-space center + combined height of all cast members."""
    xs, ys, zmin, zmax = [], [], None, None
    for m in members:
        center, height = body_world_for(m)
        xs.append(center.x)
        ys.append(center.y)
        if zmin is None or center.z - height / 2 < zmin:
            zmin = center.z - height / 2
        if zmax is None or center.z + height / 2 > zmax:
            zmax = center.z + height / 2
    return Vector((sum(xs) / len(xs), sum(ys) / len(ys),
                   (zmin + zmax) / 2.0)), zmax - zmin


def fullbody_camera_for(member: dict, scene: bpy.types.Scene) -> bpy.types.Object:
    """Camera that fits the member's full body (feet to head), torso-centered."""
    center, height = body_world_for(member)
    dist = fullbody_dist(height)
    return make_camera(scene, f"Cam_{member['id']}", center,
                       dist=dist, lateral=0.35, height=0.0)


def head_camera_for(member: dict, scene: bpy.types.Scene) -> bpy.types.Object:
    """Talking-head camera aimed at the member's head (close-up)."""
    head = head_world_for(member["arm"], member["mesh"])
    dist = HEAD_DIST * member["arm"].scale[0]
    return make_camera(scene, f"CamHead_{member['id']}", head,
                       dist=dist, lateral=0.15, height=0.05)


def dialogue_camera_for(scene: bpy.types.Scene, name: str = "Cam_Dialogue",
                        focal: float | None = None) -> bpy.types.Object:
    """Create a two-shot camera object (repositioned per frame via place_dialogue_cam).

    Uses a wider lens (``DIALOGUE_FOCAL``) and a short distance so the camera
    stays inside the room (front wall at y=-3.44) and north of the front lamp
    rail (y>=-2.92) that would otherwise sit between camera and cast.
    """
    return make_camera(scene, name, Vector((0.0, 0.0, 1.0)),
                       dist=DIALOGUE_DIST_MIN, lateral=0.0, height=0.0,
                       focal=FOCAL if focal is None else focal)


def place_dialogue_cam(cam: bpy.types.Object, members: list[dict],
                       focus: dict, dist_override: float | None = None) -> None:
    """Reposition ``cam`` to frame every ``members`` head, DOF on ``focus``."""
    heads = [head_world_for(m["arm"], m["mesh"]) for m in members]
    cx = sum(h.x for h in heads) / len(heads)
    cz = sum(h.z for h in heads) / len(heads)
    cy = min(h.y for h in heads)
    xs = [h.x for h in heads]
    span = max(xs) - min(xs)
    half_h = math.atan((SENSOR_W / 2.0) / DIALOGUE_FOCAL)
    if dist_override is not None:
        dist = min(max(dist_override, DIALOGUE_DIST_MIN), DIALOGUE_DIST_MAX)
    else:
        dist = ((span / 2.0) + 0.35) / math.tan(half_h)
        dist = min(max(dist, DIALOGUE_DIST_MIN), DIALOGUE_DIST_MAX)
    cam.data.lens = DIALOGUE_FOCAL
    cam.location = (cx, cy - dist, cz)
    aim_at(cam, Vector((cx, cy, cz)))
    fh = head_world_for(focus["arm"], focus["mesh"])
    cam.data.dof.focus_distance = (cam.location - fh).length


def set_member_facing(member: dict, target: Vector | None,
                      toward_camera: float = 0.25) -> None:
    """Rotate a member (about Z) to face ``target``, blended toward the camera.

    ``toward_camera`` in [0,1] pulls the facing direction from the target
    direction toward the camera (-Y). Pass ``target=None`` to face the camera.
    """
    arm = member["arm"]
    # glTF/GLB armatures import with rotation_mode=QUATERNION, where writing
    # rotation_euler is a silent no-op; force XYZ so the facing rotation applies.
    arm.rotation_mode = "XYZ"
    if target is None:
        arm.rotation_euler.z = 0.0
        return
    d = target - arm.location
    d.z = 0.0
    if d.length < 1e-4:
        arm.rotation_euler.z = 0.0
        return
    d.normalize()
    cam = Vector((0.0, -1.0, 0.0))
    face = (d * (1.0 - toward_camera) + cam * toward_camera).normalized()
    # default facing is -Y; after rotation theta a facing vector is (sin, -cos)
    arm.rotation_euler.z = math.atan2(face.x, -face.y)


def set_member_visible(member: dict, visible: bool) -> None:
    """Show/hide a cast member in render (armature + every parented mesh).

    Blender does not propagate ``hide_render`` from parent to children, so each
    mesh parented to the armature must be toggled individually.
    """
    scene = bpy.context.scene
    member["arm"].hide_render = not visible
    for o in scene.objects:
        if o.type == "MESH" and o.parent is member["arm"]:
            o.hide_render = not visible


def keyframe_face(scene: bpy.types.Scene, mesh: bpy.types.Object,
                  keys: list[str], weights: dict, frame: int) -> None:
    if mesh.data.shape_keys is None:
        return
    for name in keys:
        kb = mesh.data.shape_keys.key_blocks.get(name)
        if kb is None:
            continue
        kb.value = float(weights.get(name, 0.0))
        kb.keyframe_insert(data_path="value", frame=frame)


def drive_lipsync(scene: bpy.types.Scene, mesh: bpy.types.Object,
                  segments: list[dict], weights: dict, fps: int,
                  t0: float = 0.0) -> list[str]:
    keys = set()
    for seg in segments:
        keys.update(weights.get(seg["shape"].lower(), {}).keys())
    keys = sorted(keys)
    if not keys:
        log("drive_lipsync: no keys (no segments or empty weights)")
        return []
    keyframe_face(scene, mesh, keys, {}, 1)
    for seg in segments:
        f = max(1, int(round((seg["start"] - t0) * fps)))
        w = weights.get(seg["shape"].lower(), {})
        keyframe_face(scene, mesh, keys, w, f)
    log(f"lipsync keyframed {len(segments)} segments x {len(keys)} keys")
    return keys


def blink_key_names(mesh: bpy.types.Object, rpm: bool) -> tuple[str, str]:
    """Blink shape-key names for a mesh: RPM avatars use ARKit names."""
    if rpm:
        return "eyeBlinkLeft", "eyeBlinkRight"
    return "Blink_Left", "Blink_Right"


def add_blinks(scene: bpy.types.Scene, mesh: bpy.types.Object,
               times: list[float], fps: int, t0: float = 0.0,
               rpm: bool = False) -> None:
    if mesh.data.shape_keys is None:
        return
    bl = blink_key_names(mesh, rpm)
    for kb_name in bl:
        kb = mesh.data.shape_keys.key_blocks.get(kb_name)
        if kb is None:
            log(f"blink key {kb_name} missing; skipping blinks")
            return
    for t in times:
        for kb_name in bl:
            kb = mesh.data.shape_keys.key_blocks.get(kb_name)
            f0 = max(1, int(round((t - t0 - 0.04) * fps)))
            f1 = max(f0 + 1, int(round((t - t0 + 0.02) * fps)))
            f2 = max(f1 + 1, int(round((t - t0 + 0.10) * fps)))
            kb.value = 0.0
            kb.keyframe_insert(data_path="value", frame=f0)
            kb.value = 1.0
            kb.keyframe_insert(data_path="value", frame=f1)
            kb.value = 0.0
            kb.keyframe_insert(data_path="value", frame=f2)
    log(f"blinks at {[round(t - t0, 2) for t in times]}")


def add_breathing(scene: bpy.types.Scene, fps: int, start: float, end: float) -> None:
    arm = find_armature(scene)
    if arm is None:
        return
    pb = arm.pose.bones.get("mixamorig_Spine1")
    if pb is None:
        pb = arm.pose.bones.get("Spine1")
    if pb is None:
        return
    pb.rotation_mode = "XYZ"
    f0 = max(1, int(round(start * fps)))
    f1 = max(f0 + 1, int(round(end * fps)))
    n = f1 - f0
    for i in range(n):
        f = f0 + i
        t = f / fps
        pb.rotation_euler = (0.02 * math.sin(2 * math.pi * 0.35 * (t - start)), 0.0, 0.0)
        pb.keyframe_insert(data_path="rotation_euler", frame=f)
    log(f"breathing keyframed frames {f0}..{f1}")


def strip_root_motion(action) -> None:
    """Zero the root (Hips) bone's location fcurves in a retargeted action.

    Mixamo walk/run clips carry root translation that would fight our explicit
    per-frame ``arm.location`` animation during the walk-in. We keep the root
    *rotation* (the walk's natural bob) but strip its translation channels so
    the clip plays "in place" while the armature is translated by the scene.
    """
    root_stem = "Hips"
    for fc in animation_bridge.iter_fcurves(action):
        m = animation_bridge.BONE_PATH.search(fc.data_path)
        if not m or animation_bridge.stem(m.group(1)) != root_stem:
            continue
        if "location" not in fc.data_path:
            continue
        for kp in fc.keyframe_points:
            kp.co.y = 0.0
            kp.handle_left.y = 0.0
            kp.handle_right.y = 0.0


def import_cast_member(cast: dict, repo: Path, out_dir: Path, clip_paths: list[str] | None = None):
    """Import one character, generate morphs, retarget emotion clips to NLA tracks.

    Supports Mixamo FBX characters (procedural morph keys) and Ready Player Me
    GLB avatars (native ARKit/viseme shape keys, no morph generation).

    Returns {id, arm, mesh, rpm, tracks:{emotion: track}}.
    """
    scene = bpy.context.scene
    model = repo / cast["fbx"]
    before = {o for o in scene.objects if o.type == "ARMATURE"}
    if model.suffix.lower() in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=str(model))
        # RPM GLBs ship a material-less helper Icosphere; hide it from render
        for o in scene.objects:
            if o.type == "MESH" and o.name == "Icosphere" and not o.data.materials:
                o.hide_render = True
                o.hide_set(True)
    else:
        spike_render.import_fbx(str(model))
    arm = next((o for o in scene.objects
                if o.type == "ARMATURE" and o not in before), None)
    if arm is None:
        log(f"ERROR: no armature in {model.name}")
        return None

    rpm = False
    cands = face_morph_factory.face_mesh_candidates(scene, arm=arm)
    mesh = None
    if cands:
        # RPM avatars carry their shape keys on AvatarHead; prefer the mesh
        # that actually has viseme/ARKit keys over the heaviest body rig.
        rpm_cands = [m for m in cands
                     if m.data.shape_keys
                     and {"aa", "ih", "jawOpen", "eyeBlinkLeft"} & set(
                         m.data.shape_keys.key_blocks.keys())]
        if rpm_cands:
            mesh = max(rpm_cands,
                       key=lambda m: (len(m.data.shape_keys.key_blocks),
                                      len(face_morph_factory.head_weighted_verts(m))))
            rpm = True
        else:
            mesh = face_morph_factory.pick_face_mesh(cands)
    if mesh is None:
        log(f"ERROR: no face mesh in {model.name}")
        return None
    if not rpm:
        face_morph_factory.build_morph_keys(scene, mesh, arm=arm, out_dir=out_dir)
    # emotion clips provide natural arm poses; do NOT call lower_arms here
    f = float(cast.get("scale", 0.85))
    arm.scale = (arm.scale[0] * f, arm.scale[1] * f, arm.scale[2] * f)
    # park the character while other cast members are imported
    arm.location = (len(scene.objects) * 3.0, 0.0, 0.0)

    ad = arm.animation_data_create()
    if ad.action is not None:
        log(f"cast {cast['id']}: cleared active action {ad.action.name} "
            f"so NLA strips drive the pose")
        ad.action = None
    tracks = {}
    clip_len = {}
    default_stem = "Idle"
    for clip_rel in clip_paths:
        clip_path = repo / clip_rel
        if not clip_path.exists():
            log(f"WARNING: clip not found: {clip_path}")
            continue
        stem = clip_path.stem
        action = animation_bridge.retarget_clip(str(clip_path), arm,
                                                f"{cast['id']}_{stem}")
        track = ad.nla_tracks.new()
        track.name = f"clip_{stem}"
        strip = track.strips.new(action.name, 1, action)
        strip.repeat = 1
        strip.use_auto_blend = False
        tracks[stem] = track
        fr = action.frame_range
        clip_len[stem] = max(1, int(round(fr[1] - fr[0] + 1)))

    intro_track = None
    intro_len = 0
    intro_clip = cast.get("intro")
    if intro_clip:
        clip_path = repo / intro_clip
        if clip_path.exists():
            action = animation_bridge.retarget_clip(str(clip_path), arm,
                                                    f"{cast['id']}_intro")
            intro_track = ad.nla_tracks.new()
            intro_track.name = "intro"
            strip = intro_track.strips.new(action.name, 1, action)
            strip.repeat = 1
            strip.use_auto_blend = False
            fr = action.frame_range
            intro_len = max(1, int(round(fr[1] - fr[0] + 1)))
        else:
            log(f"WARNING: intro clip not found: {clip_path}")
    log(f"cast {cast['id']} ready: arm={arm.name} mesh={mesh.name} "
        f"rpm={rpm} tracks={list(tracks)} intro={'yes' if intro_track else 'no'}")
    return {"id": cast["id"], "arm": arm, "mesh": mesh, "rpm": rpm, "tracks": tracks,
            "clip_len": clip_len, "intro_track": intro_track, "intro_len": intro_len,
            "x": cast.get("x"), "y": cast.get("y")}


def place_cast(members: list[dict], scene: bpy.types.Scene) -> None:
    """Place cast members on the floor, feet at z=0.

    Uses per-character ``x``/``y`` from the cast json when present (keeps them
    clear of furniture), otherwise spreads them on x.
    """
    n = len(members)
    for i, m in enumerate(members):
        arm = m["arm"]
        # move to origin first to measure rest height from the floor
        arm.location = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()
        zmin = min(
            (o.matrix_world @ v.co).z
            for o in scene.objects
            if o.type == "MESH" and o.parent is arm and not o.hide_render
            for v in o.data.vertices
        )
        if m["x"] is not None:
            x, y = float(m["x"]), float(m["y"] if m["y"] is not None else 0.0)
        else:
            x, y = (i - (n - 1) / 2) * 0.95, 0.0
        arm.location = (x, y, -zmin)
        m["x"] = x
        m["y"] = y
        m["slot"] = (x, y)
        m["base_z"] = -zmin
    log(f"placed {n} cast members: x={[round(m['x'], 2) for m in members]}")


def camera_for_member(member: dict, scene: bpy.types.Scene) -> bpy.types.Object:
    return head_camera_for(member, scene)


def active_emotion(member: dict, lines: list[dict], t: float, default="neutral") -> str:
    for line in lines:
        if line["character_id"] == member["id"] and line["start"] <= t < line["end"]:
            return line.get("emotion", default)
    return default


SCRIPT_TO_GO = {
    "neutral": "neutral", "happy": "joy", "sad": "sadness", "angry": "anger",
    "surprised": "surprise", "worried": "nervousness", "excited": "excitement",
    "annoyed": "annoyance",
}


def load_clip_catalog(repo: Path) -> dict:
    cat_path = repo / "jobs" / "golden" / "clip_catalog.json"
    return json.loads(cat_path.read_text(encoding="utf-8"))


def select_line_clips(cast_data: dict, lines: list[dict],
                      line_emotions: dict, catalog: dict) -> tuple[dict, dict]:
    """Auto-assign an animation clip to every line, rotating per emotion.

    Returns ``(line_clips, used_clips)`` where ``line_clips`` is
    ``{character_id: {line_start: clip_path}}`` and ``used_clips`` is
    ``{character_id: {clip_name}}`` (the clips that must be imported).
    Emotion comes from the go_emotions classifier when available, else the
    static ``emotion`` field; candidates come from the clip catalog and are
    picked deterministically (rotating per occurrence so repeated emotions
    reuse different clips across the character's lines).
    """
    cdir = catalog["clips_dir"]
    overrides = catalog.get("line_overrides", {})
    listener_pool = catalog.get("listener_pool", [])
    line_clips: dict[str, dict[int, str]] = {}
    used: dict[str, set] = {}
    counters: dict[tuple[str, str], int] = {}
    by_char: dict[str, list[dict]] = {}
    for line in lines:
        by_char.setdefault(line["character_id"], []).append(line)

    for cid, clines in by_char.items():
        line_clips[cid] = {}
        used[cid] = set()
        prev_clip = None
        for line in sorted(clines, key=lambda l: l["start"]):
            key = round(line["start"], 2)
            det = line_emotions.get(f"{key:.2f}")
            label = det["top"] if det else SCRIPT_TO_GO.get(line.get("emotion", "neutral"), "neutral")
            override = overrides.get(f"{key:.2f}")
            if override:
                clip = override
            else:
                pool = catalog["go_emotions"].get(label) or catalog["go_emotions"]["neutral"]
                n = counters.get((cid, label), 0)
                clip = pool[n % len(pool)]
                if len(pool) > 1 and clip == prev_clip:
                    n += 1
                    clip = pool[n % len(pool)]
                counters[(cid, label)] = n + 1
            prev_clip = clip
            line_clips[cid][key] = f"{cdir}/{clip}"
            used[cid].add(f"{cdir}/{clip}")
        used[cid].add(f"{cdir}/{catalog['listener']}")
        for c in listener_pool:
            used[cid].add(f"{cdir}/{c}")
    return line_clips, used


def speaker_at(lines: list[dict], t: float) -> str | None:
    """id of the character speaking at time ``t``, else None (gap)."""
    for line in lines:
        if line["start"] <= t < line["end"]:
            return line["character_id"]
    return None


def set_emotion_track(member: dict, clip_stem: str, intro: bool = False) -> None:
    ad = member["arm"].animation_data
    for name, track in member["tracks"].items():
        track.mute = True
    if member.get("intro_track") is not None:
        member["intro_track"].mute = True
    if intro and member.get("intro_track") is not None:
        member["intro_track"].mute = False
    elif clip_stem in member["tracks"]:
        member["tracks"][clip_stem].mute = False
    elif member.get("default_track") is not None:
        member["default_track"].mute = False


def active_clip_stem(member: dict, lines: list[dict], t: float) -> str:
    """Clip (by file stem) for the line active at ``t``, else the default idle."""
    clips = member.get("clips", {})
    for line in lines:
        if line["character_id"] == member["id"] and line["start"] <= t < line["end"]:
            path = clips.get(round(line["start"], 2))
            if path:
                return Path(path).stem
            break
    return member.get("default_clip_stem", "Idle")


def apply_member_clip(member: dict, i: int, clip_stem: str, start_frame: int,
                      cover_frames: int, intro: bool = False) -> None:
    """Make the member's clip actually play: position its NLA strip so it starts
    at the line's start frame and loops to cover the line, then mute everything
    else. Re-positions only when (clip, start) changes, so the action keeps
    advancing across frames within a line."""
    key = (clip_stem, start_frame)
    if member.get("_clip_key") != key:
        if intro and member.get("intro_track") is not None:
            length = member.get("intro_len") or 1
            strip = member["intro_track"].strips[0]
            strip.frame_start = start_frame
            strip.repeat = max(1, int(round(cover_frames / length)))
        elif clip_stem in member["tracks"]:
            length = member["clip_len"].get(clip_stem, 1)
            strip = member["tracks"][clip_stem].strips[0]
            strip.frame_start = start_frame
            strip.repeat = max(1, int(round(cover_frames / length)))
        member["_clip_key"] = key
    set_emotion_track(member, clip_stem, intro=intro)


def apply_dialogue_clip(member: dict, i: int, fps: int) -> None:
    """Reposition + enable the clip for the member's active line at frame ``i``."""
    for win in member.get("line_windows", []):
        start_frame, end_frame, stem = win
        if start_frame <= i <= end_frame:
            apply_member_clip(member, i, stem, start_frame, end_frame - start_frame + 1)
            return


def build_line_windows(member: dict, lines: list[dict], fps: int, t0: float, t1: float) -> None:
    wins = []
    for line in lines:
        if line["character_id"] != member["id"]:
            continue
        if not (t0 <= line["start"] < t1):
            continue
        start_frame = int(round((line["start"] - t0) * fps)) + 1
        end_frame = max(start_frame, int(round((line["end"] - t0) * fps)))
        stem = active_clip_stem(member, lines, line["start"])
        wins.append((start_frame, end_frame, stem))
    wins.sort()
    member["line_windows"] = wins
    member["_clip_key"] = None


def build_listener_windows(member: dict, lines: list[dict], fps: int,
                           t0: float, t1: float,
                           pool: list[str] | None = None) -> None:
    """Assign a subtle reaction clip to every line a *different* character
    speaks inside the slice (short interjections skipped), rotating through
    the pool without repeating the previous reaction."""
    pool = pool or ["Nodding", "Agreeing"]
    wins = []
    counter = 0
    prev = None
    for line in sorted(lines, key=lambda l: l["start"]):
        if line["character_id"] == member["id"]:
            continue
        if not (t0 <= line["start"] < t1):
            continue
        if line["end"] - line["start"] < 1.0:
            continue
        start_frame = int(round((line["start"] - t0) * fps)) + 1
        end_frame = max(start_frame, int(round((line["end"] - t0) * fps)))
        stem = pool[counter % len(pool)]
        if stem == prev and len(pool) > 1:
            counter += 1
            stem = pool[counter % len(pool)]
        counter += 1
        prev = stem
        wins.append((start_frame, end_frame, stem))
    wins.sort()
    member["listener_windows"] = wins
    member["_listener_key"] = None


def apply_listener_clip(member: dict, i: int, fps: int) -> None:
    """Enable a subtle reaction clip while someone else speaks, else idle."""
    for win in member.get("listener_windows", []):
        start_frame, end_frame, stem = win
        if start_frame <= i <= end_frame:
            apply_member_clip(member, i, stem, start_frame,
                              end_frame - start_frame + 1)
            return
    if member.get("default_track") is not None:
        apply_member_clip(member, i, member["default_clip_stem"], 1,
                          max(1, int(bpy.context.scene.frame_end)))


def intro_root_end(member: dict, local_frames: int) -> Vector | None:
    """World offset of the walk clip's root at ``local_frames`` (its end frame
    inside the walk-in window), so the arm can start there and the clip's own
    root motion carries the character to the slot.

    Returns None when the intro action has no Hips location fcurves (caller
    falls back to the old glide). Only X/Y are used; the arm z stays at base_z.
    """
    track = member.get("intro_track")
    if track is None or not track.strips:
        return None
    action = track.strips[0].action
    if action is None:
        return None
    fr = action.frame_range
    loc0 = Vector((0.0, 0.0, 0.0))
    loc1 = Vector((0.0, 0.0, 0.0))
    hit = False
    for fc in animation_bridge.iter_fcurves(action):
        m = animation_bridge.BONE_PATH.search(fc.data_path)
        if not m or animation_bridge.stem(m.group(1)) != "Hips":
            continue
        if "location" not in fc.data_path:
            continue
        hit = True
        axis = fc.data_path[-1]
        if axis not in "xyz":
            continue
        idx = "xyz".index(axis)
        loc0[idx] = fc.evaluate(fr[0])
        loc1[idx] = fc.evaluate(fr[0] + local_frames)
    if not hit:
        return None
    arm = member["arm"]
    d = loc1 - loc0
    d.z = 0.0
    rot = Euler((0.0, 0.0, arm.rotation_euler.z))
    d.rotate(rot)
    return d


def run_cast_main(opts: dict) -> int:
    repo = Path(__file__).resolve().parents[1]
    cast_path = Path(opts["cast"])
    if not cast_path.is_absolute() or not cast_path.exists():
        log(f"ERROR: cast json must be an absolute existing path, got {opts['cast']!r}")
        return 1
    if not opts["out"] or not Path(opts["out"]).is_absolute():
        log(f"ERROR: outdir must be absolute, got {opts['out']!r}")
        return 1
    script_path = Path(opts["script"]) if opts["script"] else repo / "jobs" / "golden" / "script.json"
    script = json.loads(Path(script_path).read_text(encoding="utf-8"))
    visemes_path = Path(opts["visemes"]) if opts["visemes"] else repo / "jobs" / "golden" / "visemes.json"
    visemes = json.loads(visemes_path.read_text(encoding="utf-8"))
    weights = json.loads((Path(__file__).parent / WEIGHTS_NAME).read_text(encoding="utf-8"))
    weights_rpm = json.loads((Path(__file__).parent / WEIGHTS_RPM_NAME).read_text(encoding="utf-8"))
    cast_data = json.loads(Path(opts["cast"]).read_text(encoding="utf-8"))
    out_dir = Path(opts["out"])
    out_dir.mkdir(parents=True, exist_ok=True)

    only = set(opts["only"].split(",")) if opts["only"] else None
    cast = [c for c in cast_data["characters"]
            if only is None or c["id"] in only]
    if not cast:
        log("ERROR: cast is empty")
        return 1

    scene = bpy.context.scene
    start, end, fps = opts["start"], opts["end"], opts["fps"]
    scene.frame_start = 1
    scene.frame_end = max(1, int(round((end - start) * fps)))

    build_room(scene, repo, opts.get("backdrop"))
    setup_lights(scene, preset=opts.get("light"))
    setup_render(scene, opts.get("quick", False))

    # automatic animation selection: detect emotion per line, pick a clip
    line_emotions = {}
    le_path = Path(opts["line_emotions"]) if opts.get("line_emotions") else \
        repo / "jobs" / "golden" / "line_emotions.json"
    if le_path.exists():
        line_emotions = json.loads(le_path.read_text(encoding="utf-8"))
    catalog = load_clip_catalog(repo)
    line_clips, used_clips = select_line_clips(cast_data, script["lines"],
                                               line_emotions, catalog)
    for cid, mapping in sorted(line_clips.items()):
        log(f"auto clips {cid}: " + ", ".join(
            f"{k:.2f}s->{Path(v).stem}" for k, v in sorted(mapping.items())))

    members = []
    char_out = out_dir / "morphs"
    for c in cast:
        paths = sorted(used_clips.get(c["id"], set()))
        m = import_cast_member(c, repo, char_out / c["id"], clip_paths=paths)
        if m is not None:
            m["clips"] = line_clips.get(c["id"], {})
            m["default_track"] = m["tracks"].get("Idle")
            m["default_clip_stem"] = "Idle"
            build_line_windows(m, script["lines"], fps, opts["start"], opts["end"])
            if opts.get("listener", True):
                pool = [Path(c).stem for c in catalog.get("listener_pool", [])] or ["Nodding", "Agreeing"]
                build_listener_windows(m, script["lines"], fps, opts["start"],
                                       opts["end"], pool)
            else:
                m["listener_windows"] = []
            members.append(m)
    if not members:
        log("ERROR: no cast members imported")
        return 1
    place_cast(members, scene)

    # per-speaker lip-sync from visemes.json, clipped to the slice
    for m in members:
        segments = visemes.get(m["id"], [])
        segments = [s for s in segments
                    if opts["start"] <= s["start"] < opts["end"]]
        if not segments:
            log(f"WARNING: no viseme segments for {m['id']} in slice")
        w = weights_rpm if m.get("rpm") else weights
        keys = drive_lipsync(scene, m["mesh"], segments, w, fps, t0=opts["start"])
        if keys:
            f_end = scene.frame_end
            for name in keys:
                kb = m["mesh"].data.shape_keys.key_blocks.get(name)
                if kb is not None:
                    kb.value = 0.0
                    kb.keyframe_insert(data_path="value", frame=f_end)
        blink_t = [s["start"] + 0.15 for s in segments if s["start"] + 0.15 < s["end"]]
        add_blinks(scene, m["mesh"], blink_t[:4], fps, t0=opts["start"],
                   rpm=m.get("rpm", False))

    lines = script.get("lines", [])

    # ---- shot plan ------------------------------------------------------
    # walk-in windows: prefer dialogue.json walkins (staged entrance), fallback to script gap
    walkins = {}
    try:
        dj = json.loads(Path("jobs/dialogue.json").read_text(encoding="utf-8"))
        for w in dj.get("walkins", []):
            walkins[w["speaker"]] = (float(w["t0"]), float(w["t1"]))
    except: pass
    if not walkins:
        first_line = {}
        for l in sorted(lines, key=lambda l: l["start"]):
            first_line.setdefault(l["character_id"], l)
        for spk, fl in first_line.items():
            prev_end = max([l["end"] for l in lines if l["end"] <= fl["start"]], default=0.0)
            gap = fl["start"] - prev_end
            if gap >= 0.3:
                dur = min(max(gap, 0.8), 2.2)
                t0 = fl["start"] - dur
                if t0 < prev_end: t0 = prev_end
                walkins[spk] = (t0, fl["start"])
    # drop walk-ins for members that have no intro clip loaded
    for spk in [s for s in walkins
                if not any(m["id"] == s and m.get("intro_track") for m in members)]:
        del walkins[spk]
    # First speaker solo: no walk-in for earliest first line
    if walkins:
        earliest = min(walkins.items(), key=lambda kv: kv[1][1])
        # Also drop if its t0 is 0 (first line)
        first_speaker = min(first_line, key=lambda k: first_line[k]["start"]) if 'first_line' in locals() else None
        if first_speaker and first_speaker in walkins and walkins[first_speaker][0] < 0.5:
            del walkins[first_speaker]
    log(f"shot plan: walk-ins={walkins}")

    def present(spk: str, t: float) -> bool:
        if any(l["character_id"] == spk and l["start"] <= t for l in lines):
            return True
        if spk in walkins and t >= walkins[spk][0]:
            return True
        # First speaker solo: earliest speaker present from t=0
        try:
            earliest = min(lines, key=lambda l: l["start"])["character_id"]
            if spk == earliest:
                return True
        except: pass
        return False

    def dialogue_partner(spk: str, t: float, gap: float = 3.0) -> str | None:
        """Other speaker whose line is closest in time to the active line."""
        active = next((l for l in lines if l["start"] <= t < l["end"]), None)
        if active is None:
            ended = [l for l in lines if l["end"] <= t]
            active = max(ended, key=lambda l: l["end"]) if ended else None
        if active is None or active["character_id"] != spk:
            return None
        best, best_d = None, 1e9
        for l in lines:
            if l["character_id"] == spk:
                continue
            if l["end"] < active["start"] - gap or l["start"] > active["end"] + gap:
                continue
            d = min(abs(l["start"] - active["start"]),
                    abs(l["end"] - active["end"]))
            if d < best_d:
                best_d, best = d, l
        return best["character_id"] if best else None

    def walkin_at(t: float) -> str | None:
        for spk, (t0, t1) in walkins.items():
            if t0 <= t < t1:
                return spk
        return None

    def set_location(m: dict, x: float | None = None, y: float | None = None) -> None:
        sx, sy = m["slot"]
        m["arm"].location = (x if x is not None else sx,
                             y if y is not None else sy, m["base_z"])
    # ---------------------------------------------------------------------

    # one camera per member, repositioned per frame (avoids object churn)
    cams = {m["id"]: camera_for_member(m, scene) for m in members}
    d_cam = dialogue_camera_for(scene)

    group_cam = None
    if opts.get("group"):
        center, height = group_world_for(members)
        dist = min(fullbody_dist(height), MAX_CAM_DIST, DIALOGUE_DIST_MAX)
        group_cam = make_camera(scene, "Cam_Group", center,
                                dist=dist, lateral=0.0, height=0.0,
                                focal=DIALOGUE_FOCAL)
        log(f"group camera: center={tuple(round(v, 2) for v in center)} "
            f"dist={dist:.2f} height={height:.2f}")

    report = {"frames": scene.frame_end, "fps": fps, "per_frame_s": []}
    stride = max(1, int(opts.get("stride", 1)))
    frame_list = list(range(1, scene.frame_end + 1, stride))
    if opts.get("resume"):
        frame_list = [f for f in frame_list if f >= int(opts["resume"])]
    if opts.get("frame"):
        frame_list = [opts["frame"]]
    cur_focus = next((m["id"] for m in members), None)
    for i in frame_list:
        t = start + (i - 1) / fps
        # Staged entrance visibility: hide not-yet-present
        for m in members:
            vis = present(m["id"], t)
            if m["arm"].hide_render != (not vis):
                m["arm"].hide_render = not vis
                for o in scene.objects:
                    if o.parent is m["arm"]:
                        o.hide_render = not vis
                try:
                    m["arm"].keyframe_insert(data_path="hide_render", frame=i)
                except: pass
        w_spk = walkin_at(t)
        for m2 in members:
            if w_spk == m2["id"]:
                t0, t1 = walkins[w_spk]
                apply_member_clip(m2, i, "neutral",
                                  int(round((t0 - start) * fps)) + 1,
                                  max(1, int(round((t1 - t0) * fps))),
                                  intro=True)
            elif speaker_at(lines, t) == m2["id"]:
                apply_dialogue_clip(m2, i, fps)
            elif opts.get("listener", True):
                apply_listener_clip(m2, i, fps)
            else:
                apply_dialogue_clip(m2, i, fps)
        scene.frame_set(i)
        if group_cam is not None:
            cam = group_cam
        else:
            w_spk = walkin_at(t)
            if w_spk is not None:
                # --- walk-in: the walk clip's own root motion carries the
                # character into their slot (real strides, no eased glide) ---
                wm = next(m for m in members if m["id"] == w_spk)
                t0, t1 = walkins[w_spk]
                sx, sy = wm["slot"]
                set_member_facing(wm, Vector((sx, sy, 0.0)), toward_camera=0.15)
                off = intro_root_end(wm, max(1, int(round((t1 - t0) * fps))))
                if off is None:
                    set_location(wm, sx + 1.6, sy - 0.05)
                else:
                    set_location(wm, sx - off.x, sy - off.y)
                place_dialogue_cam(d_cam, [wm], wm, dist_override=0.5)
                cam = d_cam
            else:
                # active speaker (persist the last speaker through gaps)
                active = next((l for l in lines if l["start"] <= t < l["end"]), None)
                if active is None:
                    ended = [l for l in lines if l["end"] <= t]
                    active = max(ended, key=lambda l: l["end"]) if ended else None
                if active is None:
                    focus = cur_focus
                else:
                    focus = active["character_id"]
                    cur_focus = focus
                partner = dialogue_partner(focus, t) if active is not None else None
                if partner is not None and present(partner, t):
                    # --- dialogue: two-shot, partners facing each other ---
                    # T6: keep girl (SPEAKER_01) visible during dad->mom dialogue
                    trio = None
                    if focus == "SPEAKER_00" and partner == "SPEAKER_02":
                        girl = next((m for m in members if m["id"] == "SPEAKER_01"), None)
                        if girl is not None and present("SPEAKER_01", t):
                            trio = girl
                    fm = next(m for m in members if m["id"] == focus)
                    pm = next(m for m in members if m["id"] == partner)
                    members_for_cam = [fm, pm] + ([trio] if trio else [])
                    for m2 in members_for_cam:
                        set_location(m2)
                        # ensure girl stays visible (not hidden by listener logic)
                        set_member_visible(m2, True)
                    set_member_facing(fm, Vector((pm["slot"][0], pm["slot"][1], 0.0)),
                                      toward_camera=0.25)
                    set_member_facing(pm, Vector((fm["slot"][0], fm["slot"][1], 0.0)),
                                      toward_camera=0.25)
                    if trio:
                        # girl looks toward dad
                        set_member_facing(trio, Vector((fm["slot"][0], fm["slot"][1], 0.0)), toward_camera=0.15)
                    place_dialogue_cam(d_cam, members_for_cam, fm)
                    cam = d_cam
                else:
                    # --- solo: talking-head close-up on the active speaker ---
                    m = next((m for m in members if m["id"] == focus), members[0])
                    set_location(m)
                    set_member_facing(m, None)
                    cam = cams[m["id"]]
                    head = head_world_for(m["arm"], m["mesh"])
                    hd = HEAD_DIST * m["arm"].scale[0]
                    cam.location = (head.x + 0.15, head.y - hd, head.z + 0.05)
                    aim_at(cam, head)
                    cam.data.dof.focus_distance = hd
        scene.camera = cam
        scene.render.filepath = str(out_dir / f"frame_{i:06d}.jpg")
        t0 = time.time()
        bpy.ops.render.render(write_still=True)
        dt = time.time() - t0
        report["per_frame_s"].append(round(dt, 3))
        if i % 10 == 0 or i == scene.frame_end:
            log(f"frame {i}/{scene.frame_end} rendered in {dt:.2f}s")

    avg = sum(report["per_frame_s"]) / max(1, len(report["per_frame_s"]))
    report["avg_s"] = round(avg, 3)
    report["total_s"] = round(sum(report["per_frame_s"]), 2)
    (out_dir / "spike_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    log(f"done: {len(frame_list)} frames -> {out_dir} avg {avg:.2f}s/frame "
        f"total {report['total_s']}s")
    return 0


def main() -> int:
    opts = parse_args(sys.argv)
    if opts["cast"] is not None:
        return run_cast_main(opts)
    if not opts["fbx"] or not Path(opts["fbx"]).is_absolute() or not Path(opts["fbx"]).exists():
        log(f"ERROR: FBX must be an absolute existing path, got {opts['fbx']!r}")
        return 1
    if not opts["out"] or not Path(opts["out"]).is_absolute():
        log(f"ERROR: outdir must be absolute, got {opts['out']!r}")
        return 1
    out_dir = Path(opts["out"])
    out_dir.mkdir(parents=True, exist_ok=True)

    repo = Path(__file__).resolve().parents[1]
    weights_path = Path(__file__).parent / WEIGHTS_NAME
    weights = json.loads(weights_path.read_text(encoding="utf-8"))
    visemes_path = Path(opts["visemes"]) if opts["visemes"] else repo / "jobs" / "golden" / "visemes.json"
    visemes = json.loads(visemes_path.read_text(encoding="utf-8"))
    segments = visemes.get(opts["speaker"], [])
    segments = [s for s in segments if s["start"] >= opts["start"] and s["start"] < opts["end"]]
    if not segments:
        log(f"WARNING: no viseme segments for {opts['speaker']} in slice")

    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = max(1, int(round((opts["end"] - opts["start"]) * opts["fps"])))
    cut_frame = opts["cut_frame"] if opts["cut_frame"] else max(1, scene.frame_end // 2)

    spike_render.import_fbx(opts["fbx"])
    arm = find_armature(scene)
    mesh = find_face_mesh(scene)
    if mesh is None:
        log("ERROR: no mesh found")
        return 1
    log(f"armature={arm.name if arm else None} mesh={mesh.name} "
        f"shapekeys={len(mesh.data.shape_keys.key_blocks) if mesh.data.shape_keys else 0}")
    spike_render.lower_arms(scene)
    build_room(scene, repo)
    setup_lights(scene, preset=opts.get("light"))
    setup_render(scene, opts.get("quick", False))

    # camera pair (medium -> closer, angled) around the head
    head = head_world_for(arm, mesh)
    cam_a = make_camera(scene, "CamA", head, dist=1.9, lateral=0.15, height=0.02)
    cam_b = make_camera(scene, "CamB", head, dist=1.15, lateral=-0.62, height=-0.28)

    keys = drive_lipsync(scene, mesh, segments, weights, opts["fps"], t0=opts["start"])
    blink_t = [t + 0.15 for s in segments for t in (s["start"],) if t + 0.15 < s["end"]]
    add_blinks(scene, mesh, blink_t[:4], opts["fps"], t0=opts["start"])
    add_breathing(scene, opts["fps"], opts["start"], opts["end"])

    # reset face at the end of the slice so nothing holds past the last segment
    if keys:
        f_end = scene.frame_end
        for name in keys:
            kb = mesh.data.shape_keys.key_blocks.get(name)
            if kb is not None:
                kb.value = 0.0
                kb.keyframe_insert(data_path="value", frame=f_end)

    report = {"frames": scene.frame_end, "fps": opts["fps"], "per_frame_s": []}
    for i in range(1, scene.frame_end + 1):
        scene.frame_set(i)
        scene.camera = cam_b if i >= cut_frame else cam_a
        scene.render.filepath = str(out_dir / f"frame_{i:06d}.jpg")
        t0 = time.time()
        bpy.ops.render.render(write_still=True)
        dt = time.time() - t0
        report["per_frame_s"].append(round(dt, 3))
        if i % 10 == 0 or i == scene.frame_end:
            log(f"frame {i}/{scene.frame_end} rendered in {dt:.2f}s")

    avg = sum(report["per_frame_s"]) / len(report["per_frame_s"])
    report["avg_s"] = round(avg, 3)
    report["total_s"] = round(sum(report["per_frame_s"]), 2)
    (out_dir / "spike_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    log(f"done: {scene.frame_end} frames -> {out_dir} avg {avg:.2f}s/frame "
        f"total {report['total_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
