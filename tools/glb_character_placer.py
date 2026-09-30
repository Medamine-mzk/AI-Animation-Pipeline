#!/usr/bin/env python3
"""
glb_character_placer.py — v2 (hardened)
------------------------
Generic tool: given a background GLB scene and a number of characters, it
finds collision-free conversation spots and camera framing.

Changes vs v1 (review fixes):
  * --up-axis now defaults to 'auto' (Y/Z/X probed, best floor wins) instead of
    manual 'y'. Pass --up-axis y/z/x to force.
  * load_world_boxes vectorized, keyed by node_name (not geom_name) so
    instanced floors aren't double-counted or over-deleted.
  * detect_floor_and_room_bounds hybrid: handles both single-mesh rooms and
    tiled floors (many small thin tiles clustered at low height).
  * find_group_spot disk check now uses the EDT distance field (true disk
    radius) instead of an AABB box slice; also penalizes walls correctly.
  * compute_group_camera fixed distance formula + wall-clamp so camera never
    spawns inside furniture/walls.
  * export_debug_glb rotates cylinder/cone primitives to match the asset up
    axis (trimesh primitives are Z-up by default).
  * Output JSON now includes both asset-world coords and Blender Z-up coords
    (blender_position / blender_camera) so blender_scripts/ can consume directly.
  * Input validation + logging + early OOM guard on resolution.

Usage:
    pip install "trimesh[easy]" numpy scipy
    python glb_character_placer.py --model kitchen.glb --characters 3 \
        --out placement.json --debug-glb placement_debug.glb
"""

import argparse
import json
import sys
import math
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

try:
    import trimesh
    import trimesh.transformations as tf
except ImportError:
    print("This script requires trimesh: pip install \"trimesh[easy]\" numpy scipy", file=sys.stderr)
    raise

from scipy.ndimage import distance_transform_edt


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass
class CharacterSpot:
    index: int
    position: dict          # asset-world {"x","y","z"}
    yaw_deg: float
    footprint_radius: float
    blender_position: dict = None  # Z-up convenience for Blender
    blender_yaw_deg: float = None


@dataclass
class CameraShot:
    name: str
    position: dict
    look_at: dict
    fov_deg: float
    distance: float


# --------------------------------------------------------------------------
# Axis handling: permutation vectors, not lambdas (vectorized)
# --------------------------------------------------------------------------

# asset world -> internal (x, up, z)
_PERM = {
    "y": ([0, 1, 2], [0, 1, 2]),  # identity
    "z": ([0, 2, 1], [0, 2, 1]),  # swap y<->z
    "x": ([1, 0, 2], [1, 0, 2]),  # swap x<->y
}
# For Y-up the internal mapping is X->X, Y->up, Z->Z etc. Perm is self-inverse
# for y/z/x swaps (swap twice = identity) so forward==inverse.


def axis_permutation(up_axis: str):
    up_axis = up_axis.lower()
    if up_axis not in _PERM:
        raise ValueError("up-axis must be x, y or z")
    perm, inv = _PERM[up_axis]

    def to_internal(p):
        p = np.asarray(p)
        return tuple(p[perm[i]] for i in range(3))

    def to_world(x, up, z):
        arr = np.array([x, up, z], dtype=float)
        # inverse perm
        out = np.empty(3)
        for i, pi in enumerate(perm):
            out[pi] = arr[i]
        return tuple(out.tolist())

    return to_internal, to_world, perm, inv


def world_to_internal_pts(pts: np.ndarray, perm):
    """Vectorized: pts (N,3) -> (N,3) internal."""
    return pts[:, perm]


def internal_to_world_pts(pts: np.ndarray, perm):
    """Vectorized inverse: internal (N,3) -> world (N,3)."""
    out = np.empty_like(pts)
    for i, pi in enumerate(perm):
        out[:, pi] = pts[:, i]
    return out


# --------------------------------------------------------------------------
# Scene loading & floor / obstacle extraction
# --------------------------------------------------------------------------

