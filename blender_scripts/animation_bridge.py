"""Bridge: retarget a Mixamo animation clip onto a differently-prefixed rig.

Mixamo characters downloaded from the web ship with `mixamorigN:` bone names
(e.g. `mixamorig5:Hips`) while animation clips ship with the canonical
`mixamorig:` prefix. ufbx keeps both names verbatim, so a clip cannot play on
a character until its action's fcurve `data_path`s are rewritten to the
character's bone names.

This script imports a character FBX and a clip FBX into one headless Blender
session, remaps the clip action's pose-bone references via a stem mapping
(`mixamorig5:` -> `mixamorig:` -> character bone), assigns the remapped action
to the character armature, drops the clip's own armature, and saves the scene.

Run headless (all paths absolute - Blender CWD is unreliable):
    blender -b -P animation_bridge.py -- <char.fbx> <clip.fbx> <out.blend> \
        [--name ID] [--fps 24]

- char.fbx : character FBX (absolute path)
- clip.fbx : Mixamo clip FBX (absolute path)
- out.blend: where to save the remapped scene (absolute path)
- --name   : action display name (default = clip file stem)
- --fps    : animation frame rate for the action (default 24)

The remap is additive: any fcurve whose bone has no counterpart on the
character (e.g. unnamed finger helpers) is skipped and reported.
"""

import json
import re
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spike_render  # purge_defaults / import_fbx / prune helpers

BONE_PATH = re.compile(r'pose\.bones\["([^"]+)"\]')


def log(msg: str) -> None:
    print(f"[bridge] {msg}", flush=True)


def parse_args(argv: list[str]) -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {"char": None, "clip": None, "out": None, "name": None, "fps": 24}
    pos = [a for a in argv if not a.startswith("-")]
    if pos:
        opts["char"] = pos[0]
    if len(pos) > 1:
        opts["clip"] = pos[1]
    if len(pos) > 2:
        opts["out"] = pos[2]
    for a in argv:
        if a.startswith("--name="):
            opts["name"] = a.split("=", 1)[1]
        elif a.startswith("--fps="):
            opts["fps"] = int(a.split("=", 1)[1])
    return opts


def stem(name: str) -> str:
    """Bare bone segment: `mixamorig5:Hips` -> `Hips`, `LeftArm` -> `LeftArm`.

    Mixamo characters/clips carry a `mixamorigN:` namespace; Ready Player Me
    GLB avatars use bare names (`Hips`, `LeftArm`). Mapping on the bare segment
    lets one clip action retarget onto either rig.
    """
    return name.rsplit(":", 1)[-1]


def build_map(pose_bones) -> dict[str, str]:
    """Map a clip bone name (its stem) onto a character bone name, if unique."""
    by_stem: dict[str, list[str]] = {}
    for pb in pose_bones:
        by_stem.setdefault(stem(pb.name), []).append(pb.name)
    mapping = {}
    ambiguous = []
    for key, names in by_stem.items():
        if len(names) == 1:
            mapping[key] = names[0]
        elif names:
            ambiguous.append(key)
    return mapping, ambiguous


def iter_fcurves(action):
    """Yield every fcurve in a Blender 5.2 action (layered or legacy).

    Blender 5.x imports FBX animation as a layered action: fcurves live in
    layer.strips[].channelbags[].fcurves rather than the legacy action.fcurves.
    """
    if not action.is_action_layered and hasattr(action, "fcurves"):
        for fc in action.fcurves:
            yield fc
        return
    for layer in action.layers:
        for strip in layer.strips:
            for cb in strip.channelbags:
                for fc in cb.fcurves:
                    yield fc


def remap_fcurves(action, mapping: dict[str, str],
                  loc_scale: float = 1.0) -> tuple[int, int, list[str]]:
    """Rewrite pose-bone references inside ``action`` fcurves using ``mapping``.

    ``loc_scale`` multiplies `location` channel key values so clip units (cm)
    convert to the character's bone units (e.g. 0.01 for meter-scale RPM rigs).

    Returns (rewritten, dropped, dropped_channels) where each channel is a
    distinct fcurve whose bone had no mapping.
    """
    dropped_channels: list[str] = []
    rewritten = dropped = 0
    for fc in iter_fcurves(action):
        m = BONE_PATH.search(fc.data_path)
        if not m:
            continue
        clip_bone = m.group(1)
        char_bone = mapping.get(stem(clip_bone))
        if char_bone is None:
            dropped += 1
            if clip_bone not in dropped_channels:
                dropped_channels.append(clip_bone)
            continue
        if char_bone != clip_bone:
            fc.data_path = fc.data_path.replace(f'"{clip_bone}"', f'"{char_bone}"')
        if loc_scale != 1.0 and "location" in fc.data_path:
            for kp in fc.keyframe_points:
                kp.co.y *= loc_scale
                kp.handle_left.y *= loc_scale
                kp.handle_right.y *= loc_scale
        rewritten += 1
    return rewritten, dropped, dropped_channels


