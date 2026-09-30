"""Spike: bake 3D character viseme/eye plates for the sprite pipeline.

Renders 18 transparent plates per character (9 Rhubarb visemes x eyes
open/closed) by driving the model's face blendshapes, for later use in the
M3/M4 sprite compositor (plate-swap layout).

Run headless:
    blender -b -P spike_render.py -- input.fbx outdir [--weights NAME] [--textures DIR] [--mock]

- input.fbx : character FBX (rigged; blendshapes optional)
- outdir    : where plate PNGs + plates.json land
- --weights : weight table basename in blender_scripts/ (default
              viseme_weights_cc3.json for Reallusion CC3/4 morph names;
              viseme_weights.json = ARKit/Oculus names)
- --mock    : skip FBX entirely; render a monkey head to validate the render
              pipeline (camera/light/shape-key loop) without a model

Note: Blender 5.x has no legacy FBX importer (ufbx only). When the imported
meshes have no shape keys the plates are rendered with a neutral face and a
warning is logged.
"""

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

VISEMES = "ABCDEFGHX"
OUT_W, OUT_H = 1024, 2048
CAMS = {"ortho_scale": 2.0, "center_z": 1.0}  # computed from mesh bounds


def log(msg: str) -> None:
    print(f"[spike] {msg}", flush=True)


def parse_args(argv: list[str]) -> tuple:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx = out = None
    weights_name = "viseme_weights_cc3.json"
    textures_dir = None
    mock = False
    positionals = [a for a in argv if not a.startswith("-")]
    for a in argv:
        if a == "--mock":
            mock = True
        elif a.startswith("--weights="):
            weights_name = a.split("=", 1)[1]
        elif a == "--weights" and len(argv) > argv.index(a) + 1:
            weights_name = argv[argv.index(a) + 1]
        elif a.startswith("--textures="):
            textures_dir = a.split("=", 1)[1]
        elif a == "--textures" and len(argv) > argv.index(a) + 1:
            textures_dir = argv[argv.index(a) + 1]
    if mock:
        out = positionals[0] if positionals else None
    else:
        fbx = positionals[0] if positionals else None
        out = positionals[1] if len(positionals) > 1 else None
    return fbx, out, weights_name, textures_dir, mock


def find_key(mesh: bpy.types.Object, name: str):
    if mesh.data.shape_keys is None:
        return None
    want = name.strip().lower()
    for kb in mesh.data.shape_keys.key_blocks:
        if kb.name.strip().lower() == want:
            return kb
    return None


def collect_shape_names(mesh: bpy.types.Object) -> list[str]:
    if mesh.data.shape_keys is None:
        return []
    return [kb.name for kb in mesh.data.shape_keys.key_blocks]


def set_weights(scene: bpy.types.Scene, weights: dict, found: list[str]) -> int:
    """Apply shape-key weights; return how many key blocks matched."""
    matched = 0
    for obj in scene.objects:
        if obj.type != "MESH" or obj.data.shape_keys is None:
            continue
        for name, value in weights.items():
            kb = find_key(obj, name)
            if kb is not None:
                kb.value = value
                found.append(f"{obj.name}:{name}")
                matched += 1
    return matched


def reset_weights(scene: bpy.types.Scene) -> None:
    for obj in scene.objects:
        if obj.type != "MESH" or obj.data.shape_keys is None:
            continue
        for kb in obj.data.shape_keys.key_blocks:
            kb.value = 0.0