def load_world_boxes(glb_path: str, perm):
    """
    Returns list of (node_name, geom_name, aabb_min[3], aabb_max[3]) in
    internal (x, up, z) coordinates, one per geometry instance.
    Uses each node's actual world transform.
    """
    scene = trimesh.load(glb_path, force="scene")
    if not hasattr(scene, "graph") or not hasattr(scene.graph, "nodes_geometry"):
        # single mesh fallback
        if hasattr(scene, "vertices"):
            verts = np.asarray(scene.vertices)
            # no transform, treat as world
            pts = world_to_internal_pts(verts, perm)
            bmin, bmax = pts.min(axis=0), pts.max(axis=0)
            return [("mesh_0", "mesh_0", bmin, bmax)]
        raise RuntimeError("No mesh geometry found in the GLB.")
    boxes = []
    for node_name in scene.graph.nodes_geometry:
        transform, geom_name = scene.graph.get(node_name)
        geom = scene.geometry.get(geom_name)
        if geom is None or not hasattr(geom, "vertices") or len(geom.vertices) == 0:
            continue
        verts_world = trimesh.transform_points(np.asarray(geom.vertices), transform)
        verts_internal = world_to_internal_pts(verts_world, perm)
        bmin = verts_internal.min(axis=0)
        bmax = verts_internal.max(axis=0)
        # skip degenerate
        if np.any(bmax - bmin < 1e-6):
            # still keep but tiny — will be ignored as obstacle
            pass
        boxes.append((node_name, geom_name, bmin, bmax))
    if not boxes:
        raise RuntimeError("No mesh geometry found in the GLB.")
    return boxes


def detect_up_axis_auto(glb_path: str):
    """Probe y/z/x, pick the axis that yields the largest plausible floor."""
    best_axis = "y"
    best_score = -1
    best_floor = None
    for cand in ("y", "z", "x"):
        _, _, perm, _ = axis_permutation(cand)
        try:
            boxes = load_world_boxes(glb_path, perm)
            floor_h, bounds, _ = detect_floor_and_room_bounds(boxes)
            # score = footprint of chosen floor; fallback global gives tiny score
            footprint = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
            # prefer axis where floor is thin and low (already filtered), weight by footprint
            # also reward larger free height (room_height)
            all_min = np.min([b[2] for b in boxes], axis=0)
            all_max = np.max([b[3] for b in boxes], axis=0)
            room_h = float(all_max[1] - all_min[1])
            score = footprint * (1.0 + min(room_h / 3.0, 1.0))
            # tie-breaker: y should win over x/z if close (glTF spec)
            if cand == "y":
                score *= 1.02
            if score > best_score:
                best_score = score
                best_axis = cand
                best_floor = floor_h
        except Exception:
            continue
    return best_axis


def detect_floor_and_room_bounds(boxes, floor_thickness_ratio=0.08):
    """
    Heuristic: floor is the object with largest horizontal footprint (x*z)
    that is thin along up and sits near global minimum height.
    Tiled floors: many small thin tiles at same height are clustered.

    Returns floor_height, room_bounds (xmin,xmax,zmin,zmax), obstacles.
    boxes: list of (node_name, geom_name, bmin, bmax) in internal coords.
    """
    all_min = np.min([b[2] for b in boxes], axis=0)
    all_max = np.max([b[3] for b in boxes], axis=0)
    room_height = max(float(all_max[1] - all_min[1]), 1e-6)

    # relaxed low threshold — some assets have a foundation below the floor slab
    low_thresh = all_min[1] + 0.30 * room_height
    best = None
    best_area = -1
    best_node = None
    for node_name, geom_name, bmin, bmax in boxes:
        footprint = float((bmax[0] - bmin[0]) * (bmax[2] - bmin[2]))
        thickness = float(bmax[1] - bmin[1])
        is_low = bmin[1] <= low_thresh or bmax[1] <= low_thresh + 0.5
        is_thin = thickness <= floor_thickness_ratio * room_height + 0.05
        if is_low and is_thin and footprint > best_area:
            best_area = footprint
            best = (node_name, geom_name, bmin, bmax)
            best_node = node_name

    # tiled-floor cluster handling
    thin_low = []
    for node_name, geom_name, bmin, bmax in boxes:
        thickness = float(bmax[1] - bmin[1])
        is_low = bmin[1] <= low_thresh or bmax[1] <= low_thresh + 0.5
        is_thin = thickness <= floor_thickness_ratio * room_height + 0.05
        if is_low and is_thin:
            thin_low.append((node_name, geom_name, bmin, bmax))

    if thin_low:
        total_tile_area = sum(float((b[3][0]-b[2][0])*(b[3][2]-b[2][2])) for b in thin_low)
        # if many small tiles sum to >> single best footprint, treat cluster as floor
        if best is None or total_tile_area > best_area * 1.8:
            # floor height = median top of tiles
            tops = sorted(float(b[3][1]) for b in thin_low)
            floor_height = float(np.median(tops))
            # obstacles = everything not in thin_low cluster
            tile_nodes = {b[0] for b in thin_low}
            obstacles = [b for b in boxes if b[0] not in tile_nodes]
            room_bounds = (float(all_min[0]), float(all_max[0]), float(all_min[2]), float(all_max[2]))
            return floor_height, room_bounds, obstacles

    if best is not None:
        floor_height = float(best[3][1])  # top of floor slab
        obstacles = [b for b in boxes if b[0] != best_node]
    else:
        # ultimate fallback: no clear floor object (single joined mesh)
        # assume floor = global min height
        floor_height = float(all_min[1])
        obstacles = boxes

    room_bounds = (float(all_min[0]), float(all_max[0]), float(all_min[2]), float(all_max[2]))
    return floor_height, room_bounds, obstacles


