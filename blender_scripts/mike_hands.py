"""Probe: vertex group size + vertex positions per right-arm group (deformed by rest pose)."""

import sys

import bpy


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.import_scene.fbx(filepath=args[0])
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        groups = {g.name: [] for g in obj.vertex_groups}
        for v in obj.data.vertices:
            for g in v.groups:
                if g.weight > 0.01:
                    groups.setdefault(obj.vertex_groups[g.group].name, []).append(v.index)
        for name in sorted(groups):
            if "hand" not in name.lower() and "forearm" not in name.lower():
                continue
            idx = groups[name]
            if not idx:
                continue
            coords = [obj.data.vertices[i].co for i in idx]
            xs = [c.x for c in coords]; ys = [c.y for c in coords]; zs = [c.z for c in coords]
            print(
                f"[g] {name}: {len(idx)} verts "
                f"x[{min(xs):.3f},{max(xs):.3f}] y[{min(ys):.3f},{max(ys):.3f}] z[{min(zs):.3f},{max(zs):.3f}]",
                flush=True,
            )
    return 0


sys.exit(main())