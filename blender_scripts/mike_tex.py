"""Probe: material node graph + texture image pixel content."""

import sys
from collections import Counter

import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=args[0])
    for mat in bpy.data.materials:
        print(f"[t] material {mat.name!r} node_tree={mat.node_tree is not None}", flush=True)
        if mat.node_tree is None:
            continue
        for node in mat.node_tree.nodes:
            if node.type in {"TEX_IMAGE", "BSDF_PRINCIPLED", "MAPPING"}:
                links = []
                for out in node.outputs:
                    for l in out.links:
                        to = getattr(l, "to_node", None) or getattr(l, "target_node", None)
                        to_s = getattr(l, "to_socket", None) or getattr(l, "target_socket", None)
                        links.append(f"{node.name}.{out.name}->{to.name}.{to_s.name}")
                print(f"[t]   node {node.name} ({node.type}) inputs_ok; links: {links or 'NONE'}", flush=True)
        print(f"[t]   base_color={tuple(mat.diffuse_color[:3])} use_nodes={mat.use_nodes}", flush=True)
    for img in bpy.data.images:
        if img.name.startswith("MikeAlger"):
            print(f"[t] image {img.name} size={img.size} filepath={img.filepath!r} loaded={img.pixels is not None}", flush=True)
            w, h = img.size
            px = list(img.pixels[: w * h * 4])
            cols = Counter()
            for i in range(0, len(px), 4):
                r, g, b = px[i], px[i + 1], px[i + 2]
                cols[tuple(int(c * 32) // 8 * 8 for c in (r, g, b))] += 1
            print("[t] top texture colors:", cols.most_common(8), flush=True)
    return 0


sys.exit(main())