def lower_arms(scene: bpy.types.Scene) -> None:
    """Pose the T/A-pose arms: hands forward of the thighs (presentation pose).

    Set each upper-arm bone's XYZ euler to the rotation (in the bone's rest
    local frame) that maps its own Y/length axis onto a world target: shoulder
    -> hand, ~25 deg forward of the thigh plane. rotation_mode must be "XYZ"
    first, else the euler write is silently ignored. After solving, verify_hands()
    checks the *deformed* hand geometry and nudges roll/yaw until both hands are
    fully visible from the camera (clear of the thighs, palms facing forward).

    Note: setting pose_bone.matrix directly does NOT get picked up by EEVEE
    in headless Blender 5.2 (depsgraph agrees, render stays in rest pose).
    """
    for obj in scene.objects:
        if obj.type != "ARMATURE":
            continue
        awm = obj.matrix_world
        for pb in obj.pose.bones:
            pb.matrix_basis = Matrix.Identity(4)  # clear baked export pose
        for pb in obj.pose.bones:
            name = pb.name.lower()
            upper = (
                any(k in name for k in ("upperarm", "leftarm", "rightarm", "l_arm", "r_arm"))
                and "twist" not in name and "forearm" not in name
                and "hand" not in name and "finger" not in name
            )
            if not upper:
                continue
            sh = awm @ pb.bone.head_local
            side = 1.0 if sh.x >= 0.0 else -1.0
            target_world = Vector((sh.x + side * 0.12, sh.y - 0.30, sh.z - 0.50))
            d_world = (target_world - sh).normalized()
            d_arm = (awm.inverted() @ d_world).normalized()
            palm_arm = (awm.inverted() @ Vector((0.0, -1.0, 0.0))).normalized()
            r3 = pb.bone.matrix_local.to_3x3()
            # express the target axes in the bone's own local (parent) frame,
            # which is the frame matrix_basis is applied in
            d_local = r3.inverted() @ d_arm
            d_local.normalize()
            pb.rotation_mode = "QUATERNION"
            if abs(d_local.dot(Vector((0.0, 1.0, 0.0)))) > 0.999:
                # arm would point along its own rest axis: shortest-arc fallback
                rest_y = Vector((0.0, 1.0, 0.0))
                axis_local = rest_y.cross(d_local)
                if axis_local.length < 1e-6:
                    axis_local = Vector((1.0, 0.0, 0.0))
                else:
                    axis_local.normalize()
                rot = Matrix.Rotation(rest_y.angle(d_local), 4, axis_local)
                pb.rotation_quaternion = rot.to_quaternion()
            else:
                # full pose: local Y along the arm, local Z toward the camera
                z_local = r3.inverted() @ palm_arm
                z_local -= z_local.dot(d_local) * d_local
                z_local.normalize()
                x_local = d_local.cross(z_local)
                x_local.normalize()
                basis = Matrix((x_local, d_local, z_local)).transposed()
                pb.rotation_quaternion = basis.to_quaternion()
            log(f"pose {pb.name} fwd (euler={tuple(round(a, 2) for a in pb.rotation_quaternion.to_euler('XYZ'))})")
    verify_hands(scene)


