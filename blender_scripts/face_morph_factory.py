"""Phase 1 Step 1.0: add ARKit-named face shape keys to a morph-less Mixamo FBX.

Imports a rigged Mixamo character (no facial blendshapes), locates the head
region of the body mesh, auto-detects face landmarks (eye centers, mouth line,
chin, brow) from geometry + eye/lash meshes, then generates relative shape
keys named exactly like viseme_weights_mike.json expects (Blink_Left/Right,
Jaw_Down, MouthOpen, MouthUp/Down, Midmouth_Left/Right, MouthNarrow_Left/Right,
Smile_Left/Right, CheekPuff_Left/Right, MouthWhistle_NarrowAdjust_Left/Right)
so the existing Rhubarb lip-sync weight table drives any character unchanged.

Run headless (all paths absolute - Blender CWD is unreliable):
    blender -b -P face_morph_factory.py -- <fbx> <outdir> [--plates] [--landmarks FILE]

- fbx        : character FBX (absolute path)
- outdir     : report JSON + optional plate PNGs (absolute path)
- --plates   : also bake the 18 viseme/eye plates (validates the morphs visually)
- --landmarks: optional JSON override {eye_l:[x,y,z], eye_r:[...], chin:[...],
               mouth:[...], nose:[...], head_top:[...], front:[x,y,z]}
"""

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spike_render  # import_fbx / lower_arms / plate helpers

OUT_W, OUT_H = 1024, 2048
VISEMES = "ABCDEFGHX"

# shape keys the factory must produce (names = viseme_weights_mike.json keys)
MORPH_NAMES = [
    "Blink_Left", "Blink_Right",
    "Jaw_Down",
    "MouthOpen",
    "MouthUp", "MouthDown",
    "Midmouth_Left", "Midmouth_Right",
    "MouthNarrow_Left", "MouthNarrow_Right",
    "Smile_Left", "Smile_Right",
    "CheekPuff_Left", "CheekPuff_Right",
    "MouthWhistle_NarrowAdjust_Left", "MouthWhistle_NarrowAdjust_Right",
]


def log(msg: str) -> None:
    print(f"[factory] {msg}", flush=True)


def parse_args(argv: list[str]) -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {"fbx": None, "out": None, "plates": False, "landmarks": None}
    pos = [a for a in argv if not a.startswith("-")]
    if pos:
        opts["fbx"] = pos[0]
    if len(pos) > 1:
        opts["out"] = pos[1]
    for a in argv:
        if a == "--plates":
            opts["plates"] = True
        elif a.startswith("--landmarks="):
            opts["landmarks"] = a.split("=", 1)[1]
    return opts


def bone_stem(name: str) -> str:
    """'mixamorig5:Head' -> 'Head' (also handles 'mixamorig:Head')."""
    return name.split(":")[-1].strip().lower()


def head_weighted_verts(mesh: bpy.types.Object) -> list[int]:
    """Indices of mesh verts with meaningful weight in a Head bone group."""
    groups = [g for g in mesh.vertex_groups if bone_stem(g.name) in
              {"head", "headtop_end", "jaw", "brow", "mouth"}]
    want = set()
    for g in groups:
        gi = g.index
        for v in mesh.data.vertices:
            for vg in v.groups:
                if vg.group == gi and vg.weight > 0.25:
                    want.add(v.index)
    return sorted(want)


def world_coords(mesh: bpy.types.Object, indices: list[int]) -> list[Vector]:
    mw = mesh.matrix_world
    return [mw @ mesh.data.vertices[i].co for i in indices]


def face_mesh_candidates(scene: bpy.types.Scene, arm=None) -> list[bpy.types.Object]:
    out = []
    for o in scene.objects:
        if o.type != "MESH" or o.name == "Cube" or o.hide_render:
            continue
        if arm is not None and o.parent is not arm:
            continue
        if head_weighted_verts(o):
            out.append(o)
    return out


def pick_face_mesh(candidates: list[bpy.types.Object]) -> bpy.types.Object:
    """The full body mesh: most vertex groups (body rigs) and head verts.

    Hair/clothes also carry a Head group; the body mesh is the one bound to
    the whole skeleton (most bone groups), which is where the face lives.
    """
    return max(candidates, key=lambda m: (len(m.vertex_groups), len(head_weighted_verts(m))))


