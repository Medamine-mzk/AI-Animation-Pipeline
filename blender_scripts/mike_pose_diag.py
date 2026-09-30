import sys
from pathlib import Path
from mathutils import Matrix, Vector
import bpy

sys.path.insert(0, str(Path(__file__).parent))
import spike_render as sr

fbx = r"E:\PROJECTS\AI Animation Pipeline\media\3d\mike\source\MikeAlger.fbx"
arm = None
def armature():
    global arm
    arm = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
    return arm

def clear_all():
    for pb in armature().pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()

def eval_hand(bn="mixamorig_LeftHand"):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = armature().evaluated_get(dg)
    p = ev.pose.bones[bn]
    return armature().matrix_world @ p.head, p.matrix.to_3x3() @ Vector((0,1,0))

def shortest_arc(pb, d_arm_armature):
    r3 = pb.bone.matrix_local.to_3x3()
    d_local = r3.inverted() @ d_arm_armature
    d_local.normalize()
    rest_y = Vector((0.0, 1.0, 0.0))
    axis_local = rest_y.cross(d_local)
    if axis_local.length < 1e-6:
        axis_local = Vector((1.0, 0.0, 0.0))
    else:
        axis_local.normalize()
    rot = Matrix.Rotation(rest_y.angle(d_local), 4, axis_local)
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = rot.to_euler("XYZ")
    return tuple(round(a, 2) for a in pb.rotation_euler)

def analytic(pb, awm, target_world):
    sh = awm @ pb.bone.head_local
    d_world = (target_world - sh).normalized()
    d_arm = (awm.inverted() @ d_world).normalized()
    palm_arm = (awm.inverted() @ Vector((0.0, -1.0, 0.0))).normalized()
    r3 = pb.bone.matrix_local.to_3x3()
    d_local = r3.inverted() @ d_arm
    d_local.normalize()
    z_local = r3.inverted() @ palm_arm
    z_local -= z_local.dot(d_local) * d_local
    z_local.normalize()
    x_local = d_local.cross(z_local)
    x_local.normalize()
    return Matrix((x_local, d_local, z_local)).transposed()

sr.import_fbx(fbx)
awm = armature().matrix_world
sh = awm @ armature().pose.bones["mixamorig_LeftArm"].bone.head_local
side = 1.0 if sh.x >= 0.0 else -1.0
target_world = Vector((sh.x + side * 0.12, sh.y - 0.30, sh.z - 0.50))
print("intended hand:", target_world, flush=True)

pb = armature().pose.bones["mixamorig_LeftArm"]
d_world = (target_world - sh).normalized()
d_arm = (awm.inverted() @ d_world).normalized()

# method 1: shortest arc euler (bake5-style)
clear_all()
e1 = shortest_arc(pb, d_arm)
h, d = eval_hand()
print(f"M1 shortest-arc euler={e1} -> hand={h} dir={d}", flush=True)

# method 2: analytic matrix -> matrix_basis direct
clear_all()
pose = analytic(pb, awm, target_world)
pb.matrix_basis = pose.to_4x4()
h, d = eval_hand()
print(f"M2 matrix_basis direct -> hand={h} dir={d} basis_euler={pb.rotation_euler}", flush=True)

# method 3: analytic -> quaternion
clear_all()
pose = analytic(pb, awm, target_world)
pb.rotation_mode = "QUATERNION"
pb.rotation_quaternion = pose.to_quaternion()
h, d = eval_hand()
print(f"M3 quaternion -> hand={h} dir={d}", flush=True)

# method 4: analytic -> euler
clear_all()
pose = analytic(pb, awm, target_world)
pb.rotation_mode = "XYZ"
pb.rotation_euler = pose.to_euler("XYZ")
h, d = eval_hand()
print(f"M4 analytic euler -> hand={h} dir={d}", flush=True)