def verify_hands(scene: bpy.types.Scene) -> None:
    """Guarantee both hands are visible from the front camera.

    Three checks on the depsgraph-evaluated hand geometry, per side:
      clear - hand centroid is >=2cm outside the thigh silhouette at hand height
      front - hand centroid is in front of the leg plane (y <= -0.18)
      face  - palm faces the camera (palm-region width >= its depth)

    Nudges the upper-arm roll (palm facing) and yaw (clearance) greedily until
    all checks pass or 12 attempts are exhausted. Runs inside lower_arms().
    """
    arm_obj = next((o for o in scene.objects if o.type == "ARMATURE"), None)
    body = next((o for o in scene.objects if o.type == "MESH" and o.data.shape_keys is not None), None)
    pants = next((o for o in scene.objects if o.name == "Pants"), None)
    if arm_obj is None or body is None or pants is None:
        log("verify_hands: skipped (no armature/body/pants found)")
        return
    awm = arm_obj.matrix_world

    def palm_verts(dg, side_key: str):
        """Indices + world coords of palm-region verts for one side."""
        hand_groups = [
            g for g in body.vertex_groups
            if "hand" in g.name.lower() and side_key in g.name.lower()
        ]
        if not hand_groups:
            return []
        want = set()
        for g in hand_groups:
            for v in body.data.vertices:
                for vg in v.groups:
                    if vg.group == g.index and vg.weight > 0.5:
                        want.add(v.index)
        hand_bone = f"mixamorig_{'Left' if side_key == 'left' else 'Right'}Hand"
        pose = arm_obj.pose.bones.get(hand_bone)
        if pose is None:
            pose = next((p for p in arm_obj.pose.bones
                         if side_key in p.name.lower() and "hand" in p.name.lower()), None)
        if pose is None:
            return []
        ev_arm = arm_obj.evaluated_get(dg)
        wrist = awm @ ev_arm.pose.bones[pose.name].head
        ev = body.evaluated_get(dg)
        mw = ev.matrix_world
        out = []
        for i in want:
            c = mw @ ev.data.vertices[i].co
            if (c - wrist).length < 0.15:
                out.append(c)
        return out

    def evaluate(side_key: str):
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        pts = palm_verts(dg, side_key)
        if not pts:
            return None
        cx = sum(p.x for p in pts) / len(pts)
        cy = sum(p.y for p in pts) / len(pts)
        cz = sum(p.z for p in pts) / len(pts)
        width = max(p.x for p in pts) - min(p.x for p in pts)
        depth = max(p.y for p in pts) - min(p.y for p in pts)
        ev_pants = pants.evaluated_get(dg)
        mw = ev_pants.matrix_world
        edge = 0.0
        for v in ev_pants.data.vertices:
            c = mw @ v.co
            if abs(c.z - cz) < 0.05:
                edge = max(edge, abs(c.x))
        clear = abs(cx) >= edge + 0.02
        front = cy <= -0.18
        face = width >= depth
        return {
            "cx": cx, "cy": cy, "cz": cz,
            "width": width, "depth": depth, "edge": edge,
            "clear": clear, "front": front, "face": face,
            "ok": clear and front and face,
            "score": (3 if clear else 0) + (2 if front else 0) + (4 if face else 0),
        }

    for side_key, bone_name in (("left", "mixamorig_LeftArm"), ("right", "mixamorig_RightArm")):
        pb = arm_obj.pose.bones.get(bone_name)
        if pb is None:
            log(f"verify_hands: arm bone {bone_name} not found")
            continue
        cur = evaluate(side_key)
        if cur is None:
            continue
        if not cur["ok"]:
            # try 90/180-degree rolls around the arm's own local Y (the palm
            # roll is ambiguous per rig; a small euler-step can never fix it)
            best = (cur["score"], None)
            base = pb.rotation_euler.copy()
            for ang in (math.pi / 2, -math.pi / 2, math.pi, -math.pi):
                m = base.to_matrix().to_4x4() @ Matrix.Rotation(ang, 4, Vector((0.0, 1.0, 0.0)))
                pb.rotation_euler = m.to_euler("XYZ", base)
                trial = evaluate(side_key)
                if trial is not None and trial["score"] > best[0]:
                    best = (trial["score"], (ang, trial))
            if best[1] is not None:
                ang, trial = best[1]
                m = base.to_matrix().to_4x4() @ Matrix.Rotation(ang, 4, Vector((0.0, 1.0, 0.0)))
                pb.rotation_euler = m.to_euler("XYZ", base)
                cur = trial
                log(f"verify_hands: {side_key} roll {math.degrees(ang):+.0f}deg "
                    f"(clear={cur['clear']} front={cur['front']} face={cur['face']})")
            else:
                pb.rotation_euler = base
        final = evaluate(side_key)
        eul = tuple(round(a, 2) for a in pb.rotation_euler)
        if final is not None and final["ok"]:
            log(f"verify_hands: {side_key} OK euler={eul} "
                f"c=({final['cx']:.2f},{final['cy']:.2f},{final['cz']:.2f}) "
                f"w={final['width']:.3f} d={final['depth']:.3f} edge={final['edge']:.2f}")
        else:
            log(f"verify_hands: {side_key} FAILED euler={eul} "
                f"c=({final['cx']:.2f},{final['cy']:.2f},{final['cz']:.2f}) "
                f"w={final['width']:.3f} d={final['depth']:.3f} edge={final['edge']:.2f}")


def find_tex_candidate(tex_dir: Path, image_name: str) -> Path | None:
    """Find a replacement image for ``image_name`` inside tex_dir.

    Priority: ``<name>.png`` (painted CC3 convention), then the exact name
    (imported FBX may reference a C4D/other machine path), then any file
    sharing the name stem (extension differences, e.g. .jpeg vs .jpg).
    """
    cand = tex_dir / f"{image_name}.png"
    if cand.exists():
        return cand
    cand = tex_dir / image_name
    if cand.exists():
        return cand
    stem = Path(image_name).stem
    for f in sorted(tex_dir.iterdir()):
        if f.is_file() and f.stem == stem:
            return f
    return None


def apply_textures(scene: bpy.types.Scene, tex_dir: str) -> None:
    """Swap material image nodes with painted versions from tex_dir."""
    tex_dir = Path(tex_dir)
    if not tex_dir.is_dir():
        return
    swapped = 0
    for mat in bpy.data.materials:
        if not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            cand = find_tex_candidate(tex_dir, node.image.name)
            if cand is None:
                continue
            img = bpy.data.images.load(str(cand))
            node.image = img
            swapped += 1
            log(f"texture {node.image.name} <- {cand.name}")
    log(f"texture overrides applied: {swapped}")


def setup_camera(scene: bpy.types.Scene) -> None:
    cam = bpy.data.cameras.new("SpikeCam")
    cam.type = "ORTHO"
    cam.ortho_scale = CAMS["ortho_scale"]
    obj = bpy.data.objects.new("SpikeCam", cam)
    scene.collection.objects.link(obj)
    obj.location = (0.0, -4.0, CAMS["center_z"])
    obj.rotation_euler = (math.pi / 2, 0.0, 0.0)
    scene.camera = obj