def pick_eye_meshes(scene: bpy.types.Scene, arm=None) -> list[bpy.types.Object]:
    """Meshes that likely sit at the eyes (eyelash/eye geometry).

    When ``arm`` is given, only meshes parented under that armature are
    considered (multi-character scenes).
    """
    out = []
    for o in scene.objects:
        if o.type != "MESH" or o.name == "Cube":
            continue
        if arm is not None and o.parent is not arm:
            continue
        n = o.name.lower()
        if any(k in n for k in ("eye", "lash", "brow")):
            out.append(o)
    return out


def detect_landmarks(scene: bpy.types.Scene, mesh: bpy.types.Object,
                     eyes: list[bpy.types.Object], arm=None) -> dict:
    hv = head_weighted_verts(mesh)
    hw = world_coords(mesh, hv)
    # head bone -> vertical lower bound of the head region
    if arm is None:
        arm = next((o for o in scene.objects if o.type == "ARMATURE"), None)
    head_bone_v = None
    if arm is not None:
        for b in arm.data.bones:
            if bone_stem(b.name) == "head":
                head_bone_v = (arm.matrix_world @ b.head_local).z
                break
    if head_bone_v is not None:
        hw = [p for p in hw if p.z > head_bone_v - 0.02]
    if len(hw) < 50:
        raise RuntimeError("head region too small")

    # face front direction: prefer eye meshes; else nose-protrusion heuristic
    front = None
    eye_l = eye_r = None
    if eyes:
        allv = []
        for e in eyes:
            allv += [e.matrix_world @ v.co for v in e.data.vertices]
        if len(allv) >= 4:
            c = sum(allv, Vector((0, 0, 0))) / len(allv)
            f = (c - sum(hw, Vector((0, 0, 0))) / len(hw))
            f.z = 0.0
            if f.length > 1e-4:
                front = f.normalized()
            # split eye verts left/right by side of the head midline
            mw = mesh.matrix_world
            hx = sum(p.x for p in hw) / len(hw)
            ls, rs = [], []
            for e in eyes:
                for v in e.data.vertices:
                    p = e.matrix_world @ v.co
                    (ls if p.x < hx else rs).append(p)
            if ls:
                eye_l = sum(ls, Vector((0, 0, 0))) / len(ls)
            if rs:
                eye_r = sum(rs, Vector((0, 0, 0))) / len(rs)
    if front is None:
        # protrusion heuristic: front = dir to the most outlying horizontal vert
        c = sum(hw, Vector((0, 0, 0))) / len(hw)
        best, bp = 0.0, None
        for p in hw:
            d = p - c
            d.z = 0.0
            if d.length > best:
                best, bp = d.length, p
        if bp is None:
            raise RuntimeError("cannot determine face front")
        front = (bp - c)
        front.z = 0.0
        front = front.normalized()
    if eye_l is None or eye_r is None:
        # fallback: symmetric eye slots at ~45% head height
        head_center = sum(hw, Vector((0, 0, 0))) / len(hw)
        vmax = max(p.z for p in hw)
        vmin = min(p.z for p in hw)
        eye_v = vmax - 0.45 * (vmax - vmin)
        band = [p for p in hw if p.z > eye_v - 0.08 and p.z < eye_v + 0.08]
        hx = sum(p.x for p in band) / len(band)
        w = max(abs(p.x - hx) for p in band)
        eye_l = Vector((hx - 0.38 * w, eye_v, 0))
        eye_r = Vector((hx + 0.38 * w, eye_v, 0))
        eye_l.z = eye_r.z = eye_v

    head_center = sum(hw, Vector((0, 0, 0))) / len(hw)
    vmax = max(p.z for p in hw)
    vmin = min(p.z for p in hw)
    eye_v = (eye_l.z + eye_r.z) / 2.0
    chin_v = vmin
    # mouth sits ~62% of the way from eyes down to the chin
    mouth_v = eye_v - 0.62 * (eye_v - chin_v)
    nose_v = eye_v - 0.35 * (eye_v - chin_v)
    # head width (perpendicular to front, horizontal)
    right = Vector.cross(front, Vector((0, 0, 1))).normalized()
    hw2 = [p for p in world_coords(mesh, hv) if p.z > head_bone_v - 0.02] if head_bone_v else hw
    widths = [abs((p - head_center).dot(right)) for p in hw2]
    half_w = max(widths) if widths else 0.15
    mouth_center = head_center + Vector((0, 0, 0))
    mouth_center.z = mouth_v
    mouth_half = 0.33 * half_w

    return {
        "front": [round(x, 4) for x in front],
        "right": [round(x, 4) for x in right],
        "head_center": [round(x, 4) for x in head_center],
        "eye_l": [round(x, 4) for x in eye_l],
        "eye_r": [round(x, 4) for x in eye_r],
        "eye_v": round(eye_v, 4),
        "nose_v": round(nose_v, 4),
        "mouth_v": round(mouth_v, 4),
        "mouth_half": round(mouth_half, 4),
        "chin_v": round(chin_v, 4),
        "head_top": round(vmax, 4),
        "head_half_w": round(half_w, 4),
    }


