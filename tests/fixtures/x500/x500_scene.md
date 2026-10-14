# X500 demo scene provenance

approximate X500-style assembly; part placement is illustrative, not declared in the contract

**Read:** the project's scene rules require a glTF sidecar whose meaning
lives in `extras.lockwire.id`; `contract.yaml` remains the truth. All 24 IDs below
were read from that contract. No contract or application source file was changed.

**Read:** the build began by copying this read-only source into
`x500_scene_build.py`, then modifying the copy:
an earlier `build_model.py` for the PX4 sample.
**Measured:** the copied source's SHA-256 before modification was
`e4ef63cbf47d724a324203edeead7c77b9f4fef7006ee6e664f267dfe3fba3f6`.
The cube/cylinder/rod material helpers, swept opposed propellers, laminated plates,
landing gear, electronics stack, finned heatsink/fan, camera lenses and battery
were adapted from it. The arbitrary source scale, world-origin propeller rotation,
studio objects and bespoke JSON export were replaced for this fixture.

**Measured:** tool `/opt/homebrew/bin/blender --version` reports Blender **5.2.0 LTS**,
build hash `fbe6228777e7`, build date `2026-07-14`, build time `01:31:22`, Darwin Release.

## Reproduce

**Measured:** run from the worktree root, foreground and headless:

```sh
/opt/homebrew/bin/blender --background --factory-startup --python tests/fixtures/x500/x500_scene_build.py
python3 tests/fixtures/x500/x500_scene_inspect.py tests/fixtures/x500/x500.glb
shasum -a 256 tests/fixtures/x500/x500.glb
```

The build uses the following working exporter call (**read** from the script;
**measured** successful in Blender 5.2.0). `ROOT` is the build script's directory.
All specified arguments were accepted. One additional option disables unused UVs:

```python
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
```

**Measured:** the original UV-enabled export had eight varying binary bytes
across processes, confined to `TEXCOORD_0` on two beveled cubes; JSON, position,
normal and index data matched. The installed exporter's
`io_scene_gltf2/__init__.py` defines `export_texcoords` as the UV export toggle.
With `export_texcoords=False`, the final two fresh Blender processes produced
identical bytes (`cmp` exit 0) and the following `shasum -a 256` output:

```text
c388e098b2ca377ddee5cb4ca4638d635669ea2e4d658aeea4843ef0e1a5aa2c  /private/tmp/x500-t7-final-build-one.glb
c388e098b2ca377ddee5cb4ca4638d635669ea2e4d658aeea4843ef0e1a5aa2c  tests/fixtures/x500/x500.glb
```

**Measured:** 367,776 bytes; 10,832 exported vertices; 5,928 triangles; 148 mesh nodes; 172 total nodes; 24 ID parents; no URI keys.
Vertex totals sum POSITION accessor counts per mesh-node primitive, including
normal splits; triangle totals sum index counts / 3. These are exported render
counts, not unique spatial point counts. All mesh primitives have normals and
flat baseColorFactor materials; no textures or external buffers are present.

## Geometry and honesty

**Assumed (illustrative authoring choices):** Blender uses metres, +Z up, front −Y,
right +X. The robot origin is its horizontal centre on the skid contact plane,
not a claimed centre of mass. Export uses standard glTF +Y up: Blender (x,y,z)
becomes glTF (x,z,−y). No device mount_xyz is asserted in the YAML. All assembly
positions, wire routes and unsourced dimensions are illustrative. Motor order is
PX4 quad X: m1 front-right, m2 rear-left, m3 front-left, m4 rear-right.

**Read from the spec:** the vendor wheelbase target is 500 mm and the propeller target is
10 inches; these vendor statements were not fetched again because network access
was prohibited. The magnetometer's 3 mm chip is deliberately enlarged to 10 mm.

**Measured:** a temporary headless Blender inspection of the same geometry gave:

```text
DIMENSIONS frame_top_plate (0.11, 0.146, 0.004)
DIMENSIONS rfd900x_box (0.03, 0.057, 0.012)
DIMENSIONS magnetometer_chip (0.01, 0.01, 0.003)
PROP_DIAMETER 1 0.2540000128016811
PROP_DIAMETER 2 0.25399998988527933
PROP_DIAMETER 3 0.2540000128016811
PROP_DIAMETER 4 0.25399998988527933
MIN_Z -1.3901547468719855e-08
MOTOR_WHEELBASE 0.4999999850988386
```

**Measured:** a CPU Cycles render was generated to a temporary PNG and inspected
without opening a window. Four swept two-blade propellers, X arms, skids, blue FC,
front stereo lenses, rear radio and white GNSS puck were visible. The Jetson stack
and PM02 are partly occluded by the plates from the front, consistent with their
under-plate/rear-tray placement. The preview camera and lights are temporary and
not in the exported GLB. This is visual fixture QA, not a claim of physical fidelity.

**Read from the build:** each ID Empty is a scene root at the part's authored
centre, with identity rotation and scale; its children use local coordinates to
preserve world placement. Wire parents use the length-weighted centre of their
rod centreline segments. Rails use 2 mm diameter copper rods (battery rail 3 mm);
buses use 1.2 mm dark-grey rods. The telemetry power/data routes are separated
by 6 mm in X; the shared camera power/USB run is also separated by 6 mm. Each ESC
has three short copper motor leads owned by the frame, and each propeller is frame
structure. Screws, mounting standoffs, straps and the GNSS mast are also structure.

## ID table

**Measured:** node name → contract ID → exact descendant mesh-node names.
Repeated physical parts have Blender's deterministic numeric suffixes.