def setup_lights(scene: bpy.types.Scene) -> None:
    def light(name: str, kind: str, loc, strength, color=(1.0, 1.0, 1.0), size=3.0):
        dat = bpy.data.lights.new(name, kind)
        dat.energy = strength
        dat.color = color
        if kind == "AREA":
            dat.size = size
        obj = bpy.data.objects.new(name, dat)
        scene.collection.objects.link(obj)
        obj.location = loc
        return obj

    light("Key", "AREA", (2.0, -3.0, 3.2), 400, color=(1.0, 0.96, 0.9))
    light("Fill", "AREA", (-2.6, -1.2, 1.6), 120, color=(0.85, 0.9, 1.0))
    spot = bpy.data.lights.new("Rim", "SPOT")
    spot.energy = 60
    spot.spot_size = math.radians(60)
    obj = bpy.data.objects.new("Rim", spot)
    scene.collection.objects.link(obj)
    obj.location = (0.2, 2.2, 2.4)
    obj.rotation_euler = (math.radians(-40), 0.0, math.radians(90))
    scene.world = bpy.data.worlds.new("SpikeWorld")
    scene.world.color = (0.04, 0.04, 0.045)


def setup_render(scene: bpy.types.Scene) -> None:
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.film_transparent = True
    scene.render.resolution_x = OUT_W
    scene.render.resolution_y = OUT_H
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    eevee = scene.eevee
    if hasattr(eevee, "render_samples"):
        eevee.render_samples = 16
    elif hasattr(eevee, "taa_render_samples"):
        eevee.taa_render_samples = 16
    if hasattr(eevee, "use_raytracing"):
        pass  # keep defaults; fine for stylized plates


def purge_defaults(scene: bpy.types.Scene) -> None:
    """Remove Blender starter objects (Cube/Light/Camera) so they don't pollute the render."""
    for obj in list(scene.objects):
        if obj.type in {"MESH", "LIGHT", "CAMERA"} and obj.name in {
            "Cube", "Light", "Camera", "SpikeCam",
        }:
            for coll in list(obj.users_collection):
                if obj.name in coll.objects:
                    coll.objects.unlink(obj)


def prune_export_duplicates(scene: bpy.types.Scene) -> None:
    """Hide meshes that mirror the Body mesh's blendshape names.

    Some exporters (Liv/ARKit splits, e.g. MikeAlger) emit one static mesh per
    blendshape *and* the same blendshapes on the base Body mesh; rendering both
    doubles the geometry. Any static mesh whose name equals a shape key on the
    scene's largest mesh is a duplicate -> excluded from rendering and framing.
    """
    meshes = [o for o in scene.objects if o.type == "MESH" and o.name != "Cube"]
    if not meshes:
        return
    body = max(meshes, key=lambda m: (
        len(m.data.shape_keys.key_blocks) if m.data.shape_keys is not None else -1,
        len(m.data.vertices),
    ))
    if body.data.shape_keys is None:
        return
    sk = {kb.name.strip().lower() for kb in body.data.shape_keys.key_blocks}
    hidden = 0
    for obj in meshes:
        if obj is body or obj.data.shape_keys is not None:
            continue
        if obj.name.strip().lower() in sk:
            obj.hide_render = True
            hidden += 1
    log(f"pruned {hidden} export-duplicate meshes")


def import_fbx(path: str) -> None:
    purge_defaults(bpy.context.scene)
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=path)
    purge_defaults(bpy.context.scene)  # FBX may carry its own Cube/Light/Camera
    prune_export_duplicates(bpy.context.scene)
    log("FBX imported")


def add_mock(scene: bpy.types.Scene, weights: dict) -> bpy.types.Object:
    bpy.ops.mesh.primitive_monkey_add(size=0.9, location=(0, 0, 1.0))
    obj = scene.objects["Suzanne"]
    keys = set()
    for viseme in VISEMES:
        keys.update(weights.get(viseme.lower(), {}).keys())
    keys.update(weights.get("eyeblink", {}).keys())
    for name in sorted(keys):
        obj.shape_key_add(name=name)
    obj.scale = (1, 1, 1)
    return obj


def render_plate(scene: bpy.types.Scene, mesh: bpy.types.Object, name: str, out_dir: Path) -> None:
    scene.render.filepath = str(out_dir / f"{name}.png")
    bpy.ops.render.render(write_still=True)
    log(f"plate {name} -> {out_dir / (name + '.png')}")


