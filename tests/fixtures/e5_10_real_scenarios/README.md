# Real controller/plant scenario sources

Status: **source preparation only — READY for review, not approved**. No simulator, PX4 build,
controller runtime, package, product path, or hardware acceptance was run or is implied here.

## Provenance and closure receipt

`sources.json` and `assets.lock` are the machine-readable receipt. `assets.lock` records exact byte
size, upstream Git blob SHA-1, and downloaded SHA-256 for every retained file. Run only the static
verifier with:

```bash
python3 tests/fixtures/e5_10_real_scenarios/verify.py
```

The selected PX4 release is `v1.16.2`: annotated tag object
`cec6f597235c27f8d42d900e5c5fc8e4d792dbd6` peels to PX4 source commit
`54f0455ffcd755534539a7cf33a09a20bf71d29d`. That commit's actual Git tree has a `160000`
`Tools/simulation/gz` entry at `e05f4312d3f28aa621157610584a4870406cb6d3`, and its checked-in
`.gitmodules` maps that path to `PX4/PX4-gazebo-models`. Therefore the X500 model inputs use
`e05f4312...`; unrelated models-main commit `5577035...` is explicitly excluded.

The X500 closure contains the two model definitions, their configs and licenses, all five directly
referenced meshes, the two SDF-referenced texture files, and `CF.png`, which is referenced inside
`NXP-HGD-CF.dae`. The official local
`default.sdf` and PX4 airframe/manifest evidence are also retained. The ANYmal D input is official
`ANYbotics/anymal_d_simple_description@d5bf4dc3a81af81a16b9598fc15c98e339f45a33`:
its URDF, package metadata, all 18 URDF-referenced DAE meshes, and the 14 image files actually
referenced by those DAE files. Recursive GitHub trees used for sizing reported `truncated:false`.

| Source | Retained files | `.tree[].size` bytes | License |
| --- | ---: | ---: | --- |
| PX4 Autopilot release evidence | 3 | 6,620 | BSD-3-Clause, PX4 Development Team |
| PX4 Gazebo X500/model closure | 16 | 25,857,375 | BSD-3-Clause, PX4 / Rudis Laboratories |
| official ANYmal D closure | 35 | 4,590,664 | BSD-3-Clause, ANYbotics AG |
| **Total** | **54** | **30,454,659** | 36,654,205 bytes below 64 MiB |

The official license texts are retained beside the assets. No archive was downloaded or extracted;
no upstream script was executed.

## Scenario contract

`scenarios.json` supplies the later motion-runner acceptance contract. The X500 case commands the
real PX4 controller through `vehicle_command`, then decides 2 m takeoff, at least 10 s hover, landing,
and the 90 s ceiling from estimator/sensor/landed feedback—not command echo. The official model has
four Harmonic multicopter motor systems and barometer, magnetometer, IMU, and navsat sensors.

The ANYmal case uses the official inertial/collision plant and all twelve official 80 Nm-limited leg
joints. `anymal_control_overlay.xml` exposes effort commands and position/velocity/effort state to
`gz_ros2_control`; `controllers.yaml` selects JointTrajectoryController effort mode. The two squat
cycles independently require 0.15 rad HFE and KFE displacement from actual joint state, plus effort,
contact, and IMU traces. The negative case deactivates exactly `anymal_effort_controller`, confirms it
is inactive before capture, forbids controller commands during capture, and sets `hold_joints:false`.
Its explicit two-second gravity release must leave the controller inactive and produce either at
least 0.01 rad actual joint-position change or at least 0.02 rad/s actual joint speed. These are
acceptance requirements for a future disabled-controller run, not evidence that the run happened.

The static verifier mutates an in-memory copy of this negative and proves that a wrong declared or
targeted controller, removed gravity release, wrong duration, weakened continued-inactivity
condition, enabled implicit hold, weakened position or speed threshold, or controller command during
capture is rejected. It restores by discarding each copy; it never changes the fixture on disk or
substitutes for dynamic evidence.

The nominal stance and P/D gains are explicitly **LOCKWIRE TEST configuration**. They are not
inferred from ANYbotics, are not installed-robot parameters, and require runtime tuning/validation.

Primary interface references used for the authored overlay:

- [gz_ros2_control Jazzy documentation](https://control.ros.org/jazzy/doc/gz_ros2_control/doc/index.html)
- [JointTrajectoryController Jazzy documentation](https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/userdoc.html)
- [Gazebo Harmonic contact sensor tutorial](https://gazebosim.org/docs/harmonic/sensors/#contact-sensor)

## Pending ceilings

Runtime and product acceptance remain **PENDING**: existing runner/job lifecycle integration, exact
Harmonic and ROS package availability, controller activation/deactivation, two numerical squat
cycles, the completed disabled negative, PX4 2 m / 10 s / 90 s evidence traces, package inclusion,
independent review, and the product UI path. This fixture is neither simulation-validity evidence nor
physical/hardware proof.