| glTF parent node name | Contract ID | Mesh nodes |
|---|---|---|
| `bus.can.dronecan` | `bus.can.dronecan` | `bus.can.dronecan_wire_1`, `bus.can.dronecan_wire_2` |
| `bus.i2c.external` | `bus.i2c.external` | `bus.i2c.external_wire_1`, `bus.i2c.external_wire_2`, `bus.i2c.external_wire_3` |
| `bus.uart.companion` | `bus.uart.companion` | `bus.uart.companion_wire_1`, `bus.uart.companion_wire_2` |
| `bus.uart.telemetry` | `bus.uart.telemetry` | `bus.uart.telemetry_wire_1`, `bus.uart.telemetry_wire_2` |
| `bus.usb.companion` | `bus.usb.companion` | `bus.usb.companion_wire_1`, `bus.usb.companion_wire_2`, `bus.usb.companion_wire_3` |
| `dev.camera.oak-d-lite` | `dev.camera.oak-d-lite` | `camera`, `camera_lens`, `camera_lens.001`, `camera_lens_ring`, `camera_lens_ring.001` |
| `dev.companion.jetson-orin-nano` | `dev.companion.jetson-orin-nano` | `jetson_carrier`, `jetson_fan`, `jetson_fan_blade`, `jetson_fan_blade.001`, `jetson_fan_blade.002`, `jetson_fan_blade.003`, `jetson_fan_blade.004`, `jetson_heatsink_fin`, `jetson_heatsink_fin.001`, `jetson_heatsink_fin.002`, `jetson_heatsink_fin.003`, `jetson_heatsink_fin.004`, `jetson_heatsink_fin.005`, `jetson_module` |
| `dev.esc.esc1` | `dev.esc.esc1` | `esc_1_board` |
| `dev.esc.esc2` | `dev.esc.esc2` | `esc_2_board` |
| `dev.esc.esc3` | `dev.esc.esc3` | `esc_3_board` |
| `dev.esc.esc4` | `dev.esc.esc4` | `esc_4_board` |
| `dev.flight-controller.pixhawk-6c` | `dev.flight-controller.pixhawk-6c` | `pixhawk`, `pixhawk_top` |
| `dev.gnss.here3-plus` | `dev.gnss.here3-plus` | `gnss_puck`, `gnss_puck_base` |
| `dev.magnetometer.ist8310` | `dev.magnetometer.ist8310` | `magnetometer_chip` |
| `dev.motor.m1` | `dev.motor.m1` | `motor_1_base`, `motor_1_bell`, `motor_1_hub`, `motor_1_mount_0`, `motor_1_mount_1`, `motor_1_mount_2`, `motor_1_mount_3` |
| `dev.motor.m2` | `dev.motor.m2` | `motor_2_base`, `motor_2_bell`, `motor_2_hub`, `motor_2_mount_0`, `motor_2_mount_1`, `motor_2_mount_2`, `motor_2_mount_3` |
| `dev.motor.m3` | `dev.motor.m3` | `motor_3_base`, `motor_3_bell`, `motor_3_hub`, `motor_3_mount_0`, `motor_3_mount_1`, `motor_3_mount_2`, `motor_3_mount_3` |
| `dev.motor.m4` | `dev.motor.m4` | `motor_4_base`, `motor_4_bell`, `motor_4_hub`, `motor_4_mount_0`, `motor_4_mount_1`, `motor_4_mount_2`, `motor_4_mount_3` |
| `dev.power-module.pm02-v3` | `dev.power-module.pm02-v3` | `pm02`, `pm02_regulator` |
| `dev.radio.rfd900x` | `dev.radio.rfd900x` | `rfd900x_antenna`, `rfd900x_box` |
| `rail.pixhawk-telem1-5v` | `rail.pixhawk-telem1-5v` | `rail.pixhawk-telem1-5v_wire_1`, `rail.pixhawk-telem1-5v_wire_2` |
| `rail.pm02-5v` | `rail.pm02-5v` | `rail.pm02-5v_wire_1`, `rail.pm02-5v_wire_2`, `rail.pm02-5v_wire_3`, `rail.pm02-5v_wire_4`, `rail.pm02-5v_wire_5` |
| `rail.servo-motor` | `rail.servo-motor` | `rail.servo-motor_wire_1`, `rail.servo-motor_wire_2`, `rail.servo-motor_wire_3`, `rail.servo-motor_wire_4` |
| `x500_frame` | `x500_frame` | `battery`, `battery_label`, `battery_strap`, `battery_strap.001`, `battery_strap_side`, `battery_strap_side.001`, `battery_strap_side.002`, `battery_strap_side.003`, `esc_1_motor_lead_1`, `esc_1_motor_lead_2`, `esc_1_motor_lead_3`, `esc_1_strap`, `esc_2_motor_lead_1`, `esc_2_motor_lead_2`, `esc_2_motor_lead_3`, `esc_2_strap`, `esc_3_motor_lead_1`, `esc_3_motor_lead_2`, `esc_3_motor_lead_3`, `esc_3_strap`, `esc_4_motor_lead_1`, `esc_4_motor_lead_2`, `esc_4_motor_lead_3`, `esc_4_strap`, `frame_arm_1`, `frame_arm_2`, `frame_arm_3`, `frame_arm_4`, `frame_arm_clamp_1`, `frame_arm_clamp_2`, `frame_arm_clamp_3`, `frame_arm_clamp_4`, `frame_landing_leg_1_1`, `frame_landing_leg_1_2`, `frame_landing_leg_2_1`, `frame_landing_leg_2_2`, `frame_landing_skid_1`, `frame_landing_skid_2`, `frame_lower_plate`, `frame_rear_tray`, `frame_screw`, `frame_screw.001`, `frame_screw.002`, `frame_screw.003`, `frame_standoff`, `frame_standoff.001`, `frame_standoff.002`, `frame_standoff.003`, `frame_top_plate`, `gnss_mast`, `pixhawk_screw`, `pixhawk_screw.001`, `pixhawk_screw.002`, `pixhawk_screw.003`, `pixhawk_standoff`, `pixhawk_standoff.001`, `pixhawk_standoff.002`, `pixhawk_standoff.003`, `pm02_screw`, `pm02_screw.001`, `propeller_1`, `propeller_2`, `propeller_3`, `propeller_4`, `radio_tray_standoff` |