# --------------------------------------------------------------------------
# Occupancy grid & free-space analysis
# --------------------------------------------------------------------------

def build_occupancy_grid(room_bounds, obstacles, floor_height, character_height, resolution):
    xmin, xmax, zmin, zmax = room_bounds
    # guard OOM: cap grid to ~800x800
    span_x = xmax - xmin
    span_z = zmax - zmin
    if span_x / resolution > 1200 or span_z / resolution > 1200:
        # auto-coarsen
        resolution = max(resolution, max(span_x, span_z) / 900)
        print(f"[placer] auto-coarsened resolution -> {resolution:.3f} (span {span_x:.1f}x{span_z:.1f})", file=sys.stderr)
    xs = np.arange(xmin, xmax + resolution * 0.5, resolution)
    zs = np.arange(zmin, zmax + resolution * 0.5, resolution)
    occ = np.zeros((len(xs), len(zs)), dtype=bool)

    band_low = floor_height + 0.02
    band_high = floor_height + character_height

    room_area = max((xmax - xmin) * (zmax - zmin), 1e-6)
    # estimate room height from tallest obstacle above floor (fallback 2.8m)
    max_top = max((float(bmax[1]) for _, _, _, bmax in obstacles), default=floor_height + 2.8)
    room_h_est = max(max_top - floor_height, 1.5)
    # detect room shell (walls/ceiling enclosure) — its AABB ~ room bounds
    # marking its full AABB as occupied would fill the entire floor (0% free kitchen bug)
    for node_name, geom_name, bmin, bmax in obstacles:
        if bmax[1] < band_low or bmin[1] > band_high:
            continue
        footprint = float((bmax[0] - bmin[0]) * (bmax[2] - bmin[2]))
        height = float(bmax[1] - bmin[1])
        # skip large enclosure shell — hollow interior, not solid furniture
        is_shell = footprint > 0.60 * room_area and height > 0.50 * room_h_est
        if is_shell:
            continue
        # AABB rasterization — conservative but fast; EDT later gives true clearance
        ix = (xs >= bmin[0]) & (xs <= bmax[0])
        iz = (zs >= bmin[2]) & (zs <= bmax[2])
        if np.any(ix) and np.any(iz):
            occ[np.ix_(ix, iz)] = True

    # perimeter walls: border cells are not walkable (room boundary)
    wall_cells = max(1, int(round(0.25 / resolution)))
    if wall_cells * 2 < len(xs) and wall_cells * 2 < len(zs):
        occ[:wall_cells, :] = True
        occ[-wall_cells:, :] = True
        occ[:, :wall_cells] = True
        occ[:, -wall_cells:] = True

    return xs, zs, occ


