# approximate X500-style assembly; part placement is illustrative, not declared in the contract
"""Headless fixture authoring; derived from the outbox realistic/build_model.py.

Metres, Blender Z-up, front -Y and right +X. Origin is the horizontal robot
centre on the skid contact plane. This script is never imported by src/.
"""

import math
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parent
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.unit_settings.system = "METRIC"
scene.unit_settings.scale_length = 1.0


def material(name, color, metal=0.0, rough=0.35):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    bs = mat.node_tree.nodes.get("Principled BSDF")
    bs.inputs["Base Color"].default_value = (*color, 1)
    bs.inputs["Metallic"].default_value = metal
    bs.inputs["Roughness"].default_value = rough
    return mat


carbon = material("matte carbon fiber", (0.025, 0.035, 0.045), 0.45, 0.28)
alum = material("anodized aluminum", (0.22, 0.27, 0.3), 0.8, 0.24)
black = material("motor black", (0.018, 0.021, 0.024), 0.65, 0.22)
blue = material("Pixhawk blue", (0.035, 0.16, 0.29), 0.45, 0.3)
orange = material("PM02 orange", (0.8, 0.19, 0.035), 0.25, 0.3)
pcb = material("Jetson carrier", (0.025, 0.23, 0.09), 0.25, 0.3)
copper = material("copper power wire", (0.55, 0.09, 0.04), 0.7, 0.2)
signal = material("dark grey signal wire", (0.12, 0.14, 0.16), 0.1, 0.5)
prop = material("propeller composite", (0.08, 0.12, 0.14), 0.25, 0.3)
prop.use_backface_culling = False
rubber = material("skid rubber and heat shrink", (0.03, 0.035, 0.038), 0.05, 0.5)
glass = material("camera glass", (0.015, 0.09, 0.14), 0.7, 0.08)
silver = material("screw heads", (0.55, 0.6, 0.62), 0.9, 0.17)
white = material("GNSS white", (0.88, 0.9, 0.92), 0.05, 0.4)


def part(contract_id, centre):
    empty = bpy.data.objects.new(contract_id, None)
    scene.collection.objects.link(empty)
    empty.location = centre
    empty["lockwire"] = {"id": contract_id}
    return empty


def parent(obj, owner):
    # All mesh coordinates are authored in world space. Keep that placement,
    # while making the owner's local translation the actual part centre.
    obj.parent = owner
    obj.location -= owner.location
    return obj


def cube(name, loc, scale, mat, owner, bevel=0.001, rot=None):
    bpy.ops.mesh.primitive_cube_add(location=loc)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale  # half extents, inherited from the source builder
    if rot:
        obj.rotation_euler = rot
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    if bevel:
        modifier = obj.modifiers.new("machined edge", "BEVEL")
        modifier.width = bevel
        modifier.segments = 1
    return parent(obj, owner)


def cyl(name, loc, radius, depth, mat, owner, verts=16, rot=None):
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=verts, radius=radius, depth=depth, location=loc, rotation=rot or (0, 0, 0)
    )
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(mat)
    return parent(obj, owner)


def rod(name, a, b, radius, mat, owner):
    delta = Vector(b) - Vector(a)
    obj = cyl(name, (Vector(a) + Vector(b)) / 2, radius, delta.length, mat, owner, verts=8)
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = delta.to_track_quat("Z", "Y")
    return obj


def screw(name, loc, owner, radius=0.002):
    return cyl(name, loc, radius, 0.0015, silver, owner, verts=8)


frame = part("x500_frame", (0, 0, 0))
cube("frame_top_plate", (0, 0, 0.17), (0.055, 0.073, 0.002), carbon, frame, 0.004)
cube("frame_lower_plate", (0, 0, 0.112), (0.053, 0.068, 0.002), carbon, frame, 0.004)
cube("frame_rear_tray", (0, 0.098, 0.108), (0.055, 0.035, 0.002), carbon, frame)
for x in (-0.045, 0.045):
    for y in (-0.057, 0.057):
        rod("frame_standoff", (x, y, 0.114), (x, y, 0.168), 0.0025, silver, frame)
        screw("frame_screw", (x, y, 0.173), frame)