## Checks and limitations

**Measured:** no local `.venv` existed. To respect no installs and no network,
uv used already-installed system tools with these environment settings; PYTHONPATH
selects this worktree's application source:

```sh
export UV_NO_PROJECT=1 UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never
export UV_CACHE_DIR=/private/tmp/lockwire-t7-uv-cache
export PYTHONPATH="$PWD/src"
uv run ruff check . && uv run ruff format --check .
env -u FORCE_COLOR -u CLICOLOR_FORCE uv run pytest -q tests/test_gui.py
```

**Measured output (all commands exit 0):**

```text
All checks passed!
121 files already formatted
.......................................................                  [100%]
55 passed in 4.05s
```

**Measured:** temporary corrupted GLBs were refused for duplicate IDs, missing
IDs, a URI key and a mesh moved out of its owning parent. The original fixture
was not mutated. The full pytest suite was deliberately not run, as the spec requires.

**Measured limitation:** Blender initially crashed before Python execution in
sandboxed Metal device detection (`MTLBackend::metal_is_supported`). The same
background command succeeded with sandbox escalation; no GUI was opened.
**Read limitation:** these tests use installed system dependency versions rather
than a fresh uv.lock-synced environment because package installs are forbidden.
No requested artifact or check remains outstanding. No network, package installs,
main-checkout edits, other-worktree edits or push were performed.

## Full inspector output

**Measured:** the following is the complete stdout of the checked-in stdlib
inspector on the delivered GLB. It includes every node's name, extras and local
translation, exact mesh ownership and totals. Identity translations omitted by
glTF are printed as `[0, 0, 0]`.

