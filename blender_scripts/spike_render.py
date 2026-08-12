"""Spike: bake 3D character viseme/eye plates for the sprite pipeline.

Renders 18 transparent plates per character (9 Rhubarb visemes x eyes
open/closed) by driving the model's face blendshapes, for later use in the
M3/M4 sprite compositor (plate-swap layout).

Run headless:
    blender -b -P spike_render.py -- input.fbx outdir [--legacy] [--mock]

- input.fbx : Mixamo character FBX (with blendshapes)
- outdir    : where plate PNGs + plates.json land
- --legacy  : use Blender's legacy FBX importer instead of the new ufbx one
- --mock    : skip FBX entirely; render a monkey head to validate the render
              pipeline (camera/light/shape-key loop) without a model
"""

import json
import math
import sys
from pathlib import Path

import bpy

VISEMES = "ABCDEFGHX"
OUT_W, OUT_H = 1024, 2048
ORTHO_SCALE = 2.0
CAM_Z = 1.05
LOWER_ARMS = True


def log(msg: str) -> None:
    print(f"[spike] {msg}", flush=True)


def parse_args(argv: list[str]) -> tuple:
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    fbx = out = None
    legacy = mock = False
    positionals = [a for a in args if not a.startswith("-")]
    for a in args:
        if a == "--legacy":
            legacy = True
        elif a == "--mock":
            mock = True
    if mock:
        out = positionals[0] if positionals else None
    else:
        fbx = positionals[0] if positionals else None
        out = positionals[1] if len(positionals) > 1 else None
    return fbx, out, legacy, mock


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


def set_weights(mesh: bpy.types.Object, weights: dict, found: list[str]) -> None:
    found.clear()
    for name, value in weights.items():
        kb = find_key(mesh, name)
        if kb is not None:
            kb.value = value
            found.append(name)


def reset_weights(mesh: bpy.types.Object) -> None:
    if mesh.data.shape_keys is None:
        return
    for kb in mesh.data.shape_keys.key_blocks:
        kb.value = 0.0


def lower_arms(scene: bpy.types.Scene) -> None:
    """Drop T-pose arms to a relaxed hanging position (best-effort)."""
    if not LOWER_ARMS:
        return
    for obj in scene.objects:
        if obj.type != "ARMATURE":
            continue
        for pb in obj.pose.bones:
            if pb.name == "mixamorig:LeftUpArm":
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = (0.0, math.radians(90), 0.0)
                log(f"pose {pb.name} down")
            elif pb.name == "mixamorig:RightUpArm":
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = (0.0, math.radians(90), 0.0)
                log(f"pose {pb.name} down")
            elif pb.name == "mixamorig:LeftForeArm":
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = (-math.radians(8), 0.0, 0.0)
            elif pb.name == "mixamorig:RightForeArm":
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = (-math.radians(8), 0.0, 0.0)


def setup_camera(scene: bpy.types.Scene) -> None:
    cam = bpy.data.cameras.new("SpikeCam")
    cam.type = "ORTHO"
    cam.ortho_scale = ORTHO_SCALE
    obj = bpy.data.objects.new("SpikeCam", cam)
    scene.collection.objects.link(obj)
    obj.location = (0.0, -4.0, CAM_Z)
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


def import_fbx(path: str, legacy: bool) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    if legacy:
        bpy.ops.import_scene.fbx_legacy(filepath=path)
    else:
        bpy.ops.import_scene.fbx(filepath=path)
    log("FBX imported")


def add_mock(scene: bpy.types.Scene, weights: dict) -> bpy.types.Object:
    bpy.ops.mesh.primitive_monkey_add(size=0.9, location=(0, 0, 1.0))
    obj = scene.objects["Suzanne"]
    for name in ["jawOpen", "mouthStretch", "mouthFunnel", "eyeBlinkLeft", "eyeBlinkRight"]:
        obj.shape_key_add(name=name)
    obj.scale = (1, 1, 1)
    return obj


def render_plate(scene: bpy.types.Scene, mesh: bpy.types.Object, name: str, out_dir: Path) -> None:
    scene.render.filepath = str(out_dir / f"{name}.png")
    bpy.ops.render.render(write_still=True)
    log(f"plate {name} -> {out_dir / (name + '.png')}")


def main() -> int:
    fbx, out, legacy, mock = parse_args(sys.argv)
    if not out or not Path(out).is_absolute():
        log(f"ERROR: output dir must be an absolute path (Blender CWD is unreliable), got {out!r}")
        return 1
    if not mock and (not fbx or not Path(fbx).is_absolute() or not Path(fbx).exists()):
        log(f"ERROR: FBX must be an absolute existing path, got {fbx!r}")
        return 1
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    weights = json.loads((Path(__file__).parent / "viseme_weights.json").read_text(encoding="utf-8"))
    scene = bpy.context.scene

    if mock:
        mesh = add_mock(scene, weights)
        log("MOCK mode: no FBX import")
    else:
        import_fbx(fbx, legacy)
        meshes = [o for o in scene.objects if o.type == "MESH"]
        if not meshes:
            log("ERROR: no mesh found in FBX")
            return 1
        mesh = meshes[0]
        log(f"mesh: {mesh.name}; shape keys: {collect_shape_names(mesh)}")
        lower_arms(scene)

    setup_camera(scene)
    setup_lights(scene)
    setup_render(scene)

    shape_names = collect_shape_names(mesh)
    actual: list[str] = []
    plates = {}
    for viseme in VISEMES:
        reset_weights(mesh)
        set_weights(mesh, weights.get(viseme.lower(), {}), actual)
        render_plate(scene, mesh, f"{viseme}_open", out_dir)
        plates[f"{viseme}_open"] = f"{viseme}_open.png"
    # eyes closed variants on top of each closed-mouth... all visemes with blink
    for viseme in VISEMES:
        reset_weights(mesh)
        set_weights(mesh, weights.get(viseme.lower(), {}), actual)
        blink = weights.get("x", {}).copy()
        blink.update({"eyeBlinkLeft": 1.0, "eyeBlinkRight": 1.0})
        set_weights(mesh, blink, actual)
        render_plate(scene, mesh, f"{viseme}_closed", out_dir)
        plates[f"{viseme}_closed"] = f"{viseme}_closed.png"

    manifest = {
        "layout": "plates",
        "character": "mock" if mock else Path(fbx).stem,
        "shape_keys_found": shape_names,
        "plates": plates,
        "weight_table": weights,
    }
    (out_dir / "plates.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"done: {len(plates)} plates in {out_dir}")
    return 0


sys.exit(main())