# PX4 quad X: m1 front-right, m2 rear-left, m3 front-left, m4 rear-right.
axis = 0.25 / math.sqrt(2)
corners = [(axis, -axis), (-axis, axis), (-axis, -axis), (axis, axis)]
for i, (x, y) in enumerate(corners, 1):
    rod(f"frame_arm_{i}", (0, 0, 0.145), (x, y, 0.145), 0.008, carbon, frame)
    cube(
        f"frame_arm_clamp_{i}",
        (x * 0.23, y * 0.23, 0.145),
        (0.009, 0.012, 0.01),
        alum,
        frame,
        rot=(0, 0, math.atan2(y, x) - math.pi / 2),
    )

# Splayed legs and longitudinal skids, with bottom surfaces on Z=0.
for i, x in enumerate((-0.052, 0.052), 1):
    for j, y in enumerate((-0.045, 0.045), 1):
        rod(f"frame_landing_leg_{i}_{j}", (x, y, 0.11), (x * 1.5, y, 0.006), 0.004, carbon, frame)
    rod(
        f"frame_landing_skid_{i}",
        (x * 1.5, -0.1, 0.005),
        (x * 1.5, 0.1, 0.005),
        0.005,
        rubber,
        frame,
    )


def propeller(i, x, y, spin):
    # Source's swept, tapered opposed blades, now local to their own hub.
    # Each tip ends at radius 0.127 m: the pair spans exactly 10 inches.
    verts = []
    faces = []
    segments = 8
    for side in (1, -1):
        for j in range(segments + 1):
            t = j / segments
            radius = 0.009 + t * 0.118
            sweep = 0.10 + t * 0.34
            width = (1 - t) * 0.010
            z = 0.002 * math.sin(t * math.pi)
            for sign in (1, -1):
                verts.append(
                    (
                        side * (radius * math.cos(sweep) - sign * width * math.sin(sweep)),
                        side * (radius * math.sin(sweep) + sign * width * math.cos(sweep)),
                        z + sign * 0.0006 * (1 - t),
                    )
                )
        offset = 0 if side == 1 else 2 * (segments + 1)
        for j in range(segments):
            a = offset + 2 * j
            if j == segments - 1:
                faces.append((a, a + 2, a + 1))
            else:
                faces.append((a, a + 2, a + 3, a + 1))
    mesh = bpy.data.meshes.new(f"propeller_mesh_{i}")
    mesh.from_pydata(verts, [], faces)
    mesh.materials.append(prop)
    obj = bpy.data.objects.new(f"propeller_{i}", mesh)
    scene.collection.objects.link(obj)
    obj.location = (x, y, 0.188)
    obj.rotation_euler[2] = spin
    return parent(obj, frame)


esc_positions = []
for i, (x, y) in enumerate(corners, 1):
    motor = part(f"dev.motor.m{i}", (x, y, 0.168))
    cyl(f"motor_{i}_base", (x, y, 0.156), 0.014, 0.008, black, motor, verts=24)
    cyl(f"motor_{i}_bell", (x, y, 0.17), 0.014, 0.02, alum, motor, verts=24)
    cyl(f"motor_{i}_hub", (x, y, 0.188), 0.005, 0.016, silver, motor)
    for k in range(4):
        angle = 2 * math.pi * k / 4 + math.pi / 4
        screw(
            f"motor_{i}_mount_{k}",
            (x + 0.01 * math.cos(angle), y + 0.01 * math.sin(angle), 0.152),
            motor,
        )
    propeller(i, x, y, (-1 if i % 2 else 1) * 0.12)
    ex, ey = x * 0.78, y * 0.78
    esc_positions.append((ex, ey, 0.13))
    esc = part(f"dev.esc.esc{i}", (ex, ey, 0.13))
    rotation = (0, 0, math.atan2(y, x) - math.pi / 2)
    cube(f"esc_{i}_board", (ex, ey, 0.13), (0.01, 0.018, 0.003), rubber, esc, rot=rotation)
    cube(f"esc_{i}_strap", (ex, ey, 0.134), (0.011, 0.003, 0.001), black, frame, rot=rotation)
    for lead in (-1, 0, 1):
        offset = Vector((-y, x, 0)).normalized() * (lead * 0.003)
        rod(
            f"esc_{i}_motor_lead_{lead + 2}",
            Vector((ex, ey, 0.13)) + offset,
            Vector((x, y, 0.154)) + offset,
            0.0009,
            copper,
            frame,
        )