def retarget_clip(clip_path: str, char_arm, name: str, fps: int = 24) -> bpy.types.Action:
    """Import a clip FBX and return a *copy* of its action remapped to char_arm.

    Unlike main() (which mutates and assigns the imported action), this
    duplicates the action so each character in a multi-character scene can
    hold its own retargeted copy. The clip's own armature is removed.
    """
    before = {o for o in bpy.context.scene.objects if o.type == "ARMATURE"}
    spike_render.import_fbx(clip_path)
    clip_arm = next((o for o in bpy.context.scene.objects
                     if o.type == "ARMATURE" and o not in before), None)
    if clip_arm is None:
        raise RuntimeError(f"no clip armature found for {clip_path}")
    act = pick_action(clip_arm, Path(clip_path).stem)
    if act is None:
        raise RuntimeError(f"no action found in {clip_path}")
    new_act = act.copy()
    new_act.name = name
    mapping, ambiguous = build_map(char_arm.data.bones)
    if ambiguous:
        log(f"WARNING: ambiguous stems (skipped): {ambiguous}")
    loc_scale = clip_arm.scale[0] / char_arm.scale[0] if char_arm.scale[0] else 1.0
    if abs(loc_scale - 1.0) > 1e-6:
        log(f"location fcurves scaled by {loc_scale:.4g} "
            f"(clip scale {clip_arm.scale[0]:.4g} / char scale {char_arm.scale[0]:.4g})")
    rewritten, dropped, dropped_bones = remap_fcurves(new_act, mapping, loc_scale)
    log(f"clip {Path(clip_path).name}: {new_act.name} -> {char_arm.name} "
        f"({rewritten} remapped, {dropped} dropped)")
    bpy.data.objects.remove(clip_arm, do_unlink=True)
    return new_act


def pick_action(clip_arm, clip_name: str):
    """Find the animation action on a freshly-imported clip armature."""
    if clip_arm.animation_data and clip_arm.animation_data.action:
        return clip_arm.animation_data.action
    want = clip_name.lower()
    acts = [a for a in bpy.data.actions if want in a.name.lower()]
    if len(acts) == 1:
        return acts[0]
    if len(acts) > 1:
        return max(acts, key=lambda a: len(list(iter_fcurves(a))))
    acts = [a for a in bpy.data.actions if a.users > 0]
    if acts:
        return max(acts, key=lambda a: len(list(iter_fcurves(a))))
    return None


def find_armature(scene) -> bpy.types.Object | None:
    return next((o for o in scene.objects if o.type == "ARMATURE"), None)


def main() -> int:
    opts = parse_args(sys.argv)
    for key in ("char", "clip", "out"):
        val = opts[key]
        if not val or not Path(val).is_absolute():
            log(f"ERROR: {key} must be an absolute path, got {val!r}")
            return 1
    if not Path(opts["char"]).exists():
        log(f"ERROR: character FBX not found: {opts['char']}")
        return 1
    if not Path(opts["clip"]).exists():
        log(f"ERROR: clip FBX not found: {opts['clip']}")
        return 1

    scene = bpy.context.scene
    clip_name = opts["name"] or Path(opts["clip"]).stem
    scene.render.fps = opts["fps"]

    spike_render.import_fbx(opts["char"])
    char_arm = find_armature(scene)
    if char_arm is None:
        log("ERROR: no armature found in character FBX")
        return 1
    log(f"character armature: {char_arm.name} "
        f"({len(char_arm.data.bones)} bones)")

    spike_render.import_fbx(opts["clip"])
    clip_arm = next((o for o in scene.objects
                     if o.type == "ARMATURE" and o is not char_arm), None)
    if clip_arm is None:
        log("ERROR: no armature found in clip FBX")
        return 1
    clip_bone0 = clip_arm.data.bones[0].name if clip_arm.data.bones else "?"
    log(f"clip armature: {clip_arm.name} "
        f"({len(clip_arm.data.bones)} bones, first={clip_bone0})")

    mapping, ambiguous = build_map(char_arm.data.bones)
    if ambiguous:
        log(f"WARNING: ambiguous stems (skipped): {ambiguous}")

    # the clip action: first action on the clip armature, or one named for the clip
    act = pick_action(clip_arm, clip_name)
    if act is None:
        log("ERROR: no action found in clip FBX")
        return 1
    log(f"clip action: {act.name} ({len(list(iter_fcurves(act)))} fcurves, "
        f"{act.frame_range[1] - act.frame_range[0] + 1} frames)")

    loc_scale = clip_arm.scale[0] / char_arm.scale[0] if char_arm.scale[0] else 1.0
    if abs(loc_scale - 1.0) > 1e-6:
        log(f"location fcurves scaled by {loc_scale:.4g}")
    rewritten, dropped, dropped_bones = remap_fcurves(act, mapping, loc_scale)

    if char_arm.animation_data is None:
        char_arm.animation_data_create()
    char_arm.animation_data.action = act
    if act.slots:
        # bind the action's slot to the character object so fcurves evaluate
        char_arm.animation_data.action_slot = act.slots[0]
    char_arm.animation_data.action.name = clip_name

    # retarget the clip armature's action too (harmless) then drop the clip rig
    clip_arm.animation_data.action = None
    bpy.data.objects.remove(clip_arm, do_unlink=True)

    scene.frame_start = max(1, int(round(act.frame_range[0])))
    scene.frame_end = max(1, int(round(act.frame_range[1])))
    log(f"assigned action to {char_arm.name}: {clip_name} "
        f"({scene.frame_start}..{scene.frame_end})")

    out_blend = Path(opts["out"])
    out_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out_blend))

    report = {
        "clip": opts["clip"], "character": opts["char"],
        "action": clip_name, "stem_mapping": mapping,
        "fcurves_rewritten": rewritten, "fcurves_dropped": dropped,
        "dropped_bones": dropped_bones, "ambiguous_stems": ambiguous,
        "frames": (scene.frame_start, scene.frame_end),
    }
    (out_blend.with_suffix(".json")).write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    log(f"done: {rewritten} fcurves remapped, {dropped} dropped "
        f"({len(dropped_bones)} bones) -> {out_blend}")
    return 0


if __name__ == "__main__":
    sys.exit(main())