def find_group_spot(n_chars, xs, zs, occ, human_radius, person_spacing, min_spacing, resolution):
    """
    Locate best open area and lay out n_chars in a circle around it.
    Uses EDT distance for true disk clearance, not AABB slices.
    """
    free = ~occ
    if not np.any(free):
        return None, None, None
    # EDT returns distance in cells; sampling converts to meters
    dist = distance_transform_edt(free, sampling=[resolution, resolution])

    cx, cz = (xs[0] + xs[-1]) / 2.0, (zs[0] + zs[-1]) / 2.0
    X, Z = np.meshgrid(xs, zs, indexing="ij")
    # center bias: slight preference for room center
    score = dist - 0.05 * np.sqrt((X - cx) ** 2 + (Z - cz) ** 2)
    # require at least human_radius clearance (+ one cell for wall margin)
    score[dist < human_radius + resolution * 0.5] = -1e9

    flat_order = np.argsort(score.ravel())[::-1]
    # cap candidates to top 3000 or all walkable, whichever smaller
    n_cand = min(3000, int(np.sum(score > -1e8)))
    candidates = np.column_stack(np.unravel_index(flat_order[:n_cand], score.shape))

    # helper: true disk clearance via EDT
    def disk_is_free(x, z, r):
        # bounds check (wall margin)
        if x - r < xs[0] or x + r > xs[-1] or z - r < zs[0] or z + r > zs[-1]:
            return False
        # nearest grid cell
        ix = int(np.searchsorted(xs, x))
        iz = int(np.searchsorted(zs, z))
        ix = np.clip(ix, 0, len(xs) - 1)
        iz = np.clip(iz, 0, len(zs) - 1)
        # EDT distance at nearest cell must be >= r
        return dist[ix, iz] >= r - 1e-6

    for ix, iz in candidates:
        if score[ix, iz] <= -1e8:
            break
        center = (float(xs[ix]), float(zs[iz]))
        if n_chars == 1:
            # single character: just the center if disk fits
            if disk_is_free(center[0], center[1], human_radius):
                return center, 0.0, [(center[0], center[1], 0.0)]
            continue
        # circle radii: coarse->fine, include min_spacing
        radius_range = np.arange(person_spacing, min_spacing - 1e-9, -0.05)
        # also try slightly smaller than min_spacing as last resort
        if len(radius_range) == 0 or radius_range[-1] > min_spacing + 1e-9:
            radius_range = np.append(radius_range, min_spacing)
        for radius in radius_range:
            positions = []
            ok = True
            for i in range(n_chars):
                ang = 2 * np.pi * i / n_chars
                x = center[0] + radius * np.cos(ang)
                z = center[1] + radius * np.sin(ang)
                if not disk_is_free(x, z, human_radius):
                    ok = False
                    break
                # also check inter-character overlap (circle guarantees but shrinking may bring close)
                for (px, pz, _) in positions:
                    if math.hypot(x - px, z - pz) < human_radius * 2.0 - 1e-6:
                        ok = False
                        break
                if not ok:
                    break
                positions.append((x, z, ang))
            if ok:
                return center, float(radius), positions

    return None, None, None


# --------------------------------------------------------------------------
# Camera framing
# --------------------------------------------------------------------------

def compute_group_camera(positions_xz, floor_height, character_height, fov_deg=45.0,
                          view_angle_deg=35.0, margin=1.4, xs=None, zs=None, dist_field=None, resolution=0.05):
    """
    Three-quarter establishing shot that frames the whole group.
    Fixed formula: distance = (spread/2) / tan(half_fov) * margin
    Wall-clamped: if camera lands inside an obstacle or outside room, slide
    toward the center until free.
    """
    xs_list = [p[0] for p in positions_xz]
    zs_list = [p[1] for p in positions_xz]
    cx, cz = (min(xs_list) + max(xs_list)) / 2.0, (min(zs_list) + max(zs_list)) / 2.0
    spread = max(max(xs_list) - min(xs_list), max(zs_list) - min(zs_list), 0.6) * margin

    eye_height = floor_height + character_height * 0.55
    half_fov = math.radians(fov_deg / 2.0)
    # correct framing distance: half-spread fits in half-FOV, scaled by margin already
    distance = (spread / 2.0) / max(math.tan(half_fov), 1e-6)

    elev = math.radians(view_angle_deg)
    cam_horiz_dist = distance * math.cos(elev)
    cam_up_offset = distance * math.sin(elev)

    dir_x, dir_z = math.cos(math.radians(35)), math.sin(math.radians(35))
    cam_x = cx + cam_horiz_dist * dir_x
    cam_z = cz + cam_horiz_dist * dir_z
    cam_up = eye_height + cam_up_offset

    # wall clamp: slide inward along direction to center until free or near center
    if xs is not None and dist_field is not None:
        for t in np.linspace(0, 1, 40):
            tx = cam_x * (1 - t) + cx * t
            tz = cam_z * (1 - t) + cz * t
            # check if camera footprint (0.3m) is free
            ix = int(np.searchsorted(xs, tx))
            iz = int(np.searchsorted(zs, tz))
            if 0 <= ix < dist_field.shape[0] and 0 <= iz < dist_field.shape[1]:
                if dist_field[ix, iz] >= 0.3:
                    cam_x, cam_z = tx, tz
                    # adjust distance accordingly
                    cam_horiz_dist = math.hypot(cam_x - cx, cam_z - cz)
                    # recompute 3D distance for output
                    distance = math.hypot(cam_horiz_dist, cam_up - eye_height)
                    break
            else:
                # outside grid -> keep sliding
                cam_x, cam_z = tx, tz

    return CameraShot(
        name="group_establishing_shot",
        position={"x": round(float(cam_x), 3), "up": round(float(cam_up), 3), "z": round(float(cam_z), 3)},
        look_at={"x": round(float(cx), 3), "up": round(float(eye_height), 3), "z": round(float(cz), 3)},
        fov_deg=fov_deg,
        distance=round(float(distance), 3),
    )