# Electronics retain the source's recognisable boards, fan and stereo lenses.
fc = part("dev.flight-controller.pixhawk-6c", (0, 0, 0.193))
cube("pixhawk", (0, 0, 0.193), (0.032, 0.025, 0.012), blue, fc, 0.003)
cube("pixhawk_top", (0, 0, 0.206), (0.025, 0.018, 0.001), blue, fc)
for x in (-0.025, 0.025):
    for y in (-0.018, 0.018):
        rod("pixhawk_standoff", (x, y, 0.172), (x, y, 0.181), 0.002, silver, frame)
        screw("pixhawk_screw", (x, y, 0.2075), frame, 0.0015)

pm = part("dev.power-module.pm02-v3", (0, 0.047, 0.159))
cube("pm02", pm.location, (0.02, 0.012, 0.003), orange, pm)
cube("pm02_regulator", (0, 0.047, 0.164), (0.008, 0.008, 0.002), black, pm)
for x in (-0.015, 0.015):
    screw("pm02_screw", (x, 0.047, 0.163), frame, 0.0015)

jet = part("dev.companion.jetson-orin-nano", (0, 0.096, 0.13))
cube("jetson_carrier", (0, 0.096, 0.113), (0.045, 0.027, 0.002), pcb, jet)
cube("jetson_module", (0, 0.096, 0.119), (0.035, 0.022, 0.004), pcb, jet)
for x in (-0.025, -0.015, -0.005, 0.005, 0.015, 0.025):
    cube("jetson_heatsink_fin", (x, 0.096, 0.132), (0.0015, 0.019, 0.009), alum, jet, 0.0003)
cyl("jetson_fan", (0, 0.096, 0.143), 0.015, 0.003, black, jet, verts=24)
for i in range(5):
    angle = i * 2 * math.pi / 5
    cube(
        "jetson_fan_blade",
        (0.008 * math.cos(angle), 0.096 + 0.008 * math.sin(angle), 0.145),
        (0.0012, 0.0055, 0.0006),
        silver,
        jet,
        0.0003,
        rot=(0, 0, angle),
    )

cam = part("dev.camera.oak-d-lite", (0, -0.089, 0.132))
cube("camera", cam.location, (0.045, 0.009, 0.014), black, cam, 0.003)
for x in (-0.03, 0.03):
    cyl(
        "camera_lens_ring",
        (x, -0.1, 0.132),
        0.009,
        0.003,
        alum,
        cam,
        verts=24,
        rot=(math.pi / 2, 0, 0),
    )
    cyl(
        "camera_lens",
        (x, -0.102, 0.132),
        0.007,
        0.002,
        glass,
        cam,
        verts=24,
        rot=(math.pi / 2, 0, 0),
    )

radio = part("dev.radio.rfd900x", (0, 0.108, 0.188))
cube("rfd900x_box", radio.location, (0.015, 0.0285, 0.006), black, radio, 0.0015)
rod("rfd900x_antenna", (0, 0.1365, 0.188), (0, 0.159, 0.221), 0.002, rubber, radio)
rod("radio_tray_standoff", (0, 0.108, 0.15), (0, 0.108, 0.18), 0.003, silver, frame)

