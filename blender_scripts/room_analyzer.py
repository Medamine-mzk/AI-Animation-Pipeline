"""room_analyzer.py — fit any GLB backdrop to the stage and locate character spots.

For a given backdrop it:
  1. imports the GLB and drops Sketchfab preview objects,
  2. "zooms in if necessary": scales so the *interior* height (floor->ceiling)
     reaches ROOM_CEILING and snaps the main walking floor to z=0,
  3. ray-casts the floor to build a walkable grid (ground at z=0, no obstacles,
     head clearance),
  4. clusters walkable cells, ranks candidate character spots (clearance +
     region size),
  5. writes a JSON report, a schematic floor map, and a perspective preview
     contact sheet (a capsule standing at each spot) for human validation.

Usage:
  blender -b -P room_analyzer.py -- <backdrop.glb> <outdir> [--ceiling=2.35]
      [--cell=0.15] [--clearance=0.30] [--max-spots=12] [--label=kitchen]
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

import bpy
from mathutils import Vector

ROOM_CEILING = 2.35
CH_HEIGHT = 1.70      # character height for clearance checks / preview capsule
CH_HEAD = 1.55        # head height for preview camera target
HEAD_PROBE = 1.85     # clearance probe height above the floor
OUT_W, OUT_H = 1920, 1080
SENSOR_W = 36.0


def log(msg: str) -> None:
    print(f"[room] {msg}", flush=True)


def parse_args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:]
    opts = {"backdrop": None, "out": None, "ceiling": ROOM_CEILING,
            "cell": 0.15, "clearance": 0.30, "max_spots": 12, "label": None,
            "debug": False}
    pos = []
    for a in argv:
        if a.startswith("--ceiling="):
            opts["ceiling"] = float(a.split("=", 1)[1])
        elif a.startswith("--cell="):
            opts["cell"] = float(a.split("=", 1)[1])
        elif a.startswith("--clearance="):
            opts["clearance"] = float(a.split("=", 1)[1])
        elif a.startswith("--max-spots="):
            opts["max_spots"] = int(a.split("=", 1)[1])
        elif a.startswith("--label="):
            opts["label"] = a.split("=", 1)[1]
        elif a.startswith("--debug"):
            opts["debug"] = True
        else:
            pos.append(a)
    if pos:
        opts["backdrop"] = pos[0]
        opts["out"] = pos[1] if len(pos) > 1 else None
    return opts


def import_and_clean(path: Path, scene: bpy.types.Scene) -> None:
    if not path.exists():
        log(f"ERROR: backdrop not found: {path}")
        raise SystemExit(2)
    bpy.ops.import_scene.gltf(filepath=str(path))
    for o in list(bpy.data.objects):
        if o.name in ("Cube", "Light", "Camera"):
            for coll in list(o.users_collection):
                coll.objects.unlink(o)
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.context.view_layer.update()
    # drop every non-mesh object (preview empties/lights/cameras)
    for o in list(bpy.data.objects):
        if o.type != "MESH":
            for coll in list(o.users_collection):
                coll.objects.unlink(o)
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.context.view_layer.update()


def bake_world_transform(scene: bpy.types.Scene) -> bpy.types.Object:
    """Flatten parents/scale/location so mesh local coords == world coords,
    then join every mesh into a single object."""
    meshes = [o for o in scene.objects if o.type == "MESH"]
    if not meshes:
        log("ERROR: no meshes in backdrop")
        raise SystemExit(2)
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.context.view_layer.update()
    if len(meshes) > 1:
        bpy.ops.object.join()
    room = bpy.context.view_layer.objects.active
    room.name = "Room"
    room.data.name = "RoomMesh"
    bpy.context.view_layer.update()
    return room


def ensure_z_up(room: bpy.types.Object) -> bool:
    """Some GLBs are Y-up (floor normal is +Y). Detect and rotate to Z-up
    so the rest of the pipeline (Z is up) works."""
    room.data.calc_loop_triangles()
    m3 = room.matrix_world.to_3x3()
    nz = ny = 0
    for t in room.data.loop_triangles:
        try:
            n = (m3 @ t.normal).normalized()
        except Exception:
            continue
        if abs(n.z) > 0.9:
            nz += 1
        elif abs(n.y) > 0.9:
            ny += 1
    if ny > nz * 1.5 and ny > 100:
        log(f"  fix: Y-up detected (ny={ny} nz={nz}) -> rotating -90deg X to Z-up")
        room.rotation_euler = (math.radians(-90), 0, 0)
        bpy.context.view_layer.update()
        bpy.ops.object.select_all(action="DESELECT")
        room.select_set(True)
        bpy.context.view_layer.objects.active = room
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        bpy.context.view_layer.update()
        return True
    return False


def mesh_arrays(room: bpy.types.Object):
    room.data.calc_loop_triangles()
    mw = room.matrix_world
    m3 = mw.to_3x3()
    verts = np.array([(mw @ v.co).to_tuple() for v in room.data.vertices],
                     dtype=float)
    tris = room.data.loop_triangles
    tri_v = np.array([list(t.vertices) for t in tris], dtype=int)
    nz = np.array([(m3 @ t.normal).normalized().z for t in tris], dtype=float)
    s = m3[0][0] if m3[0][0] else 1.0
    area = np.array([t.area * s * s for t in tris], dtype=float)
    return verts, tri_v, nz, area


def world_bounds(room: bpy.types.Object):
    mw = room.matrix_world
    xs = ys = zs = None
    for v in room.data.vertices:
        p = mw @ v.co
        if xs is None:
            xs, ys, zs = [], [], []
        xs.append(p.x); ys.append(p.y); zs.append(p.z)
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def surface_bins(verts, tri_v, nz, area, sign, min_area_frac=0.02, bin_w=0.05):
    """Area-weighted histogram of triangle z-centroids with normal.z*`sign`>0.92.
    Returns [(z, area)] sorted by area desc, dropping tiny bins."""
    c = verts[tri_v].mean(axis=1)
    m = (nz * sign) > 0.92
    z = c[m, 2]
    a = area[m]
    if len(z) == 0:
        return []
    zmin = float(z.min())
    k = ((z - zmin) / bin_w).astype(int)
    h = np.bincount(k, weights=a)
    total = h.sum()
    out = []
    for t in range(len(h)):
        if h[t] > total * min_area_frac:
            out.append((zmin + (t + 0.5) * bin_w, float(h[t])))
    out.sort(key=lambda v: -v[1])
    return out


def occupancy_between(verts, tri_v, area, z0, z1):
    c = verts[tri_v].mean(axis=1)
    m = (c[:, 2] >= z0) & (c[:, 2] <= z1)
    return float(area[m].sum())


def detect_levels(verts, tri_v, nz, area):
    """Find the main walking floor + ceiling in current (preliminary) units."""
    ups = surface_bins(verts, tri_v, nz, area, +1)
    dns = surface_bins(verts, tri_v, nz, area, -1)
    zmin = float(verts[:, 2].min())
    zmax = float(verts[:, 2].max())
    if not ups:
        return zmin, zmax, [], []
    zlo = zmin + 0.15 * (zmax - zmin)
    zhi = zmax - 0.15 * (zmax - zmin)
    mid = [(z, a) for z, a in ups if zlo <= z <= zhi]
    if mid:
        floor_z = max(mid, key=lambda t: t[1])[0]
    else:
        floor_z = max(ups, key=lambda t: t[1])[0]
    cands = [(z, a) for z, a in dns if z > floor_z + 1.0]
    ceiling_z = max(cands, key=lambda t: t[1])[0] if cands else zmax
    return floor_z, ceiling_z, ups, dns


def ray_cast_down(room, x, y, z0):
    mw_inv = room.matrix_world.inverted()
    o = mw_inv @ Vector((x, y, z0))
    d = mw_inv.to_3x3() @ Vector((0.0, 0.0, -1.0))
    res, loc, _, _ = room.ray_cast(o, d)
    if not res:
        return None
    return (room.matrix_world @ loc).z


def ray_hit_above(room, x, y, z0):
    """Cast up from (x, y, z0); return world z of first hit or None."""
    mw_inv = room.matrix_world.inverted()
    o = mw_inv @ Vector((x, y, z0))
    d = mw_inv.to_3x3() @ Vector((0.0, 0.0, 1.0))
    res, loc, _, _ = room.ray_cast(o, d)
    if not res:
        return None
    return (room.matrix_world @ loc).z


def pick_floor_ceiling(ups, dns, zmin, zmax, target_ceiling):
    """Pick walking floor + ceiling.

    Floor = the largest up-facing surface that has headroom for a 1.7m
    character (any down-facing surface >= floor + 1.7m). This rejects
    furniture tops (bed top close to ceiling) while keeping the real floor.
    Ceiling = the highest significant down-facing surface above floor+1.0."""
    floor_thresh = max(0.3, 0.01 * sum(a for _, a in ups))
    cands = [(z, a) for z, a in ups if a >= floor_thresh]
    if not cands:
        return zmin, zmax
    dn_thresh = max(0.3, 0.02 * sum(a for _, a in dns)) if dns else 0.3
    sig_dn = [(z, a) for z, a in dns if a >= dn_thresh]
    # largest area with headroom (character can stand; 1.9m rejects furniture tops like beds)
    # headroom is satisfied if the model's top (zmax) is high enough; down surfaces
    # are not reliable for this (ceilings may be tiny/missing)
    for z, a in sorted(cands, key=lambda c: -c[1]):
        if z + 1.9 <= zmax:
            above = [dz for dz, _ in sig_dn if dz >= z + 1.0]
            top = max(above) if above else max([dz for dz, _ in dns if dz >= z + 1.0], default=zmax)
            return z, top
    # none has headroom: fallback to the largest surface
    z, a = max(cands, key=lambda c: c[1])
    above = [dz for dz, _ in sig_dn if dz >= z + 1.0]
    if above:
        return z, max(above)
    return z, zmax


def fit_room(room, opts):
    """Fit to the pipeline convention: zmax scaled to the ceiling target,
    main floor shifted to z=0 (no rescale), room centered on x/y."""
    verts, tri_v, nz, area = mesh_arrays(room)
    bounds = world_bounds(room)
    zmin = float(verts[:, 2].min())
    zmax = float(verts[:, 2].max())
    ups = surface_bins(verts, tri_v, nz, area, +1)
    dns = surface_bins(verts, tri_v, nz, area, -1)
    floor_z, ceiling_z = pick_floor_ceiling(ups, dns, zmin, zmax, opts["ceiling"])
    log(f"  fit dbg: ups_n={len(ups)} dns_n={len(dns)} "
        f"top_ups={[(round(z,2), round(a,2)) for z, a in ups[:6]]} "
        f"top_dns={[(round(z,2), round(a,2)) for z, a in dns[:6]]} "
        f"floor={floor_z:.3f} ceil={ceiling_z:.3f}")
    interior = ceiling_z - floor_z
    if interior < 0.8:
        interior = max(0.8, zmax - zmin)
        ceiling_z = floor_z + interior
    room.location = (-(bounds[0] + bounds[1]) / 2.0, -(bounds[2] + bounds[3]) / 2.0,
                     -floor_z)
    log(f"  fit dbg: floor_raw={floor_z:.3f} ceil_raw={ceiling_z:.3f} "
        f"interior={interior:.2f} zmin={zmin:.2f} zmax={zmax:.2f}")
    bpy.context.evaluated_depsgraph_get().update()
    verts, tri_v, nz, area = mesh_arrays(room)
    bounds = world_bounds(room)
    ups = surface_bins(verts, tri_v, nz, area, +1)
    dns = surface_bins(verts, tri_v, nz, area, -1)
    return {
        "scale2": 1.0, "floor_raw": floor_z, "ceiling_raw": ceiling_z,
        "interior_raw": interior, "bounds": bounds,
        "floor_z": 0.0, "ceiling_z": ceiling_z - floor_z,
        "height": ceiling_z - floor_z,
        "room_center": ((bounds[0] + bounds[1]) / 2.0, (bounds[2] + bounds[3]) / 2.0),
        "floor_candidates": [{"z": z, "area": a} for z, a in ups[:6]],
        "ceiling_candidates": [{"z": z, "area": a} for z, a in dns[:6]],
    }


def build_walkable(room, bounds, cell, clearance, floor_target):
    x0, x1, y0, y1 = bounds[0], bounds[1], bounds[2], bounds[3]
    x0 -= 0.4; x1 += 0.4; y0 -= 0.4; y1 += 0.4
    cols = max(1, int(math.ceil((x1 - x0) / cell)))
    rows = max(1, int(math.ceil((y1 - y0) / cell)))
    xc = x0 + (np.arange(cols) + 0.5) * cell
    yc = y0 + (np.arange(rows) + 0.5) * cell
    probe_z = floor_target + 2.0
    grid = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        for c in range(cols):
            h = ray_cast_down(room, xc[c], yc[r], probe_z)
            if h is None or abs(h - floor_target) > 0.1:
                continue
            hz = ray_hit_above(room, xc[c], yc[r], floor_target + 0.05)
            if hz is not None and (hz - floor_target) < CH_HEIGHT + 0.15:
                continue
            grid[r, c] = True
    # erosion layers -> clearance distance
    erode_rad = max(1, int(round(clearance / cell)))
    layers = np.zeros_like(grid, dtype=int)
    cur = grid.copy()
    layer = 0
    while cur.any():
        layer += 1
        layers[cur] = layer
        eroded = cur.copy()
        eroded[:, 1:] &= cur[:, :-1]
        eroded[:, :-1] &= cur[:, 1:]
        eroded[1:, :] &= cur[:-1, :]
        eroded[:-1, :] &= cur[1:, :]
        if (eroded == cur).all() and layer > erode_rad:
            break
        cur = eroded
    return grid, layers, xc, yc


def cluster_and_spots(grid, layers, xc, yc, max_spots):
    rows, cols = grid.shape
    seen = np.zeros_like(grid, dtype=bool)
    regions = []
    for r in range(rows):
        for c in range(cols):
            if not grid[r, c] or seen[r, c]:
                continue
            stack = [(r, c)]
            seen[r, c] = True
            cells = []
            while stack:
                cr, cc = stack.pop()
                cells.append((cr, cc))
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nr, nc = cr + dr, cc + dc
                    if 0 <= nr < rows and 0 <= nc < cols and grid[nr, nc] \
                            and not seen[nr, nc]:
                        seen[nr, nc] = True
                        stack.append((nr, nc))
            regions.append(cells)
    spots = []
    cx = xc.mean()
    cy = yc.mean()
    for cells in sorted(regions, key=len, reverse=True):
        best_clear = max(layers[r, c] for r, c in cells)
        best = max(cells, key=lambda rc: (layers[rc[0], rc[1]], -((xc[rc[1]] - cx) ** 2 + (yc[rc[0]] - cy) ** 2)))
        spots.append((best, best_clear, len(cells)))
        if len(spots) >= max_spots:
            break
    spots.sort(key=lambda s: (s[1], s[2]), reverse=True)
    out = []
    for (r, c), clear, area in spots:
        out.append({"x": float(xc[c]), "y": float(yc[r]), "z": 0.0,
                    "clearance": round(float(clear) * 0.15, 2),
                    "region_area": round(float(area) * 0.15 * 0.15, 2)})
    return out


def room_center_facing(spot, center):
    dx = center[0] - spot["x"]
    dy = center[1] - spot["y"]
    n = math.hypot(dx, dy)
    if n < 1e-4:
        return {"x": 0.0, "y": -1.0}
    return {"x": dx / n, "y": dy / n}


def setup_lighting(scene):
    def light(name, kind, loc, strength, size=3.0, color=(1.0, 0.98, 0.94),
              ang=None):
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

    light("Key", "AREA", (1.6, -1.8, 3.4), 500, size=3.0)
    light("Fill", "AREA", (-2.6, -0.6, 1.6), 150, size=2.5,
          color=(0.82, 0.88, 1.0))
    light("Rim", "SPOT", (0.4, 2.0, 2.6), 180,
          ang=(math.radians(-45), 0.0, math.radians(90)))
    scene.world = bpy.data.worlds.new("SceneWorld")
    scene.world.color = (0.02, 0.02, 0.025)


def setup_render(scene, w, h):
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = w
    scene.render.resolution_y = h
    scene.render.image_settings.file_format = "PNG"
    eevee = scene.eevee
    if hasattr(eevee, "render_samples"):
        eevee.render_samples = 8
    elif hasattr(eevee, "taa_render_samples"):
        eevee.taa_render_samples = 8
    scene.render.film_transparent = True


def spot_marker_material():
    mat = bpy.data.materials.new("SpotMarker")
    mat.use_nodes = True
    ng = mat.node_tree
    ng.nodes.clear()
    out = ng.nodes.new("ShaderNodeOutputMaterial")
    em = ng.nodes.new("ShaderNodeEmission")
    em.inputs[0].default_value = (1.0, 0.15, 0.5, 1.0)
    em.inputs[1].default_value = 4.0
    ng.links.new(em.outputs[0], out.inputs["Surface"])
    return mat


def add_marker(x, y, z=0.0, mat=None):
    bpy.ops.mesh.primitive_cylinder_add(radius=0.28, depth=CH_HEIGHT,
                                        location=(x, y, CH_HEIGHT / 2))
    body = bpy.context.view_layer.objects.active
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.19, location=(x, y, CH_HEAD + 0.1))
    head = bpy.context.view_layer.objects.active
    if mat is not None:
        for o in (body, head):
            if o.data.materials:
                o.data.materials[0] = mat
            else:
                o.data.materials.append(mat)
    return body, head


def export_glb(objs, filepath):
    try:
        bpy.ops.object.select_all(action="DESELECT")
        for o in objs:
            o.select_set(True)
        if not bpy.context.selected_objects:
            log(f"export glb: nothing selected for {Path(filepath).name}")
            return False
        bpy.ops.export_scene.gltf(
            filepath=str(filepath), use_selection=True, export_apply=True)
        log(f"exported {Path(filepath).name}")
        return True
    except Exception as e:
        log(f"export glb {Path(filepath).name} FAILED: {e}")
        return False


def make_camera(scene, name, loc, target, focal=20.0):
    dat = bpy.data.cameras.new(name)
    dat.lens = focal
    dat.sensor_width = SENSOR_W
    cam = bpy.data.objects.new(name, dat)
    scene.collection.objects.link(cam)
    cam.location = loc
    d = (Vector(target) - cam.location).normalized()
    cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    return cam


def render_topdown(scene, bounds, out_file):
    x0, x1, y0, y1, z0, z1 = bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    cam = make_camera(scene, "TopDown", (cx, cy, z1 + 1.0), (cx, cy, z0))
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = max(y1 - y0, (x1 - x0) * OUT_H / OUT_W) + 0.4
    cam.rotation_euler = (0.0, 0.0, 0.0)
    scene.camera = cam
    scene.render.filepath = str(out_file)
    bpy.ops.render.render(write_still=True)
    return cam


def render_spot(scene, spot, center, out_file):
    dx = center[0] - spot["x"]
    dy = center[1] - spot["y"]
    n = math.hypot(dx, dy)
    if n < 1e-4:
        dx, dy = 0.0, -1.0
        n = 1.0
    ux, uy = dx / n, dy / n
    cam_loc = (spot["x"] - ux * 0.6, spot["y"] - uy * 0.6, CH_HEAD)
    cam = make_camera(scene, "SpotCam", cam_loc, (spot["x"], spot["y"], CH_HEAD),
                      focal=20.0)
    scene.camera = cam
    scene.render.filepath = str(out_file)
    bpy.ops.render.render(write_still=True)


def render_overview(scene, bounds, out_file):
    # Front-side view (like the pipeline camera, at -y looking +y), auto-framed
    # to the room size and rendered opaque so the room reads as a space.
    x0, x1, y0, y1, z0, z1 = bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    h = max(0.5, z1 - z0)
    w = max(x1 - x0, y1 - y0)
    focal = 24.0
    fill = 0.75
    d = max(h, w * (focal / 36.0)) * focal / (fill * 24.0)
    d = max(2.8, d)
    cam = make_camera(scene, "Overview", (cx, cy - d, 1.45), (cx, cy, 1.05),
                      focal=focal)
    prev = scene.render.film_transparent
    scene.render.film_transparent = False
    scene.camera = cam
    scene.render.filepath = str(out_file)
    bpy.ops.render.render(write_still=True)
    scene.render.film_transparent = prev
    return cam


def render_front(scene, bounds, out_file):
    """Wide front view with all spot markers visible — the 'set' shot."""
    x0, x1, y0, y1, z0, z1 = bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    h = max(0.5, z1 - z0)
    w = max(x1 - x0, y1 - y0)
    focal = 24.0
    fill = 0.70
    d = max(h, w * (focal / 36.0)) * focal / (fill * 24.0)
    d = max(2.8, d)
    cam = make_camera(scene, "Front", (cx, cy - d, 1.30), (cx, cy, 1.05),
                      focal=focal)
    prev = scene.render.film_transparent
    scene.render.film_transparent = False
    scene.camera = cam
    scene.render.filepath = str(out_file)
    bpy.ops.render.render(write_still=True)
    scene.render.film_transparent = prev
    return cam


def draw_floor_map(spots, grid, layers, xc, yc, bounds, out_file,
                   cell, max_layers):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return
    W, H = 1200, 900
    img = Image.new("RGB", (W, H), (12, 12, 16))
    d = ImageDraw.Draw(img)
    x0, x1, y0, y1 = bounds[0], bounds[1], bounds[2], bounds[3]
    pad = 60
    sx = (W - 2 * pad) / max(1e-6, (x1 - x0))
    sy = (H - 2 * pad) / max(1e-6, (y1 - y0))
    s = min(sx, sy)
    ox = (W - (x1 - x0) * s) / 2
    oy = (H - (y1 - y0) * s) / 2

    def w2p(px, py):
        return ox + (px - x0) * s, oy + (py - y0) * s

    for r in range(grid.shape[0]):
        for c in range(grid.shape[1]):
            if not grid[r, c]:
                continue
            l = layers[r, c]
            a = 60 + int(90 * (l / max(1, max_layers)))
            g = 120 + int(90 * (l / max(1, max_layers)))
            px, py = w2p(xc[c] - cell / 2, yc[r] - cell / 2)
            d.rectangle([px, py, px + cell * s, py + cell * s],
                        fill=(20, g, a))
    for i, sp in enumerate(spots, 1):
        px, py = w2p(sp["x"], sp["y"])
        d.ellipse([px - 8, py - 8, px + 8, py + 8], fill=(255, 60, 60),
                  outline=(255, 255, 255))
        d.text((px + 10, py - 6), str(i), fill=(255, 255, 255))
    try:
        fnt = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 16)
    except Exception:
        fnt = ImageFont.load_default()
    d.text((10, 8), f"floor map - {len(spots)} candidate spots",
           fill=(220, 220, 220), font=fnt)
    img.save(out_file)


def montage_previews(spot_files, out_file, labels):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return
    thumbs = []
    for f in spot_files:
        im = Image.open(f).convert("RGB")
        thumbs.append(im)
    if not thumbs:
        return
    tw, th = thumbs[0].size
    label_h = 26
    cols = 4
    rows = int(math.ceil(len(thumbs) / cols))
    sheet = Image.new("RGB", (cols * tw, rows * (th + label_h) + 30),
                      (10, 10, 12))
    d = ImageDraw.Draw(sheet)
    try:
        fnt = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 13)
    except Exception:
        fnt = ImageFont.load_default()
    d.text((10, 8), "candidate spots - capsule = character standing here",
           fill=(220, 220, 220), font=fnt)
    for i, (im, lab) in enumerate(zip(thumbs, labels)):
        r, c = divmod(i, cols)
        x, y = c * tw, 30 + r * (th + label_h)
        sheet.paste(im, (x, y))
        d.rectangle([x, y + th, x + tw, y + th + label_h], fill=(30, 30, 36))
        d.text((x + 6, y + th + 5), lab, fill=(255, 220, 120), font=fnt)
    sheet.save(out_file)


def main(opts) -> int:
    scene = bpy.context.scene
    backdrop = Path(opts["backdrop"])
    out = Path(opts["out"]) if opts["out"] else \
        Path(__file__).resolve().parents[1] / "jobs" / "rooms" / \
        (opts["label"] or backdrop.stem)
    out.mkdir(parents=True, exist_ok=True)

    log(f"main() opts.debug={opts.get('debug')} backdrop={backdrop}")
    import_and_clean(backdrop, scene)
    room = bake_world_transform(scene)
    ensure_z_up(room)
    verts, tri_v, nz, area = mesh_arrays(room)
    pre_bounds = world_bounds(room)

    # scale so the model's top (zmax) reaches the ceiling target (pipeline convention)
    scale1 = opts["ceiling"] / max(1e-6, pre_bounds[5])
    log(f"  fit dbg: pre_bounds={[round(v,2) for v in pre_bounds]} "
        f"scale1={scale1:.5f} room_scale_before={room.scale[0]:.4f}")
    room.scale = (room.scale[0] * scale1, room.scale[1] * scale1,
                  room.scale[2] * scale1)
    bpy.context.evaluated_depsgraph_get().update()
    log(f"  fit dbg: room_scale_after={room.scale[0]:.5f} "
        f"zmax_after={world_bounds(room)[5]:.3f}")
    fit = fit_room(room, opts)
    log(f"fit: floor={fit['floor_raw']:.2f}->{fit['floor_z']:.2f} "
        f"ceiling={fit['ceiling_raw']:.2f}->{fit['ceiling_z']:.2f} "
        f"height={fit['height']:.2f} scale={fit['scale2']:.3f}")
    verts, tri_v, nz, area = mesh_arrays(room)
    bounds = world_bounds(room)
    grid, layers, xc, yc = build_walkable(room, bounds, opts["cell"],
                                          opts["clearance"],
                                          floor_target=fit["floor_z"])
    floor_hits = 0
    head_blocks = 0
    n_probed = 0
    for r in range(0, grid.shape[0], max(1, grid.shape[0] // 20)):
        for c in range(0, grid.shape[1], max(1, grid.shape[1] // 20)):
            n_probed += 1
            h = ray_cast_down(room, float(xc[c]), float(yc[r]), 1.85)
            if h is not None and abs(h - fit["floor_z"]) <= 0.1:
                floor_hits += 1
                hz = ray_hit_above(room, float(xc[c]), float(yc[r]), HEAD_PROBE)
                if hz is not None and (hz - HEAD_PROBE) < 0.15:
                    head_blocks += 1
    log(f"grid dbg: probed={n_probed} floor_hits={floor_hits} "
        f"head_blocks={head_blocks} cells={grid.sum()}")
    np.savez(out / "grid.npz", grid=grid, layers=layers, xc=xc, yc=yc,
             bounds=np.array(bounds))
    spots = cluster_and_spots(grid, layers, xc, yc, opts["max_spots"])
    for sp in spots:
        sp["facing"] = room_center_facing(sp, fit["room_center"])

    report = {
        "label": opts["label"] or backdrop.stem,
        "source": str(backdrop),
        "preliminary_scale": scale1,
        "fit": {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in fit.items()},
        "walkable_cells": int(grid.sum()),
        "spots": spots,
    }
    with open(out / "room_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log(json.dumps(report, indent=2)[:1800])
    export_glb([room], out / "room_fitted.glb")

    setup_lighting(scene)
    setup_render(scene, 960, 540)
    render_overview(scene, bounds, out / "overview.png")

    if grid.sum() == 0:
        log("WARNING: no walkable floor found")
        return 0

    # --- previews ---
    setup_lighting(scene)
    setup_render(scene, OUT_W, OUT_H)
    top_file = out / "top_down.png"
    render_topdown(scene, bounds, top_file)

    scene.render.resolution_x = 960
    scene.render.resolution_y = 540
    mat = spot_marker_material()
    spot_files = []
    labels = []
    marker_objs = []
    for i, sp in enumerate(spots, 1):
        bodies, heads = add_marker(sp["x"], sp["y"], mat=mat)
        marker_objs += [bodies, heads]
        f = out / f"spot_{i:02d}.png"
        render_spot(scene, sp, fit["room_center"], f)
        spot_files.append(f)
        labels.append(f"#{i} clr={sp['clearance']:.1f}m "
                       f"({sp['x']:.2f},{sp['y']:.2f})")
        for o in marker_objs:
            for coll in list(o.users_collection):
                coll.objects.unlink(o)
            bpy.data.objects.remove(o, do_unlink=True)
        marker_objs = []

    draw_floor_map(spots, grid, layers, xc, yc, bounds, out / "floor_map.png",
                   opts["cell"], int(layers.max()))
    montage_previews(spot_files, out / "spots_preview.png", labels)

    mat2 = spot_marker_material()
    marker_objs = []
    for sp in spots:
        bodies, heads = add_marker(sp["x"], sp["y"], mat=mat2)
        marker_objs += [bodies, heads]
    # Front "set" shot with all markers visible (opaque, well-framed)
    try:
        render_front(scene, bounds, out / "front.png")
    except Exception as e:
        log(f"front render failed: {e}")
    export_glb([room] + marker_objs, out / "spots.glb")
    for o in marker_objs:
        for coll in list(o.users_collection):
            coll.objects.unlink(o)
        bpy.data.objects.remove(o, do_unlink=True)

    log(f"wrote report + previews to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(parse_args()))