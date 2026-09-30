"""Paint cartoon garments onto a CC3+ base character via UV flood-fill.

Headless:
    blender -b -P paint_clothes.py -- input.fbx [--colors R,G,B]

For every face of the body mesh, classify it by world-space position into a
garment (shoes / pants / shirt sleeves / shirt torso / skin) and fill its UV
triangle in the diffuse texture(s) with a flat toon color. Faces in the
"skin" class are left untouched so skin textures stay intact. Saves images
in place (run against a COPY of the model folder).

Region model (character ~1.8 m, feet at z_min):
    h = (z - z_min) / (z_max - z_min)
    |x| > 0.30                     -> arm region
        h < 0.42                   -> skin (forearm, hand)
        otherwise                  -> shirt (sleeve)
    |x| <= 0.30
        h < 0.05                   -> shoes
        h < 0.26                   -> pants
        h < 0.75                   -> shirt (torso)
        otherwise                  -> skin (head, neck)
"""

import sys
from pathlib import Path

import bpy
import bmesh
import numpy as np


def find_body_mesh() -> bpy.types.Object:
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH" and obj.name in {"CC_Base_Body", "CC3_Base_Body", "Body"}:
            return obj
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    return max(meshes, key=lambda m: len(m.data.vertices)) if meshes else None


def material_image(material) -> bpy.types.Image | None:
    if material is None or material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node.image
    return None


def classify(x: float, z: float, z_min: float, height: float) -> str | None:
    """Return garment name or None for skin."""
    h = (z - z_min) / height
    if x > 0.30:
        return None if h < 0.42 else "shirt"
    if h < 0.05:
        return "shoe"
    if h < 0.26:
        return "pants"
    if h < 0.75:
        return "shirt"
    return None


def in_triangle(px, py, a, b, c) -> bool:
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1 = sign((px, py), a, b)
    d2 = sign((px, py), b, c)
    d3 = sign((px, py), c, a)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def fill_triangle(arr: np.ndarray, w: int, h: int, uv_pts, color: tuple) -> None:
    """Rasterize UV triangle(s) into a flat RGBA numpy array (index = (y*w+x)*4)."""
    pts = np.array([(u % 1.0 * w, v % 1.0 * h) for u, v in uv_pts], dtype=np.float64)
    n = len(pts)
    if n < 3:
        return
    tris = ((0, 1, 2),)
    if n == 4:
        tris = ((0, 1, 2), (0, 2, 3))
    for i0, i1, i2 in tris:
        a, b, c = pts[i0], pts[i1], pts[i2]
        min_x, max_x = int(np.floor(pts[:, 0].min())), int(np.ceil(pts[:, 0].max()))
        min_y, max_y = int(np.floor(pts[:, 1].min())), int(np.ceil(pts[:, 1].max()))
        if max_x < 0 or min_x >= w or max_y < 0 or min_y >= h:
            continue
        min_x = max(min_x, 0); max_x = min(max_x, w - 1)
        min_y = max(min_y, 0); max_y = min(max_y, h - 1)
        xs = np.arange(min_x, max_x + 1) + 0.5
        ys = np.arange(min_y, max_y + 1) + 0.5
        gx, gy = np.meshgrid(xs, ys)
        pxy = np.stack([gx.ravel(), gy.ravel()], axis=1)
        d1 = np.cross(pxy - a, b - a)
        d2 = np.cross(pxy - b, c - b)
        d3 = np.cross(pxy - c, a - c)
        ccw = (d1 >= 0) & (d2 >= 0) & (d3 >= 0)
        cw = (d1 <= 0) & (d2 <= 0) & (d3 <= 0)
        inside = ccw | cw
        if not inside.any():
            continue
        rows = min_y + np.nonzero(inside)[0] // (max_x - min_x + 1)
        cols = min_x + np.nonzero(inside)[0] % (max_x - min_x + 1)
        flat = (rows * w + cols) * 4
        rgb = np.array(color, dtype=np.float64) / 255.0
        arr[flat + 0] = rgb[0]
        arr[flat + 1] = rgb[1]
        arr[flat + 2] = rgb[2]
        arr[flat + 3] = 1.0


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    fbx = argv[0]
    colors_arg = None
    if "--colors" in argv:
        colors_arg = argv[argv.index("--colors") + 1]
    if not fbx or not Path(fbx).exists():
        print("ERROR: need an existing FBX path", flush=True)
        return 1

    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=fbx)
    obj = find_body_mesh()
    if obj is None:
        print("ERROR: no body mesh found", flush=True)
        return 1
    print(f"body mesh: {obj.name} ({len(obj.data.vertices)} verts)", flush=True)

    mat_images = [material_image(m) for m in obj.data.materials]
    print(f"materials: {[None if i is None else i.name for i in mat_images]}", flush=True)

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    uv_layer = bm.loops.layers.uv.active

    xs = [obj.matrix_world @ v.co for v in obj.data.vertices]
    z_min = min(v[2] for v in xs)
    z_max = max(v[2] for v in xs)
    height = z_max - z_min
    print(f"bounds z: {z_min:.2f}..{z_max:.2f}", flush=True)

    colors = {
        "shoe": (55, 55, 65),
        "pants": (72, 92, 148),
        "shirt": (96, 152, 226),
    }
    if colors_arg:
        rgb = [int(v) for v in colors_arg.split(",")]
        colors = {k: tuple(rgb) for k in colors}

    painted = {name: 0 for name in colors}
    buffers = {img: np.array(img.pixels[:], dtype=np.float64) for img in set(m for m in mat_images if m is not None)}
    for face in bm.faces:
        coords = [obj.matrix_world @ v.co for v in face.verts]
        cx = sum(v[0] for v in coords) / len(coords)
        cz = sum(v[2] for v in coords) / len(coords)
        garment = classify(cx, cz, z_min, height)
        if garment is None:
            continue
        img = mat_images[face.material_index] if face.material_index < len(mat_images) else None
        if img is None:
            print(f"WARNING: face material {face.material_index} has no image; skipping", flush=True)
            continue
        fill_triangle(buffers[img], img.size[0], img.size[1],
                      [l[uv_layer].uv[:] for l in face.loops], colors[garment])
        painted[garment] += 1

    bpy.context.view_layer.update()
    painted_dir = Path(fbx).parent / "painted"
    painted_dir.mkdir(exist_ok=True)
    saved = []
    for img in set(m for m in mat_images if m is not None):
        img.pixels[:] = buffers[img].tolist()
        img.update()
        out = painted_dir / f"{img.name}.png"
        img.filepath_raw = str(out)
        img.file_format = "PNG"
        img.save()
        saved.append(str(out))
    print(f"painted faces: {painted}", flush=True)
    print(f"saved images ({len(saved)}):", flush=True)
    for s in saved:
        print(f"  {s}", flush=True)
    bm.free()
    return 0


sys.exit(main())