def morph_deltas(mesh: bpy.types.Object, L: dict) -> dict[str, list[Vector]]:
    """Compute per-vertex world deltas for every morph key."""
    mw = mesh.matrix_world
    verts = mesh.data.vertices
    front = Vector(L["front"])
    right = Vector(L["right"])
    up = Vector((0, 0, 1))
    eye_l = Vector(L["eye_l"])
    eye_r = Vector(L["eye_r"])
    mouth_v = L["mouth_v"]
    mouth_half = L["mouth_half"]
    head_top = L["head_top"]
    head_half_w = L["head_half_w"]
    eye_v = L["eye_v"]
    chin_v = L["chin_v"]
    head_center = Vector(L["head_center"])

    basis = [mw @ v.co for v in verts]
    deltas = {name: [Vector((0, 0, 0)) for _ in verts] for name in MORPH_NAMES}

    def add(name, idx, delta, weight):
        deltas[name][idx] += delta * weight

    # head-local frame: origin = head_center; u=right, v=up, n=front
    vo = head_center.z
    mouth_v_l = mouth_v - vo
    eye_v_l = eye_v - vo
    chin_v_l = chin_v - vo
    eye_l_l = Vector((
        (eye_l - head_center).dot(right),
        (eye_l - head_center).dot(up),
        (eye_l - head_center).dot(front)))
    eye_r_l = Vector((
        (eye_r - head_center).dot(right),
        (eye_r - head_center).dot(up),
        (eye_r - head_center).dot(front)))

    # only verts in the head/face vertical band may be deformed
    z_lo = chin_v - 0.06
    z_hi = head_top + 0.05
    face_region = [i for i, p in enumerate(basis) if z_lo <= p.z <= z_hi]

    def local(p):
        u = (p - head_center).dot(right)
        v = (p - head_center).dot(up)
        n = (p - head_center).dot(front)
        return u, v, n

    def gauss(dist, sigma):
        return math.exp(-(dist * dist) / (2.0 * sigma * sigma))

    vert_face = {}
    for i in face_region:
        u, v, n = local(basis[i])
        vert_face[i] = (u, v, n)

    mouth_center_world = head_center + up * (mouth_v - head_center.z)

    for i in face_region:
        p = basis[i]
        u, v, n = vert_face[i]
        # ------- Jaw_Down: rotate lower face (v < mouth_v) about the right axis
        if v < mouth_v_l + 0.02:
            pivot = mouth_center_world
            mask = 1.0 if v < mouth_v_l - 0.015 else gauss(v - mouth_v_l, 0.015)
            rot = Matrix.Rotation(math.radians(-5.5) * mask, 4, right)
            d = (rot @ (p - pivot)) + pivot - p
            add("Jaw_Down", i, d, 1.0)
        # ------- mouth region helpers
        du = abs(u) / max(mouth_half, 1e-4)
        dv = abs(v - mouth_v_l)
        lip = gauss(dv, 0.035) * (1.0 if du <= 1.6 else 0.0)
        upper = 1.0 if v > mouth_v_l else 0.0
        lower = 1.0 if v < mouth_v_l else 0.0
        # ------- MouthOpen: lower lip down + upper lip up
        if lip > 0.01:
            add("MouthOpen", i, up * (0.012 if lower else -0.006) + front * 0.004, lip)
        # ------- MouthUp / MouthDown
        if lip > 0.01:
            add("MouthUp", i, up * 0.012, lip * upper)
            add("MouthDown", i, up * -0.014, lip * lower)
        # ------- Midmouth: pull the corner region sideways (wide open)
        corner = gauss(abs(u) - mouth_half, 0.03) * lip
        if u > 0:
            add("Midmouth_Right", i, right * 0.018, corner)
        else:
            add("Midmouth_Left", i, right * -0.018, corner)
        # ------- Smile: corner pulled up and out
        if abs(u) > mouth_half * 0.6 and lip > 0.05:
            smile = lip * gauss(abs(u) - mouth_half * 0.7, 0.05)
            if u > 0:
                add("Smile_Right", i, up * 0.012 + right * 0.006, smile)
            else:
                add("Smile_Left", i, up * 0.012 + right * -0.006, smile)
        # ------- MouthNarrow: lips pull toward the center + protrude (pucker)
        if lip > 0.01:
            inward = right * (u * -0.6 / max(mouth_half, 1e-4)) * 0.012
            add("MouthNarrow_Left" if u <= 0 else "MouthNarrow_Right", i,
                inward + front * 0.008, lip)
        # ------- MouthWhistle_NarrowAdjust: strong pucker (like /u/)
        if lip > 0.05:
            puck = right * (u * -0.9 / max(mouth_half, 1e-4)) * 0.015 + front * 0.014
            add("MouthWhistle_NarrowAdjust_Left" if u <= 0 else "MouthWhistle_NarrowAdjust_Right", i, puck, lip)
        # ------- CheekPuff: cheek zone (side of face, below eye, beside mouth)
        cheek_u = abs(u)
        if cheek_u > mouth_half * 0.8 and mouth_v_l - 0.12 < v < eye_v_l + 0.02:
            cm = gauss(cheek_u - head_half_w * 0.62, 0.07) * gauss(v - (mouth_v_l + eye_v_l) / 2, 0.09)
            if cm > 0.01:
                add("CheekPuff_Left" if u < 0 else "CheekPuff_Right", i, front * 0.02, cm)
        # ------- Blink: upper eyelid folds down over the eye
        for side, eye in (("Left", eye_l_l), ("Right", eye_r_l)):
            eu, ev, en = eye.x, eye.y, eye.z
            dr = math.hypot(u - eu, v - ev)
            if dr < 0.10 and v > ev - 0.005:
                bm = gauss(dr, 0.055) * (0.35 + 0.65 * (1 if v > ev else 0.25))
                # lid drops toward the eye and slightly inward
                add(f"Blink_{side}", i, up * -0.030 * bm + front * -0.008 * bm, 1.0)
    return deltas


