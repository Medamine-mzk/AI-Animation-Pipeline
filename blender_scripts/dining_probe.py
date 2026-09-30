import bpy, sys, numpy as np
from mathutils import Vector
from pathlib import Path

path = Path("assets/background/dining_room.glb")
bpy.ops.import_scene.gltf(filepath=str(path))
for o in list(bpy.data.objects):
    if o.name in ("Cube","Light","Camera"):
        bpy.data.objects.remove(o, do_unlink=True)
bpy.context.view_layer.update()
for o in list(bpy.data.objects):
    if o.type!="MESH":
        bpy.data.objects.remove(o, do_unlink=True)
bpy.context.view_layer.update()
meshes=[o for o in bpy.data.objects if o.type=="MESH"]
for o in meshes: o.select_set(True)
bpy.context.view_layer.objects.active=meshes[0]
bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
bpy.context.view_layer.update()
if len(meshes)>1: bpy.ops.object.join()
room=bpy.context.view_layer.objects.active
room.data.calc_loop_triangles()
mw=room.matrix_world
m3=mw.to_3x3()
# stats
import math
for v in room.data.vertices[:3]:
    print("v", (mw@v.co).to_tuple())
# check normals
nz=[]
ny=[]
nx=[]
for t in room.data.loop_triangles[:2000]:
    n=(m3@t.normal).normalized()
    nz.append(n.z); ny.append(n.y); nx.append(n.x)
nz=np.array(nz); ny=np.array(ny); nx=np.array(nx)
print(f"nz mean {nz.mean():.3f} std {nz.std():.3f} max {nz.max():.3f} min {nz.min():.3f} | nz>0.9 {(nz>0.9).sum()} nz<-0.9 {(nz<-0.9).sum()}")
print(f"ny mean {ny.mean():.3f} std {ny.std():.3f} | ny>0.9 {(ny>0.9).sum()} ny<-0.9 {(ny<-0.9).sum()}")
print(f"nx mean {nx.mean():.3f} | nx>0.9 {(nx>0.9).sum()}")
# bounds
xs,ys,zs=[],[],[]
for v in room.data.vertices:
    p=mw@v.co
    xs.append(p.x); ys.append(p.y); zs.append(p.z)
print(f"bounds x[{min(xs):.2f},{max(xs):.2f}] y[{min(ys):.2f},{max(ys):.2f}] z[{min(zs):.2f},{max(zs):.2f}]")
# also try to see which axis has most horizontal area
from collections import Counter
import numpy as np
# quick area by normal
area=np.array([t.area for t in room.data.loop_triangles])
# up area by nz etc
print(f"areas by normal: z-up {(np.abs(nz)>0.9).sum()} y-up {(np.abs(ny)>0.9).sum()}")