```text
NODES (translations are local glTF Y-up metres; omitted = [0, 0, 0])
{"extras": {}, "index": 0, "name": "bus.can.dronecan_wire_1", "translation": [0.0014645629562437534, -0.032586514949798584, 0.0036614052951335907]}
{"extras": {}, "index": 1, "name": "bus.can.dronecan_wire_2", "translation": [-0.0005354369059205055, 0.011913493275642395, -0.0013385936617851257]}
{"extras": {"lockwire": {"id": "bus.can.dronecan"}}, "index": 2, "name": "bus.can.dronecan", "translation": [0.004535437095910311, 0.23658651113510132, -0.033661406487226486]}
{"extras": {}, "index": 3, "name": "bus.i2c.external_wire_1", "translation": [-0.0032602425199002028, -0.03688007593154907, 0.003045111894607544]}
{"extras": {}, "index": 4, "name": "bus.i2c.external_wire_2", "translation": [-0.0012602426577359438, 0.0036199241876602173, -0.0019548870623111725]}
{"extras": {}, "index": 5, "name": "bus.i2c.external_wire_3", "translation": [0.007739757187664509, 0.034619927406311035, 0.00204511359333992]}
{"extras": {"lockwire": {"id": "bus.i2c.external"}}, "index": 6, "name": "bus.i2c.external", "translation": [-0.0027397575322538614, 0.2408800721168518, -0.03304511308670044]}
{"extras": {}, "index": 7, "name": "bus.uart.companion_wire_1", "translation": [0.0010117385536432266, 0.01598547399044037, 0.00991503894329071]}
{"extras": {}, "index": 8, "name": "bus.uart.companion_wire_2", "translation": [-0.0014882609248161316, -0.023514524102211, -0.014584962278604507]}
{"extras": {"lockwire": {"id": "bus.uart.companion"}}, "index": 9, "name": "bus.uart.companion", "translation": [-0.023511739447712898, 0.15551452338695526, -0.05291504040360451]}
{"extras": {}, "index": 10, "name": "bus.uart.telemetry_wire_1", "translation": [0.00029312819242477417, 0.00029312074184417725, 0.0034003183245658875]}
{"extras": {}, "index": 11, "name": "bus.uart.telemetry_wire_2", "translation": [-0.002206871286034584, -0.0022068768739700317, -0.025599680840969086]}
{"extras": {"lockwire": {"id": "bus.uart.telemetry"}}, "index": 12, "name": "bus.uart.telemetry", "translation": [0.020706871524453163, 0.1902068704366684, -0.05490031838417053]}
{"extras": {}, "index": 13, "name": "bus.usb.companion_wire_1", "translation": [0.003378499299287796, -0.0056671202182769775, -0.06625406444072723]}
{"extras": {}, "index": 14, "name": "bus.usb.companion_wire_2", "translation": [-0.0026215016841888428, 0.0023328810930252075, 0.019245941191911697]}
{"extras": {}, "index": 15, "name": "bus.usb.companion_wire_3", "translation": [0.004878498613834381, 0.00333288311958313, 0.08374594151973724]}
{"extras": {"lockwire": {"id": "bus.usb.companion"}}, "index": 16, "name": "bus.usb.companion", "translation": [-0.053378500044345856, 0.1276671141386032, -0.0017459397204220295]}
{"extras": {}, "index": 17, "name": "camera", "translation": [0, 0, 0]}
{"extras": {}, "index": 18, "name": "camera_lens", "translation": [-0.029999999329447746, 0, 0.012999996542930603]}
{"extras": {}, "index": 19, "name": "camera_lens.001", "translation": [0.029999999329447746, 0, 0.012999996542930603]}
{"extras": {}, "index": 20, "name": "camera_lens_ring", "translation": [-0.029999999329447746, 0, 0.010999999940395355]}
{"extras": {}, "index": 21, "name": "camera_lens_ring.001", "translation": [0.029999999329447746, 0, 0.010999999940395355]}
{"extras": {"lockwire": {"id": "dev.camera.oak-d-lite"}}, "index": 22, "name": "dev.camera.oak-d-lite", "translation": [0, 0.13199999928474426, 0.08900000154972076]}
{"extras": {}, "index": 23, "name": "jetson_carrier", "translation": [0, -0.016999997198581696, 0]}
{"extras": {}, "index": 24, "name": "jetson_fan", "translation": [0, 0.013000011444091797, 0]}
{"extras": {}, "index": 25, "name": "jetson_fan_blade", "translation": [0.00800000037997961, 0.015000000596046448, 0]}
{"extras": {}, "index": 26, "name": "jetson_fan_blade.001", "translation": [0.0024721359368413687, 0.015000000596046448, -0.007608450949192047]}
{"extras": {}, "index": 27, "name": "jetson_fan_blade.002", "translation": [-0.00647213589400053, 0.015000000596046448, -0.004702277481555939]}
{"extras": {}, "index": 28, "name": "jetson_fan_blade.003", "translation": [-0.00647213589400053, 0.015000000596046448, 0.004702284932136536]}
{"extras": {}, "index": 29, "name": "jetson_fan_blade.004", "translation": [0.0024721359368413687, 0.015000000596046448, 0.007608450949192047]}
{"extras": {}, "index": 30, "name": "jetson_heatsink_fin", "translation": [-0.02500000037252903, 0.0020000040531158447, 0]}
{"extras": {}, "index": 31, "name": "jetson_heatsink_fin.001", "translation": [-0.014999999664723873, 0.0020000040531158447, 0]}
{"extras": {}, "index": 32, "name": "jetson_heatsink_fin.002", "translation": [-0.004999999888241291, 0.0020000040531158447, 0]}
{"extras": {}, "index": 33, "name": "jetson_heatsink_fin.003", "translation": [0.004999999888241291, 0.0020000040531158447, 0]}
{"extras": {}, "index": 34, "name": "jetson_heatsink_fin.004", "translation": [0.014999999664723873, 0.0020000040531158447, 0]}
{"extras": {}, "index": 35, "name": "jetson_heatsink_fin.005", "translation": [0.02500000037252903, 0.0020000040531158447, 0]}
{"extras": {}, "index": 36, "name": "jetson_module", "translation": [0, -0.010999992489814758, 0]}
{"extras": {"lockwire": {"id": "dev.companion.jetson-orin-nano"}}, "index": 37, "name": "dev.companion.jetson-orin-nano", "translation": [0, 0.12999999523162842, -0.09600000083446503]}
{"extras": {}, "index": 38, "name": "esc_1_board", "translation": [0, 0, 0]}
{"extras": {"lockwire": {"id": "dev.esc.esc1"}}, "index": 39, "name": "dev.esc.esc1", "translation": [0.13788582384586334, 0.12999999523162842, 0.13788582384586334]}
{"extras": {}, "index": 40, "name": "esc_2_board", "translation": [0, 0, 0]}
{"extras": {"lockwire": {"id": "dev.esc.esc2"}}, "index": 41, "name": "dev.esc.esc2", "translation": [-0.13788582384586334, 0.12999999523162842, -0.13788582384586334]}
{"extras": {}, "index": 42, "name": "esc_3_board", "translation": [0, 0, 0]}
{"extras": {"lockwire": {"id": "dev.esc.esc3"}}, "index": 43, "name": "dev.esc.esc3", "translation": [-0.13788582384586334, 0.12999999523162842, 0.13788582384586334]}
{"extras": {}, "index": 44, "name": "esc_4_board", "translation": [0, 0, 0]}
{"extras": {"lockwire": {"id": "dev.esc.esc4"}}, "index": 45, "name": "dev.esc.esc4", "translation": [0.13788582384586334, 0.12999999523162842, -0.13788582384586334]}
{"extras": {}, "index": 46, "name": "pixhawk", "translation": [0, 0, 0]}
{"extras": {}, "index": 47, "name": "pixhawk_top", "translation": [0, 0.012999996542930603, 0]}
{"extras": {"lockwire": {"id": "dev.flight-controller.pixhawk-6c"}}, "index": 48, "name": "dev.flight-controller.pixhawk-6c", "translation": [0, 0.19300000369548798, 0]}
{"extras": {}, "index": 49, "name": "gnss_puck", "translation": [0, 0.0020000040531158447, 0]}
{"extras": {}, "index": 50, "name": "gnss_puck_base", "translation": [0, -0.006999999284744263, 0]}
{"extras": {"lockwire": {"id": "dev.gnss.here3-plus"}}, "index": 51, "name": "dev.gnss.here3-plus", "translation": [0, 0.289000004529953, -0.03500000014901161]}
{"extras": {}, "index": 52, "name": "magnetometer_chip", "translation": [0, 0, 0]}
{"extras": {"lockwire": {"id": "dev.magnetometer.ist8310"}}, "index": 53, "name": "dev.magnetometer.ist8310", "translation": [0.014000000432133675, 0.2770000100135803, -0.027000000700354576]}
{"extras": {}, "index": 54, "name": "motor_1_base", "translation": [0, -0.01199999451637268, 0]}
{"extras": {}, "index": 55, "name": "motor_1_bell", "translation": [0, 0.0020000040531158447, 0]}
{"extras": {}, "index": 56, "name": "motor_1_hub", "translation": [0, 0.019999995827674866, 0]}
{"extras": {}, "index": 57, "name": "motor_1_mount_0", "translation": [0.007071077823638916, -0.01600000262260437, -0.007071062922477722]}
{"extras": {}, "index": 58, "name": "motor_1_mount_1", "translation": [-0.007071062922477722, -0.01600000262260437, -0.007071062922477722]}
{"extras": {}, "index": 59, "name": "motor_1_mount_2", "translation": [-0.007071062922477722, -0.01600000262260437, 0.007071077823638916]}
{"extras": {}, "index": 60, "name": "motor_1_mount_3", "translation": [0.007071077823638916, -0.01600000262260437, 0.007071077823638916]}
{"extras": {"lockwire": {"id": "dev.motor.m1"}}, "index": 61, "name": "dev.motor.m1", "translation": [0.1767766922712326, 0.1679999977350235, 0.1767766922712326]}
{"extras": {}, "index": 62, "name": "motor_2_base", "translation": [0, -0.01199999451637268, 0]}
{"extras": {}, "index": 63, "name": "motor_2_bell", "translation": [0, 0.0020000040531158447, 0]}
{"extras": {}, "index": 64, "name": "motor_2_hub", "translation": [0, 0.019999995827674866, 0]}
{"extras": {}, "index": 65, "name": "motor_2_mount_0", "translation": [0.007071062922477722, -0.01600000262260437, -0.007071077823638916]}
{"extras": {}, "index": 66, "name": "motor_2_mount_1", "translation": [-0.007071077823638916, -0.01600000262260437, -0.007071077823638916]}
{"extras": {}, "index": 67, "name": "motor_2_mount_2", "translation": [-0.007071077823638916, -0.01600000262260437, 0.007071062922477722]}
{"extras": {}, "index": 68, "name": "motor_2_mount_3", "translation": [0.007071062922477722, -0.01600000262260437, 0.007071062922477722]}
{"extras": {"lockwire": {"id": "dev.motor.m2"}}, "index": 69, "name": "dev.motor.m2", "translation": [-0.1767766922712326, 0.1679999977350235, -0.1767766922712326]}
{"extras": {}, "index": 70, "name": "motor_3_base", "translation": [0, -0.01199999451637268, 0]}
{"extras": {}, "index": 71, "name": "motor_3_bell", "translation": [0, 0.0020000040531158447, 0]}
{"extras": {}, "index": 72, "name": "motor_3_hub", "translation": [0, 0.019999995827674866, 0]}
{"extras": {}, "index": 73, "name": "motor_3_mount_0", "translation": [0.007071062922477722, -0.01600000262260437, -0.007071062922477722]}
{"extras": {}, "index": 74, "name": "motor_3_mount_1", "translation": [-0.007071077823638916, -0.01600000262260437, -0.007071062922477722]}
{"extras": {}, "index": 75, "name": "motor_3_mount_2", "translation": [-0.007071077823638916, -0.01600000262260437, 0.007071077823638916]}
{"extras": {}, "index": 76, "name": "motor_3_mount_3", "translation": [0.007071062922477722, -0.01600000262260437, 0.007071077823638916]}
{"extras": {"lockwire": {"id": "dev.motor.m3"}}, "index": 77, "name": "dev.motor.m3", "translation": [-0.1767766922712326, 0.1679999977350235, 0.1767766922712326]}
{"extras": {}, "index": 78, "name": "motor_4_base", "translation": [0, -0.01199999451637268, 0]}
{"extras": {}, "index": 79, "name": "motor_4_bell", "translation": [0, 0.0020000040531158447, 0]}
{"extras": {}, "index": 80, "name": "motor_4_hub", "translation": [0, 0.019999995827674866, 0]}
{"extras": {}, "index": 81, "name": "motor_4_mount_0", "translation": [0.007071077823638916, -0.01600000262260437, -0.007071077823638916]}
{"extras": {}, "index": 82, "name": "motor_4_mount_1", "translation": [-0.007071062922477722, -0.01600000262260437, -0.007071077823638916]}
{"extras": {}, "index": 83, "name": "motor_4_mount_2", "translation": [-0.007071062922477722, -0.01600000262260437, 0.007071062922477722]}
{"extras": {}, "index": 84, "name": "motor_4_mount_3", "translation": [0.007071077823638916, -0.01600000262260437, 0.007071062922477722]}
{"extras": {"lockwire": {"id": "dev.motor.m4"}}, "index": 85, "name": "dev.motor.m4", "translation": [0.1767766922712326, 0.1679999977350235, -0.1767766922712326]}
{"extras": {}, "index": 86, "name": "pm02", "translation": [0, 0, 0]}
{"extras": {}, "index": 87, "name": "pm02_regulator", "translation": [0, 0.005000010132789612, 0]}
{"extras": {"lockwire": {"id": "dev.power-module.pm02-v3"}}, "index": 88, "name": "dev.power-module.pm02-v3", "translation": [0, 0.1589999943971634, -0.04699999839067459]}
{"extras": {}, "index": 89, "name": "rfd900x_antenna", "translation": [0, 0.016499996185302734, -0.03974998742341995]}
{"extras": {}, "index": 90, "name": "rfd900x_box", "translation": [0, 0, 0]}
{"extras": {"lockwire": {"id": "dev.radio.rfd900x"}}, "index": 91, "name": "dev.radio.rfd900x", "translation": [0, 0.18799999356269836, -0.1080000028014183]}
{"extras": {}, "index": 92, "name": "rail.pixhawk-telem1-5v_wire_1", "translation": [0.0002931300550699234, 0.00029312074184417725, 0.0034003183245658875]}
{"extras": {}, "index": 93, "name": "rail.pixhawk-telem1-5v_wire_2", "translation": [-0.0022068703547120094, -0.0022068768739700317, -0.025599680840969086]}
{"extras": {"lockwire": {"id": "rail.pixhawk-telem1-5v"}}, "index": 94, "name": "rail.pixhawk-telem1-5v", "translation": [0.01470686960965395, 0.1902068704366684, -0.05490031838417053]}
{"extras": {}, "index": 95, "name": "rail.pm02-5v_wire_1", "translation": [0.04742524027824402, 0.010646045207977295, -0.03570890799164772]}
{"extras": {}, "index": 96, "name": "rail.pm02-5v_wire_2", "translation": [0.06292524188756943, -0.01185394823551178, -0.06020890548825264]}
{"extras": {}, "index": 97, "name": "rail.pm02-5v_wire_3", "translation": [-0.01457475870847702, 0.010646045207977295, -0.028208907693624496]}
{"extras": {}, "index": 98, "name": "rail.pm02-5v_wire_4", "translation": [-0.03357475996017456, -0.0038539469242095947, 0.03279109299182892]}
{"extras": {}, "index": 99, "name": "rail.pm02-5v_wire_5", "translation": [-0.026074759662151337, -0.0028539448976516724, 0.09729109704494476]}
{"extras": {"lockwire": {"id": "rail.pm02-5v"}}, "index": 100, "name": "rail.pm02-5v", "translation": [-0.0164252407848835, 0.133853942155838, -0.015291091054677963]}
{"extras": {}, "index": 101, "name": "rail.servo-motor_wire_1", "translation": [0.06894291192293167, 0, 0.057718027383089066]}
{"extras": {}, "index": 102, "name": "rail.servo-motor_wire_2", "translation": [-0.06894291192293167, 0, -0.08016780018806458]}
{"extras": {}, "index": 103, "name": "rail.servo-motor_wire_3", "translation": [-0.06894291192293167, 0, 0.057718027383089066]}
{"extras": {}, "index": 104, "name": "rail.servo-motor_wire_4", "translation": [0.06894291192293167, 0, -0.08016780018806458]}
{"extras": {"lockwire": {"id": "rail.servo-motor"}}, "index": 105, "name": "rail.servo-motor", "translation": [0, 0.14249999821186066, -0.01227511465549469]}
{"extras": {}, "index": 106, "name": "battery", "translation": [0, 0.06700000166893005, 0.004999999888241291]}
{"extras": {}, "index": 107, "name": "battery_label", "translation": [0, 0.06700000166893005, 0.057999998331069946]}
{"extras": {}, "index": 108, "name": "battery_strap", "translation": [0, 0.09300000220537186, 0.03500000014901161]}
{"extras": {}, "index": 109, "name": "battery_strap.001", "translation": [0, 0.09300000220537186, -0.02500000037252903]}
{"extras": {}, "index": 110, "name": "battery_strap_side", "translation": [-0.03400000184774399, 0.06700000166893005, 0.03500000014901161]}
{"extras": {}, "index": 111, "name": "battery_strap_side.001", "translation": [0.03400000184774399, 0.06700000166893005, 0.03500000014901161]}
{"extras": {}, "index": 112, "name": "battery_strap_side.002", "translation": [-0.03400000184774399, 0.06700000166893005, -0.02500000037252903]}
{"extras": {}, "index": 113, "name": "battery_strap_side.003", "translation": [0.03400000184774399, 0.06700000166893005, -0.02500000037252903]}
{"extras": {}, "index": 114, "name": "esc_1_motor_lead_1", "translation": [0.15520994365215302, 0.1419999897480011, 0.15945257246494293]}
{"extras": {}, "index": 115, "name": "esc_1_motor_lead_2", "translation": [0.15733125805854797, 0.1419999897480011, 0.15733125805854797]}
{"extras": {}, "index": 116, "name": "esc_1_motor_lead_3", "translation": [0.15945257246494293, 0.1419999897480011, 0.15520994365215302]}
{"extras": {}, "index": 117, "name": "esc_1_strap", "translation": [0.13788582384586334, 0.1340000033378601, 0.13788582384586334]}
{"extras": {}, "index": 118, "name": "esc_2_motor_lead_1", "translation": [-0.15520994365215302, 0.1419999897480011, -0.15945257246494293]}
{"extras": {}, "index": 119, "name": "esc_2_motor_lead_2", "translation": [-0.15733125805854797, 0.1419999897480011, -0.15733125805854797]}
{"extras": {}, "index": 120, "name": "esc_2_motor_lead_3", "translation": [-0.15945257246494293, 0.1419999897480011, -0.15520994365215302]}
{"extras": {}, "index": 121, "name": "esc_2_strap", "translation": [-0.13788582384586334, 0.1340000033378601, -0.13788582384586334]}
{"extras": {}, "index": 122, "name": "esc_3_motor_lead_1", "translation": [-0.15945257246494293, 0.1419999897480011, 0.15520994365215302]}
{"extras": {}, "index": 123, "name": "esc_3_motor_lead_2", "translation": [-0.15733125805854797, 0.1419999897480011, 0.15733125805854797]}
{"extras": {}, "index": 124, "name": "esc_3_motor_lead_3", "translation": [-0.15520994365215302, 0.1419999897480011, 0.15945257246494293]}
{"extras": {}, "index": 125, "name": "esc_3_strap", "translation": [-0.13788582384586334, 0.1340000033378601, 0.13788582384586334]}
{"extras": {}, "index": 126, "name": "esc_4_motor_lead_1", "translation": [0.15945257246494293, 0.1419999897480011, -0.15520994365215302]}
{"extras": {}, "index": 127, "name": "esc_4_motor_lead_2", "translation": [0.15733125805854797, 0.1419999897480011, -0.15733125805854797]}
{"extras": {}, "index": 128, "name": "esc_4_motor_lead_3", "translation": [0.15520994365215302, 0.1419999897480011, -0.15945257246494293]}
{"extras": {}, "index": 129, "name": "esc_4_strap", "translation": [0.13788582384586334, 0.1340000033378601, -0.13788582384586334]}
{"extras": {}, "index": 130, "name": "frame_arm_1", "translation": [0.0883883461356163, 0.14499999582767487, 0.0883883461356163]}
{"extras": {}, "index": 131, "name": "frame_arm_2", "translation": [-0.0883883461356163, 0.14499999582767487, -0.0883883461356163]}
{"extras": {}, "index": 132, "name": "frame_arm_3", "translation": [-0.0883883461356163, 0.14499999582767487, 0.0883883461356163]}
{"extras": {}, "index": 133, "name": "frame_arm_4", "translation": [0.0883883461356163, 0.14499999582767487, -0.0883883461356163]}
{"extras": {}, "index": 134, "name": "frame_arm_clamp_1", "translation": [0.04065864160656929, 0.14499999582767487, 0.04065864160656929]}
{"extras": {}, "index": 135, "name": "frame_arm_clamp_2", "translation": [-0.04065864160656929, 0.14499999582767487, -0.04065864160656929]}
{"extras": {}, "index": 136, "name": "frame_arm_clamp_3", "translation": [-0.04065864160656929, 0.14499999582767487, 0.04065864160656929]}
{"extras": {}, "index": 137, "name": "frame_arm_clamp_4", "translation": [0.04065864160656929, 0.14499999582767487, -0.04065864160656929]}
{"extras": {}, "index": 138, "name": "frame_landing_leg_1_1", "translation": [-0.06499999761581421, 0.057999998331069946, 0.04500000178813934]}
{"extras": {}, "index": 139, "name": "frame_landing_leg_1_2", "translation": [-0.06499999761581421, 0.057999998331069946, -0.04500000178813934]}
{"extras": {}, "index": 140, "name": "frame_landing_leg_2_1", "translation": [0.06499999761581421, 0.057999998331069946, 0.04500000178813934]}
{"extras": {}, "index": 141, "name": "frame_landing_leg_2_2", "translation": [0.06499999761581421, 0.057999998331069946, -0.04500000178813934]}
{"extras": {}, "index": 142, "name": "frame_landing_skid_1", "translation": [-0.07800000160932541, 0.004999999888241291, 0]}
{"extras": {}, "index": 143, "name": "frame_landing_skid_2", "translation": [0.07800000160932541, 0.004999999888241291, 0]}
{"extras": {}, "index": 144, "name": "frame_lower_plate", "translation": [0, 0.1120000034570694, 0]}
{"extras": {}, "index": 145, "name": "frame_rear_tray", "translation": [0, 0.1080000028014183, -0.09799999743700027]}
{"extras": {}, "index": 146, "name": "frame_screw", "translation": [-0.04500000178813934, 0.17299999296665192, 0.05700000002980232]}
{"extras": {}, "index": 147, "name": "frame_screw.001", "translation": [-0.04500000178813934, 0.17299999296665192, -0.05700000002980232]}
{"extras": {}, "index": 148, "name": "frame_screw.002", "translation": [0.04500000178813934, 0.17299999296665192, 0.05700000002980232]}
{"extras": {}, "index": 149, "name": "frame_screw.003", "translation": [0.04500000178813934, 0.17299999296665192, -0.05700000002980232]}
{"extras": {}, "index": 150, "name": "frame_standoff", "translation": [-0.04500000178813934, 0.14100000262260437, 0.05700000002980232]}
{"extras": {}, "index": 151, "name": "frame_standoff.001", "translation": [-0.04500000178813934, 0.14100000262260437, -0.05700000002980232]}
{"extras": {}, "index": 152, "name": "frame_standoff.002", "translation": [0.04500000178813934, 0.14100000262260437, 0.05700000002980232]}
{"extras": {}, "index": 153, "name": "frame_standoff.003", "translation": [0.04500000178813934, 0.14100000262260437, -0.05700000002980232]}
{"extras": {}, "index": 154, "name": "frame_top_plate", "translation": [0, 0.17000000178813934, 0]}
{"extras": {}, "index": 155, "name": "gnss_mast", "translation": [0, 0.22600001096725464, -0.03500000014901161]}
{"extras": {}, "index": 156, "name": "pixhawk_screw", "translation": [-0.02500000037252903, 0.20749999582767487, 0.017999999225139618]}
{"extras": {}, "index": 157, "name": "pixhawk_screw.001", "translation": [-0.02500000037252903, 0.20749999582767487, -0.017999999225139618]}
{"extras": {}, "index": 158, "name": "pixhawk_screw.002", "translation": [0.02500000037252903, 0.20749999582767487, 0.017999999225139618]}
{"extras": {}, "index": 159, "name": "pixhawk_screw.003", "translation": [0.02500000037252903, 0.20749999582767487, -0.017999999225139618]}
{"extras": {}, "index": 160, "name": "pixhawk_standoff", "translation": [-0.02500000037252903, 0.17649999260902405, 0.017999999225139618]}
{"extras": {}, "index": 161, "name": "pixhawk_standoff.001", "translation": [-0.02500000037252903, 0.17649999260902405, -0.017999999225139618]}
{"extras": {}, "index": 162, "name": "pixhawk_standoff.002", "translation": [0.02500000037252903, 0.17649999260902405, 0.017999999225139618]}
{"extras": {}, "index": 163, "name": "pixhawk_standoff.003", "translation": [0.02500000037252903, 0.17649999260902405, -0.017999999225139618]}
{"extras": {}, "index": 164, "name": "pm02_screw", "translation": [-0.014999999664723873, 0.16300000250339508, -0.04699999839067459]}
{"extras": {}, "index": 165, "name": "pm02_screw.001", "translation": [0.014999999664723873, 0.16300000250339508, -0.04699999839067459]}
{"extras": {}, "index": 166, "name": "propeller_1", "translation": [0.1767766922712326, 0.18799999356269836, 0.1767766922712326]}
{"extras": {}, "index": 167, "name": "propeller_2", "translation": [-0.1767766922712326, 0.18799999356269836, -0.1767766922712326]}
{"extras": {}, "index": 168, "name": "propeller_3", "translation": [-0.1767766922712326, 0.18799999356269836, 0.1767766922712326]}
{"extras": {}, "index": 169, "name": "propeller_4", "translation": [0.1767766922712326, 0.18799999356269836, -0.1767766922712326]}
{"extras": {}, "index": 170, "name": "radio_tray_standoff", "translation": [0, 0.16500000655651093, -0.1080000028014183]}
{"extras": {"lockwire": {"id": "x500_frame"}}, "index": 171, "name": "x500_frame", "translation": [0, 0, 0]}

ID TABLE (node name = contract ID; exact descendant mesh-node names)
bus.can.dronecan -> bus.can.dronecan -> bus.can.dronecan_wire_1, bus.can.dronecan_wire_2
bus.i2c.external -> bus.i2c.external -> bus.i2c.external_wire_1, bus.i2c.external_wire_2, bus.i2c.external_wire_3
bus.uart.companion -> bus.uart.companion -> bus.uart.companion_wire_1, bus.uart.companion_wire_2
bus.uart.telemetry -> bus.uart.telemetry -> bus.uart.telemetry_wire_1, bus.uart.telemetry_wire_2
bus.usb.companion -> bus.usb.companion -> bus.usb.companion_wire_1, bus.usb.companion_wire_2, bus.usb.companion_wire_3
dev.camera.oak-d-lite -> dev.camera.oak-d-lite -> camera, camera_lens, camera_lens.001, camera_lens_ring, camera_lens_ring.001
dev.companion.jetson-orin-nano -> dev.companion.jetson-orin-nano -> jetson_carrier, jetson_fan, jetson_fan_blade, jetson_fan_blade.001, jetson_fan_blade.002, jetson_fan_blade.003, jetson_fan_blade.004, jetson_heatsink_fin, jetson_heatsink_fin.001, jetson_heatsink_fin.002, jetson_heatsink_fin.003, jetson_heatsink_fin.004, jetson_heatsink_fin.005, jetson_module
dev.esc.esc1 -> dev.esc.esc1 -> esc_1_board
dev.esc.esc2 -> dev.esc.esc2 -> esc_2_board
dev.esc.esc3 -> dev.esc.esc3 -> esc_3_board
dev.esc.esc4 -> dev.esc.esc4 -> esc_4_board
dev.flight-controller.pixhawk-6c -> dev.flight-controller.pixhawk-6c -> pixhawk, pixhawk_top
dev.gnss.here3-plus -> dev.gnss.here3-plus -> gnss_puck, gnss_puck_base
dev.magnetometer.ist8310 -> dev.magnetometer.ist8310 -> magnetometer_chip
dev.motor.m1 -> dev.motor.m1 -> motor_1_base, motor_1_bell, motor_1_hub, motor_1_mount_0, motor_1_mount_1, motor_1_mount_2, motor_1_mount_3
dev.motor.m2 -> dev.motor.m2 -> motor_2_base, motor_2_bell, motor_2_hub, motor_2_mount_0, motor_2_mount_1, motor_2_mount_2, motor_2_mount_3
dev.motor.m3 -> dev.motor.m3 -> motor_3_base, motor_3_bell, motor_3_hub, motor_3_mount_0, motor_3_mount_1, motor_3_mount_2, motor_3_mount_3
dev.motor.m4 -> dev.motor.m4 -> motor_4_base, motor_4_bell, motor_4_hub, motor_4_mount_0, motor_4_mount_1, motor_4_mount_2, motor_4_mount_3
dev.power-module.pm02-v3 -> dev.power-module.pm02-v3 -> pm02, pm02_regulator
dev.radio.rfd900x -> dev.radio.rfd900x -> rfd900x_antenna, rfd900x_box
rail.pixhawk-telem1-5v -> rail.pixhawk-telem1-5v -> rail.pixhawk-telem1-5v_wire_1, rail.pixhawk-telem1-5v_wire_2
rail.pm02-5v -> rail.pm02-5v -> rail.pm02-5v_wire_1, rail.pm02-5v_wire_2, rail.pm02-5v_wire_3, rail.pm02-5v_wire_4, rail.pm02-5v_wire_5
rail.servo-motor -> rail.servo-motor -> rail.servo-motor_wire_1, rail.servo-motor_wire_2, rail.servo-motor_wire_3, rail.servo-motor_wire_4
x500_frame -> x500_frame -> battery, battery_label, battery_strap, battery_strap.001, battery_strap_side, battery_strap_side.001, battery_strap_side.002, battery_strap_side.003, esc_1_motor_lead_1, esc_1_motor_lead_2, esc_1_motor_lead_3, esc_1_strap, esc_2_motor_lead_1, esc_2_motor_lead_2, esc_2_motor_lead_3, esc_2_strap, esc_3_motor_lead_1, esc_3_motor_lead_2, esc_3_motor_lead_3, esc_3_strap, esc_4_motor_lead_1, esc_4_motor_lead_2, esc_4_motor_lead_3, esc_4_strap, frame_arm_1, frame_arm_2, frame_arm_3, frame_arm_4, frame_arm_clamp_1, frame_arm_clamp_2, frame_arm_clamp_3, frame_arm_clamp_4, frame_landing_leg_1_1, frame_landing_leg_1_2, frame_landing_leg_2_1, frame_landing_leg_2_2, frame_landing_skid_1, frame_landing_skid_2, frame_lower_plate, frame_rear_tray, frame_screw, frame_screw.001, frame_screw.002, frame_screw.003, frame_standoff, frame_standoff.001, frame_standoff.002, frame_standoff.003, frame_top_plate, gnss_mast, pixhawk_screw, pixhawk_screw.001, pixhawk_screw.002, pixhawk_screw.003, pixhawk_standoff, pixhawk_standoff.001, pixhawk_standoff.002, pixhawk_standoff.003, pm02_screw, pm02_screw.001, propeller_1, propeller_2, propeller_3, propeller_4, radio_tray_standoff

SUMMARY {"bytes": 367776, "ids": 24, "mesh_nodes": 148, "nodes": 172, "sha256": "c388e098b2ca377ddee5cb4ca4638d635669ea2e4d658aeea4843ef0e1a5aa2c", "triangles": 5928, "uri_keys": 0, "vertices": 10832}
PASS: IDs, ownership, embedded BIN, no URI/textures, materials/normals, size, motor/ESC placement
```