def build_morph_keys(scene: bpy.types.Scene, mesh: bpy.types.Object,
                     arm=None, landmarks_override: dict | None = None,
                     out_dir: Path | None = None) -> dict:
    """Create the 16 ARKit-named shape keys on ``mesh`` and return a report.

    Shared by the standalone CLI (main) and the multi-character scene
    automator, so characters can get their morphs in-place before animation.
    ``landmarks_override`` maps landmark keys to new values; when ``out_dir``
    is given a morph_report.json is also written.
    """
    eyes = pick_eye_meshes(scene, arm=arm)
    landmarks = detect_landmarks(scene, mesh, eyes, arm=arm)
    if landmarks_override:
        for k in ("eye_l", "eye_r", "chin_v", "mouth_v", "nose_v", "front"):
            if k in landmarks_override:
                v = landmarks_override[k]
                landmarks[k] = [float(x) for x in v] if isinstance(v, (list, tuple)) else float(v)
        log(f"landmark override applied ({len(landmarks_override)} keys)")

    mesh.shape_key_add(name="Basis", from_mix=False)
    inv_mw = mesh.matrix_world.inverted()
    inv3 = inv_mw.to_3x3()
    basis_co = [v.co.copy() for v in mesh.data.vertices]
    deltas = morph_deltas(mesh, landmarks)
    report_morphs = {}
    for name in MORPH_NAMES:
        kb = mesh.shape_key_add(name=name, from_mix=False)
        kb.relative_key = mesh.data.shape_keys.key_blocks["Basis"]
        maxd = 0.0
        n = 0
        for i, d in enumerate(deltas[name]):
            if d.length > 1e-6:
                n += 1
                kb.data[i].co = basis_co[i] + inv3 @ d
                maxd = max(maxd, d.length)
        report_morphs[name] = {"verts_affected": n, "max_disp_m": round(maxd, 4)}
        log(f"key {name}: {n} verts, max {maxd*100:.2f} cm")

    if out_dir is not None:
        report = {
            "character": mesh.name,
            "mesh": mesh.name,
            "landmarks": landmarks,
            "morphs": report_morphs,
            "weights_table": "viseme_weights_mike.json",
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "morph_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
    return {"landmarks": landmarks, "morphs": report_morphs}


def main() -> int:
    opts = parse_args(sys.argv)
    if not opts["fbx"] or not Path(opts["fbx"]).is_absolute() or not Path(opts["fbx"]).exists():
        log(f"ERROR: FBX must be an absolute existing path, got {opts['fbx']!r}")
        return 1
    if not opts["out"] or not Path(opts["out"]).is_absolute():
        log(f"ERROR: outdir must be absolute, got {opts['out']!r}")
        return 1
    out_dir = Path(opts["out"])
    out_dir.mkdir(parents=True, exist_ok=True)

    scene = bpy.context.scene
    spike_render.import_fbx(opts["fbx"])
    candidates = face_mesh_candidates(scene)
    if not candidates:
        log("ERROR: no mesh with head vertex group found")
        return 1
    mesh = pick_face_mesh(candidates)
    log(f"face mesh: {mesh.name} ({len(head_weighted_verts(mesh))} head verts)")
    eyes = pick_eye_meshes(scene)

    landmarks = detect_landmarks(scene, mesh, eyes)
    if opts["landmarks"]:
        over = json.loads(Path(opts["landmarks"]).read_text(encoding="utf-8"))
        for k in ("eye_l", "eye_r", "chin_v", "mouth_v", "nose_v", "front"):
            if k in over:
                if isinstance(over[k], (list, tuple)):
                    landmarks[k] = [float(x) for x in over[k]]
                else:
                    landmarks[k] = float(over[k])
        log(f"landmark override applied from {opts['landmarks']}")
    log("landmarks: " + json.dumps(landmarks, indent=1).replace("\n", " "))

    result = build_morph_keys(scene, mesh, landmarks_override=landmarks,
                              out_dir=out_dir)
    report_morphs = result["morphs"]

    report = {
        "character": Path(opts["fbx"]).stem,
        "mesh": mesh.name,
        "landmarks": result["landmarks"],
        "morphs": report_morphs,
        "weights_table": "viseme_weights_mike.json",
    }
    (out_dir / "morph_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    if opts["plates"]:
        spike_render.lower_arms(scene)
        spike_render.fit_camera(scene)
        spike_render.setup_camera(scene)
        spike_render.setup_lights(scene)
        spike_render.setup_render(scene)
        weights_path = Path(__file__).parent / "viseme_weights_mike.json"
        weights = json.loads(weights_path.read_text(encoding="utf-8"))
        plate_dir = out_dir / "plates"
        plate_dir.mkdir(parents=True, exist_ok=True)
        found = []
        plates = {}
        for viseme in VISEMES:
            spike_render.reset_weights(scene)
            spike_render.set_weights(scene, weights.get(viseme.lower(), {}), found)
            spike_render.render_plate(scene, mesh, f"{viseme}_open", plate_dir)
            plates[f"{viseme}_open"] = f"{viseme}_open.png"
        for viseme in VISEMES:
            spike_render.reset_weights(scene)
            spike_render.set_weights(scene, weights.get(viseme.lower(), {}), found)
            spike_render.set_weights(scene, weights.get("eyeblink", {}), found)
            spike_render.render_plate(scene, mesh, f"{viseme}_closed", plate_dir)
            plates[f"{viseme}_closed"] = f"{viseme}_closed.png"
        (plate_dir / "plates.json").write_text(json.dumps({
            "character": report["character"], "weights_table": report["weights_table"],
            "matched": found, "plates": plates,
        }, indent=2), encoding="utf-8")
        log(f"plates baked: {len(plates)} -> {plate_dir}")

    log(f"done: {len(MORPH_NAMES)} morph keys on {mesh.name}; report in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())