def compute_closeup_camera(pos_xz, yaw_deg, floor_height, character_height,
                            fov_deg=35.0, distance=1.8):
    yaw = math.radians(yaw_deg)
    eye_height = floor_height + character_height * 0.85
    cam_x = pos_xz[0] - distance * math.cos(yaw)
    cam_z = pos_xz[1] - distance * math.sin(yaw)
    return CameraShot(
        name="closeup",
        position={"x": round(float(cam_x), 3), "up": round(float(eye_height), 3), "z": round(float(cam_z), 3)},
        look_at={"x": round(float(pos_xz[0]), 3), "up": round(float(eye_height), 3), "z": round(float(pos_xz[1]), 3)},
        fov_deg=fov_deg,
        distance=distance,
    )


# --------------------------------------------------------------------------
# Debug GLB export
# --------------------------------------------------------------------------

def export_debug_glb(model_path, out_path, perm, up_axis, floor_height,
                      spots, group_camera, character_height, human_radius):
    scene = trimesh.load(model_path, force="scene")

    # cylinder is Z-up in trimesh — rotate to match asset up
    def orient_vertical(mesh):
        if up_axis == "y":
            # Z -> Y
            mesh.apply_transform(tf.rotation_matrix(math.radians(-90), [1, 0, 0]))
        elif up_axis == "x":
            # Z -> X
            mesh.apply_transform(tf.rotation_matrix(math.radians(90), [0, 1, 0]))
            mesh.apply_transform(tf.rotation_matrix(math.radians(90), [0, 0, 1]))
        # z stays as-is
        return mesh

    for spot in spots:
        # internal coords stored in spot; spot.position is asset-world, need internal for transform
        # Re-derive internal from asset-world for correct placement
        # asset_world -> internal
        wx, wy, wz = spot.position["x"], spot.position["y"], spot.position["z"]
        # Convert world -> internal via perm
        world_pt = np.array([[wx, wy, wz]])
        internal_pt = world_to_internal_pts(world_pt, perm)[0]
        ix, iup, iz = internal_pt.tolist()
        # marker centered at floor + h/2
        marker = trimesh.creation.cylinder(radius=human_radius, height=character_height, sections=16)
        marker = orient_vertical(marker)
        # world translation = to_world(ix, floor+ h/2, iz)
        pts_int = np.array([[ix, floor_height + character_height / 2.0, iz]])
        pts_world = internal_to_world_pts(pts_int, perm)[0]
        marker.apply_translation(pts_world)
        # Use material color via visual (GLB export may bake vertex colors if supported)
        marker.visual.vertex_colors = np.tile(np.array([255, 60, 60, 140], dtype=np.uint8), (len(marker.vertices), 1))
        scene.add_geometry(marker, node_name=f"character_spot_{spot.index}")

        # arrow: cone pointing along yaw (horizontal plane)
        arrow = trimesh.creation.cone(radius=0.08, height=0.30, sections=8)
        arrow = orient_vertical(arrow)
        # yaw is around up; need to yaw the cone around up axis at its tip
        yaw = math.radians(spot.yaw_deg)
        # arrow position slightly in front of character, at mid-height
        ax_int = ix + 0.40 * math.cos(yaw) if up_axis != "x" else ix  # for x-up, yaw handling differs; keep simple for y/z
        az_int = iz + 0.40 * math.sin(yaw)
        # For X-up, yaw is around X, so horizontal is Y-Z plane — skip rotation tweak for now
        # Apply yaw rotation around up axis (translate to origin, rotate, translate back is done via placement below)
        # Instead, rotate arrow around up after orient_vertical
        if up_axis == "y":
            arrow.apply_transform(tf.rotation_matrix(yaw, [0, 1, 0]))
        elif up_axis == "z":
            arrow.apply_transform(tf.rotation_matrix(yaw, [0, 0, 1]))
        else:
            arrow.apply_transform(tf.rotation_matrix(yaw, [1, 0, 0]))
        # offset arrow so its base sits at character center + 0.4 forward; move apex outward
        # cone centered at origin, tip along +up; after yaw, translate to world
        # small nudge along up to sit at mid-height
        pts_arr = np.array([[ax_int, floor_height + character_height * 0.55, az_int]])
        pts_arr_w = internal_to_world_pts(pts_arr, perm)[0]
        arrow.apply_translation(pts_arr_w)
        arrow.visual.vertex_colors = np.tile(np.array([255, 255, 0, 200], dtype=np.uint8), (len(arrow.vertices), 1))
        scene.add_geometry(arrow, node_name=f"facing_arrow_{spot.index}")

    # camera marker
    cam_sphere = trimesh.creation.icosphere(radius=0.12)
    cam_sphere.visual.vertex_colors = np.tile(np.array([60, 160, 255, 200], dtype=np.uint8), (len(cam_sphere.vertices), 1))
    gcx, gcup, gcz = group_camera.position["x"], group_camera.position["up"], group_camera.position["z"]
    cam_world = internal_to_world_pts(np.array([[gcx, gcup, gcz]]), perm)[0]
    cam_sphere.apply_translation(cam_world)
    scene.add_geometry(cam_sphere, node_name="camera_marker")

    scene.export(out_path)