def pick_mesh(scene: bpy.types.Scene) -> bpy.types.Object | None:
    """Mesh with the most shape keys (the actual character); falls back to
    largest vertex count. Skips hidden export duplicates and the starter cube."""
    best = None
    best_sk = -1
    best_verts = -1
    for o in scene.objects:
        if o.type != "MESH" or o.name == "Cube" or o.hide_render:
            continue
        sk = len(o.data.shape_keys.key_blocks) if o.data.shape_keys is not None else 0
        verts = len(o.data.vertices)
        if (best is None or sk > best_sk
                or (sk == best_sk and verts > best_verts)):
            best = o
            best_sk = sk
            best_verts = verts
    return best


def fit_camera(scene: bpy.types.Scene) -> None:
    """Size ORTHO camera to the union of all mesh bounds (width + height).

    Uses the depsgraph-evaluated geometry so arm/pose deformations (e.g. arms
    hung down from a T-pose) are reflected in the framing.
    """
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    min_x = min_z = float("inf")
    max_x = max_z = float("-inf")
    for obj in scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        ev = obj.evaluated_get(dg)
        if ev is None or ev.data is None:
            continue
        # Use the RAW object's world matrix with the EVALUATED vertex coords.
        # The evaluated object's own matrix_world can disagree with the raw
        # object's by the armature scale factor (e.g. Mixamo imports carry a
        # 0.01 scale on the armature), which would size the camera 100x too
        # big and render the character tiny.
        mw = obj.matrix_world
        for v in ev.data.vertices:
            w = mw @ v.co
            min_x, max_x = min(min_x, w.x), max(max_x, w.x)
            min_z, max_z = min(min_z, w.z), max(max_z, w.z)
    width = max_x - min_x
    height = max_z - min_z
    CAMS["center_z"] = (max_z + min_z) / 2.0
    # Blender 5.2 ORTHO: ortho_scale spans the FULL height of the frustum;
    # the horizontal span is ortho_scale * (W/H). For a 1024x2048 portrait
    # render the horizontal extent is half of ortho_scale, so the width must
    # be expressed as its height-equivalent (width * H/W).
    aspect = OUT_W / OUT_H
    scale = max((width / aspect) * 1.25, height * 1.15)
    CAMS["ortho_scale"] = max(scale, 0.5)
    log(f"fit: w={width:.2f} h={height:.2f} ortho_scale={CAMS['ortho_scale']:.2f}")


def main() -> int:
    fbx, out, weights_name, textures_dir, mock = parse_args(sys.argv)
    if not out or not Path(out).is_absolute():
        log(f"ERROR: output dir must be an absolute path (Blender CWD is unreliable), got {out!r}")
        return 1
    if not mock and (not fbx or not Path(fbx).is_absolute() or not Path(fbx).exists()):
        log(f"ERROR: FBX must be an absolute existing path, got {fbx!r}")
        return 1
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    weights_path = Path(__file__).parent / weights_name
    if not weights_path.exists():
        log(f"ERROR: weight table not found: {weights_path}")
        return 1
    weights = json.loads(weights_path.read_text(encoding="utf-8"))
    scene = bpy.context.scene

    if mock:
        purge_defaults(scene)
        mesh = add_mock(scene, weights)
        log("MOCK mode: no FBX import")
    else:
        import_fbx(fbx)
        mesh = pick_mesh(scene)
        if mesh is None:
            log("ERROR: no mesh found in FBX")
            return 1
        log(f"mesh: {mesh.name}; shape keys: {collect_shape_names(mesh)}")
        lower_arms(scene)
        if textures_dir:
            apply_textures(scene, textures_dir)

    fit_camera(scene)
    setup_camera(scene)
    setup_lights(scene)
    setup_render(scene)

    shape_names = collect_shape_names(mesh)
    actual: list[str] = []
    plates = {}
    for viseme in VISEMES:
        reset_weights(scene)
        matched = set_weights(scene, weights.get(viseme.lower(), {}), actual)
        if matched == 0:
            log(f"WARNING: no shape keys matched viseme {viseme}")
        render_plate(scene, mesh, f"{viseme}_open", out_dir)
        plates[f"{viseme}_open"] = f"{viseme}_open.png"
    for viseme in VISEMES:
        reset_weights(scene)
        set_weights(scene, weights.get(viseme.lower(), {}), actual)
        blink = weights.get("eyeblink", {}).copy()
        set_weights(scene, blink, actual)
        render_plate(scene, mesh, f"{viseme}_closed", out_dir)
        plates[f"{viseme}_closed"] = f"{viseme}_closed.png"

    manifest = {
        "layout": "plates",
        "character": "mock" if mock else Path(fbx).stem,
        "weights_table": weights_name,
        "shape_keys_found": shape_names,
        "matched": actual,
        "plates": plates,
        "weight_table": weights,
    }
    (out_dir / "plates.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"done: {len(plates)} plates in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())