rod("gnss_mast", (0, 0.035, 0.172), (0, 0.035, 0.28), 0.003, alum, frame)
gnss = part("dev.gnss.here3-plus", (0, 0.035, 0.289))
cyl("gnss_puck_base", (0, 0.035, 0.282), 0.026, 0.004, black, gnss, verts=24)
cyl("gnss_puck", (0, 0.035, 0.291), 0.027, 0.014, white, gnss, verts=32)
mag = part("dev.magnetometer.ist8310", (0.014, 0.027, 0.277))
cube("magnetometer_chip", mag.location, (0.005, 0.005, 0.0015), black, mag, 0.0003)

cube("battery", (0, -0.005, 0.067), (0.032, 0.052, 0.023), rubber, frame, 0.005)
cube("battery_label", (0, -0.058, 0.067), (0.022, 0.0005, 0.015), silver, frame, 0)
for y in (-0.035, 0.025):
    cube("battery_strap", (0, y, 0.093), (0.035, 0.004, 0.002), black, frame)
    for x in (-0.034, 0.034):
        cube("battery_strap_side", (x, y, 0.067), (0.002, 0.004, 0.025), black, frame)


def wire(contract_id, paths, radius, mat):
    # A distributed part's centre is its length-weighted centreline midpoint.
    segments = [(Vector(a), Vector(b)) for path in paths for a, b in zip(path, path[1:])]
    length = sum((b - a).length for a, b in segments)
    centre = sum(((a + b) / 2 * (b - a).length for a, b in segments), Vector()) / length
    owner = part(contract_id, centre)
    for i, (a, b) in enumerate(segments, 1):
        rod(f"{contract_id}_wire_{i}", a, b, radius, mat, owner)


wire(
    "rail.pm02-5v",
    [
        [(0.012, 0.047, 0.159), (0.05, 0.055, 0.13), (0.043, 0.096, 0.114)],
        [
            (-0.012, 0.047, 0.159),
            (-0.05, 0.04, 0.13),
            (-0.05, -0.075, 0.13),
            (-0.035, -0.089, 0.132),
        ],
    ],
    0.001,
    copper,
)
wire(
    "rail.pixhawk-telem1-5v",
    [[(0.015, 0.025, 0.193), (0.015, 0.078, 0.188), (0.01, 0.083, 0.188)]],
    0.001,
    copper,
)
wire("rail.servo-motor", [[(0, 0.047, 0.155), pos] for pos in esc_positions], 0.0015, copper)
wire(
    "bus.uart.telemetry",
    [[(0.021, 0.025, 0.193), (0.021, 0.078, 0.188), (0.016, 0.083, 0.188)]],
    0.0006,
    signal,
)
wire(
    "bus.uart.companion",
    [[(-0.02, 0.025, 0.193), (-0.025, 0.061, 0.15), (-0.025, 0.074, 0.114)]],
    0.0006,
    signal,
)
wire(
    "bus.usb.companion",
    [
        [
            (-0.044, 0.096, 0.114),
            (-0.056, 0.04, 0.13),
            (-0.056, -0.075, 0.13),
            (-0.041, -0.089, 0.132),
        ]
    ],
    0.0006,
    signal,
)
wire(
    "bus.can.dronecan",
    [[(0.008, 0.025, 0.193), (0.004, 0.035, 0.215), (0.004, 0.035, 0.282)]],
    0.0006,
    signal,
)
wire(
    "bus.i2c.external",
    [
        [
            (-0.008, 0.025, 0.193),
            (-0.004, 0.035, 0.215),
            (-0.004, 0.035, 0.274),
            (0.014, 0.027, 0.277),
        ]
    ],
    0.0006,
    signal,
)

# Flat colours need no UVs. Bevel's generated UVs vary by a float ULP across
# Blender processes; disabling their export also makes this fixture smaller.
bpy.ops.export_scene.gltf(
    filepath=str(ROOT / "x500.glb"),
    export_format="GLB",
    export_extras=True,
    export_yup=True,
    export_apply=True,
    export_normals=True,
    export_texcoords=False,
    export_materials="EXPORT",
    export_animations=False,
    export_cameras=False,
    export_lights=False,
    export_skins=False,
    export_morph=False,
    use_selection=False,
)