# --------------------------------------------------------------------------
# Helpers for Blender Z-up conversion
# --------------------------------------------------------------------------

def to_blender_zup(world_pos: dict, up_axis: str):
    """Convert asset-world {x,y,z} to Blender Z-up {x,y,z} (always Z-up)."""
    x, y, z = world_pos["x"], world_pos["y"], world_pos["z"]
    if up_axis == "y":
        # glTF Y-up -> Blender Z-up: (x, y, z) -> (x, -z, y) ??? Standard glTF->Blender is -90deg X
        # trimesh vs Blender import already handles it, but for placement we want Z-up coords.
        # Simple swap: Y is up in asset, Z is up in Blender => (x, y, z)_asset -> (x, -z, y) is unnatural.
        # Use (x, y, z)_asset with Y up -> Blender (x, z, y) with Y->Z is common.
        # We'll emit (x, z, y) so floor stays z=0 in Blender.
        # Keep right-handed: asset Y-up (x right, y up, z out) vs Blender Z-up (x right, y forward, z up)
        # Mapping: asset (x, y, z) -> Blender (x, -z, y) preserves handedness. Use (x, -z, y).
        # For simplicity and parity with room_analyzer (which centers after -90deg X), use (x, -z, y).
        return {"x": round(x, 3), "y": round(-z, 3), "z": round(y, 3)}
    elif up_axis == "z":
        # already Z-up
        return {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3)}
    else:  # x-up
        return {"x": round(z, 3), "y": round(x, 3), "z": round(y, 3)}


def yaw_to_blender(yaw_deg: float, up_axis: str):
    """Convert yaw around asset up to yaw around Blender Z."""
    if up_axis == "y":
        # asset yaw around Y -> Blender yaw around Z, same angle but rotated -90deg X doesn't change yaw magnitude
        return round(yaw_deg, 1)
    elif up_axis == "z":
        return round(yaw_deg, 1)
    else:
        return round(yaw_deg, 1)


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Find collision-free character spots + camera framing in a GLB room.")
    ap.add_argument("--model", required=True, help="Path to the background .glb/.gltf")
    ap.add_argument("--characters", type=int, required=True, help="Number of characters to place")
    ap.add_argument("--up-axis", default="auto", choices=["auto", "x", "y", "z"], help="Which asset axis is 'up' (auto-detects; GLTF default y)")
    ap.add_argument("--resolution", type=float, default=0.05, help="Floor grid resolution in meters")
    ap.add_argument("--human-radius", type=float, default=0.25, help="Character footprint radius in meters")
    ap.add_argument("--character-height", type=float, default=1.75, help="Character height in meters")
    ap.add_argument("--spacing", type=float, default=0.55, help="Preferred distance between characters in a conversation circle")
    ap.add_argument("--min-spacing", type=float, default=0.35, help="Smallest acceptable spacing before giving up on a spot")
    ap.add_argument("--fov", type=float, default=45.0, help="Camera field of view in degrees")
    ap.add_argument("--elevation", type=float, default=35.0, help="Camera elevation angle above horizontal, degrees")
    ap.add_argument("--format", choices=["placer", "room_report"], default="placer", help="Output JSON schema: placer (native) or room_report (pipeline-compatible)")
    ap.add_argument("--out", default="placement.json", help="Output JSON file")
    ap.add_argument("--debug-glb", default=None, help="Optional path to export an annotated debug .glb")
    args = ap.parse_args()

    # validation
    if args.characters < 1 or args.characters > 12:
        print("ERROR: --characters must be 1..12", file=sys.stderr)
        sys.exit(2)
    if args.resolution < 0.01 or args.resolution > 0.5:
        print("ERROR: --resolution must be 0.01..0.5", file=sys.stderr)
        sys.exit(2)
    if args.human_radius < 0.15 or args.human_radius > 0.6:
        print("ERROR: --human-radius must be 0.15..0.6", file=sys.stderr)
        sys.exit(2)
    if not Path(args.model).exists():
        print(f"ERROR: model not found: {args.model}", file=sys.stderr)
        sys.exit(2)

    # auto-detect up
    up_axis = args.up_axis
    if up_axis == "auto":
        up_axis = detect_up_axis_auto(args.model)
        print(f"[placer] auto up-axis -> {up_axis}", file=sys.stderr)

    _, _, perm, _ = axis_permutation(up_axis)

    boxes = load_world_boxes(args.model, perm)
    floor_height, room_bounds, obstacles = detect_floor_and_room_bounds(boxes)
    print(f"[placer] floor_height={floor_height:.3f} bounds={tuple(round(v,2) for v in room_bounds)} obstacles={len(obstacles)} floor_candidates={len(boxes)-len(obstacles)}", file=sys.stderr)

    xs, zs, occ = build_occupancy_grid(
        room_bounds, obstacles, floor_height, args.character_height, args.resolution
    )
    free_cells = int(np.sum(~occ))
    walkable_ratio = free_cells / max(occ.size, 1)
    print(f"[placer] grid {len(xs)}x{len(zs)} free={free_cells}/{occ.size} ({walkable_ratio:.1%}) res={args.resolution}", file=sys.stderr)
    if walkable_ratio < 0.02:
        print(f"[placer] WARNING: only {walkable_ratio:.1%} free — room may be too cluttered or floor detection wrong. Try --up-axis or --human-radius smaller.", file=sys.stderr)

    # EDT for camera clamp as well
    free = ~occ
    dist_field = distance_transform_edt(free, sampling=[args.resolution, args.resolution])

    center, radius, positions = find_group_spot(
        args.characters, xs, zs, occ,
        args.human_radius, args.spacing, args.min_spacing, args.resolution
    )

    if positions is None:
        print("ERROR: could not find a collision-free spot for that many characters. "
              "Try --human-radius or --spacing smaller, or reduce --characters, or check --up-axis.", file=sys.stderr)
        sys.exit(1)

    print(f"[placer] conversation center={tuple(round(v,2) for v in center)} radius={radius:.2f}m", file=sys.stderr)

    spots = []
    for i, (x, z, ang) in enumerate(positions):
        yaw_deg = float(np.degrees((ang + np.pi) % (2 * np.pi))) if args.characters > 1 else 0.0
        pts_int = np.array([[x, floor_height, z]])
        pts_world = internal_to_world_pts(pts_int, perm)[0]
        wx, wy, wz = pts_world.tolist()
        world_pos = {"x": round(float(wx), 3), "y": round(float(wy), 3), "z": round(float(wz), 3)}
        blender_pos = to_blender_zup(world_pos, up_axis)
        spots.append(CharacterSpot(
            index=i,
            position=world_pos,
            yaw_deg=round(yaw_deg, 1),
            footprint_radius=args.human_radius,
            blender_position=blender_pos,
            blender_yaw_deg=yaw_to_blender(yaw_deg, up_axis),
        ))

    positions_xz = [(p[0], p[1]) for p in positions]
    group_cam = compute_group_camera(
        positions_xz, floor_height, args.character_height,
        fov_deg=args.fov, view_angle_deg=args.elevation,
        xs=xs, zs=zs, dist_field=dist_field, resolution=args.resolution
    )
    # convert camera internal -> world for output
    gcx, gcup, gcz = group_cam.position["x"], group_cam.position["up"], group_cam.position["z"]
    lx, lup, lz = group_cam.look_at["x"], group_cam.look_at["up"], group_cam.look_at["z"]
    g_world = internal_to_world_pts(np.array([[gcx, gcup, gcz]]), perm)[0]
    l_world = internal_to_world_pts(np.array([[lx, lup, lz]]), perm)[0]
    group_cam_world = CameraShot(
        name=group_cam.name,
        position={"x": round(float(g_world[0]), 3), "y": round(float(g_world[1]), 3), "z": round(float(g_world[2]), 3)},
        look_at={"x": round(float(l_world[0]), 3), "y": round(float(l_world[1]), 3), "z": round(float(l_world[2]), 3)},
        fov_deg=group_cam.fov_deg,
        distance=group_cam.distance,
    )
    group_cam_blender = CameraShot(
        name=group_cam.name,
        position=to_blender_zup(group_cam_world.position, up_axis),
        look_at=to_blender_zup(group_cam_world.look_at, up_axis),
        fov_deg=group_cam.fov_deg,
        distance=group_cam.distance,
    )

    closeups = []
    closeups_blender = []
    for i, (x, z, ang) in enumerate(positions):
        yaw_deg = float(np.degrees((ang + np.pi) % (2 * np.pi))) if args.characters > 1 else 0.0
        cc = compute_closeup_camera((x, z), yaw_deg, floor_height, args.character_height)
        ccx, ccup, ccz = cc.position["x"], cc.position["up"], cc.position["z"]
        clx, clup, clz = cc.look_at["x"], cc.look_at["up"], cc.look_at["z"]
        wcc = internal_to_world_pts(np.array([[ccx, ccup, ccz]]), perm)[0]
        wcl = internal_to_world_pts(np.array([[clx, clup, clz]]), perm)[0]
        pos_w = {"x": round(float(wcc[0]), 3), "y": round(float(wcc[1]), 3), "z": round(float(wcc[2]), 3)}
        look_w = {"x": round(float(wcl[0]), 3), "y": round(float(wcl[1]), 3), "z": round(float(wcl[2]), 3)}
        closeups.append({
            "character_index": i,
            "position": pos_w,
            "look_at": look_w,
            "fov_deg": cc.fov_deg,
            "distance": cc.distance,
        })
        closeups_blender.append({
            "character_index": i,
            "position": to_blender_zup(pos_w, up_axis),
            "look_at": to_blender_zup(look_w, up_axis),
            "fov_deg": cc.fov_deg,
            "distance": cc.distance,
        })

    # Build output
    if args.format == "room_report":
        # pipeline-compatible shape (mirrors room_analyzer room_report.json)
        spots_rr = []
        for s in spots:
            spots_rr.append({
                "x": s.blender_position["x"] if s.blender_position else s.position["x"],
                "y": s.blender_position["y"] if s.blender_position else s.position["y"],
                "z": s.blender_position["z"] if s.blender_position else s.position["z"],
                "yaw_deg": s.blender_yaw_deg if s.blender_yaw_deg is not None else s.yaw_deg,
                "clearance": round(float(radius), 2),
                "region_area": round(math.pi * args.human_radius ** 2, 2),
                "facing": {"x": round(math.cos(math.radians(s.yaw_deg)), 3), "y": round(math.sin(math.radians(s.yaw_deg)), 3)},
            })
        output = {
            "label": Path(args.model).stem,
            "source": str(args.model),
            "floor_height": round(float(floor_height), 3),
            "up_axis": up_axis,
            "walkable_cells": int(free_cells),
            "walkable_ratio": round(float(walkable_ratio), 3),
            "spots": spots_rr,
            "cameras": {
                "group_establishing_shot": asdict(group_cam_blender),
                "closeups": closeups_blender,
            },
            "_placer_raw": {
                "conversation_circle_radius_m": radius,
                "characters": [asdict(s) for s in spots],
                "asset_world_cameras": {
                    "group_establishing_shot": asdict(group_cam_world),
                    "closeups": closeups,
                },
            },
        }
    else:
        output = {
            "model": args.model,
            "up_axis": up_axis,
            "floor_height": round(float(floor_height), 3),
            "num_characters": args.characters,
            "conversation_circle_radius_m": radius,
            "characters": [asdict(s) for s in spots],
            "cameras": {
                "group_establishing_shot": asdict(group_cam_world),
                "closeups": closeups,
            },
            "blender": {
                "up_axis": "z",
                "characters": [{"index": s.index, "position": s.blender_position, "yaw_deg": s.blender_yaw_deg} for s in spots],
                "group_establishing_shot": asdict(group_cam_blender),
                "closeups": closeups_blender,
            },
            "grid": {
                "resolution": args.resolution,
                "free_cells": int(free_cells),
                "walkable_ratio": round(float(walkable_ratio), 3),
            },
        }

    with open(args.out, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {args.out} ({args.format})", file=sys.stderr)

    if args.debug_glb:
        export_debug_glb(
            args.model, args.debug_glb, perm, up_axis, floor_height,
            spots, group_cam, args.character_height, args.human_radius
        )
        print(f"Wrote debug preview {args.debug_glb}", file=sys.stderr)


if __name__ == "__main__":
    main()
