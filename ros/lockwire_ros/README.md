# lockwire_ros

Lockwire's preflight gate for ROS 2. One command subscribes to the topics your
lockfile declares, records a few seconds of what the robot is doing, and says
go or no-go, with an exit code a shell or a launch file can act on.

```bash
ros2 run lockwire_ros preflight lockwire.yaml --seconds 10
echo $?      # 0 GO, 1 NO-GO, 2 the gate could not run, 3 nothing was scored
```

Runs on **Humble, Jazzy, Kilted and Lyrical**.

---

## Why this package imports nothing from lockwire

lockwire's core needs Python ≥ 3.12. ROS 2 Humble ships Python 3.10 and is
supported until May 2027. A node that did `import lockwire` could therefore
never run on Humble at all.

So it doesn't. This package is thin rclpy with no dependency outside
`ros_core`, and it invokes the `lockwire` executable **out of process**. The
node runs on whatever Python your distro ships; the core runs on its own.
`tests/fixtures/humble/run_suite.sh` proves it in one container with two
interpreters (Humble's 3.10 for the node, 3.12 for the core).

## Install

lockwire is open source (Apache-2.0). This package is not in rosdistro; build it from source.
You need two things on the robot:

1. **The lockwire core**, in its own Python ≥ 3.12 environment:
   `pip install lockwire` (or `uv tool install lockwire`). Anywhere on `PATH`,
   or pass `--lockwire-executable` / `lockwire_executable:=`.
2. **This package** in your colcon workspace. It is not in a rosdistro yet, so
   build it from source:

```bash
cp -r /path/to/lockwire/ros/lockwire_ros ~/ws/src/
cd ~/ws && colcon build --packages-select lockwire_ros && . install/setup.bash
```

## One command: gate the robot as it runs now

```bash
ros2 run lockwire_ros preflight lockwire.yaml --seconds 10 --context bench-idle
```

It reads each device's `driver` block in the lockfile (`topic`, `msg_type`,
`signals`), subscribes to those topics, captures for `--seconds`, writes a
lockwire recording and runs `lockwire preflight --from` on it. It prints the
same verdict text the `lockwire` CLI prints and exits with the same code.

| flag | default | meaning |
|---|---|---|
| `--seconds` | `10` | capture window |
| `--context` | `bench-idle` | `bench-idle`, `armed-idle` or `nominal-op`: which envelopes apply |
| `--out DIR` | a temp dir | keep the recording; the run's `lockwire-preflight.json` lands beside it |
| `--json` | off | print the core's JSON document on stdout (other text goes to stderr) |
| `--no-ledger` | off | do not append this run to the health ledger (`lockwire.health.jsonl`) |
| `--profile`, `--require-evidence` | `analysis` | passed to the core, as on the CLI |
| `--lockwire-executable` | `lockwire` | path to the core |
| `--timeout-s` | `120` | a gate that hangs is a refusal, not a pass |

A topic that publishes nothing in the window is named in the log. If no
declared topic publishes at all, nothing is recorded and the gate refuses
with exit 2, naming the topics: silence is never a pass.

**What it can read:** numeric fields reached by a dotted path, such as
`linear_acceleration.z` on `sensor_msgs/msg/Imu` or `data` on a `std_msgs`
scalar, and array entries on `sensor_msgs/msg/JointState`: `position[2]` by
index, or `position[name=knee_joint]` by joint name. The syntax is in
[docs/guides/ros2.md](../../docs/guides/ros2.md).

## Gate your bringup behind it

Nothing starts until the gate says GO, and a refusal makes `ros2 launch` exit
nonzero:

```bash
ros2 launch lockwire_ros lockwire_gate.launch.py contract:=lockwire.yaml \
    live:=true capture_s:=10 bringup:=/path/to/my_robot/bringup.launch.py
```

Or from your own launch file:

```python
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from lockwire_ros.launch_gate import gate_bringup

def generate_launch_description():
    bringup = IncludeLaunchDescription(PythonLaunchDescriptionSource("bringup.launch.py"))
    return LaunchDescription(gate_bringup(
        on_pass=[bringup], contract="lockwire.yaml", live=True, capture_s=10))
```

Launch arguments of `lockwire_gate.launch.py` (and `preflight.launch.py`, the
same file under its old name): `contract`, `live` (default `false`),
`capture_s` (`10.0`), `context` (`bench-idle`), `recording` (gate a recording
instead of live topics), `bringup` (launch file to start after a GO),
`lockwire_executable`.

**Include it with `bringup:=`, not beside your nodes.** A bare
`IncludeLaunchDescription` of the gate still fails the launch on NO-GO, but
the other actions in your file start at the same time as the gate, not after
it. Put what must wait inside `bringup` (or `on_pass`).

To gate a recording you already have instead of live topics, pass
`recording:=recordings/bench-idle` and leave `live` off.

## The lifecycle node

For a managed bringup, the gate is also a lifecycle node that refuses to
configure on anything short of GO:

```bash
ros2 run lockwire_ros gate_node --ros-args \
    -p contract:=lockwire.yaml -p capture_s:=10.0
ros2 lifecycle set /lockwire_gate configure
ros2 lifecycle get /lockwire_gate     # "unconfigured" if it refused
```

### Parameters

| parameter | default | meaning |
|---|---|---|
| `contract` | `lockwire.yaml` | the lockfile to gate against |
| `recording` | `""` | recording directory or `.ulg` to check; wins over `capture_s` |
| `capture_s` | `0.0` | capture the contract's topics live for this long, then gate |
| `capture_context` | `bench-idle` | context of the live capture |
| `capture_dir` | a temp dir | where the live capture is written |
| `lockwire_executable` | `lockwire` | path to the core |
| `timeout_s` | `120.0` | a gate that hangs is a refusal, not a pass |
| `profile` | `analysis` | use `powered-release` to require named evidence before clearance |
| `required_evidence` | `[]` | string array passed as repeated `--require-evidence`; powered-release requires a nonempty list |
| `publish_diagnostics` | `true` | publish one result to `/diagnostics` at configure |
| `diagnostics_topic` | `/diagnostics` | where to publish |
| `refuse_when_nothing_scored` | `true` | refuse a pass whose denominator is zero |

For a powered release, select `profile:=powered-release` and explicitly name the
required evidence: `watched-command-stream`, `live-hardware-software-reconciliation`,
and/or `comm-links-under-representative-load`. The core refuses missing or
unverified required evidence, even when every scored check otherwise passes.

## Two behaviours worth knowing before you file a bug

**It fails closed, and "refused" is not "NO-GO".** If the core is missing, times
out, crashes, or writes a document the node can't read, the outcome is
`REFUSED` and configuring fails. A robot cleared because the gate could not run
is the worst thing this package could do, so there is exactly one route to GO: a
clean exit with a document that says so. The two words are kept distinct in the
logs so you can tell a robot that failed from a gate that never ran.

**`ros2 launch` does not propagate child exit codes, and this package works
around it.** `LaunchService.run()` only returns nonzero when an exception
reaches it — never because a launched process exited nonzero
([ros2/launch#586](https://github.com/ros2/launch/issues/586), open since
January 2022). Measured on Humble: a launch file that reacts to a refusal with
`EmitEvent(Shutdown(...))` — the shape almost everyone writes — shuts the launch
down and **still exits 0**. `gate_bringup` raises instead, so the refusal reaches
whatever is watching. Both halves are pinned by tests, the naive shape included
as a negative control, so if upstream ever fixes this we find out.

## Running the tests

Off-robot (the ROS-free half — decision logic and diagnostics mapping):

```bash
uv run pytest ros/lockwire_ros/test/         # rclpy cases skip
```

On Humble, everything including the live core:

```bash
docker build -t lockwire-humble:gate -f tests/fixtures/humble/Dockerfile \
    tests/fixtures/humble
docker run --rm -v "$PWD":/ws:ro \
    -v "$PWD/tests/fixtures/humble/run_suite.sh":/run.sh:ro \
    lockwire-humble:gate bash /run.sh
```

The harness prints skip reasons (`-rs`) on purpose. An early run of this suite
reported "25 passed" while every rclpy case had silently skipped, because
`PYTHONPATH` was overwritten instead of prepended and `rclpy` was invisible. A
skip is not a pass, and a suite that can't tell you the difference is not
evidence.

## Trying it on a bag you already have — no ROS, no this package

If you just want to know whether lockwire says anything useful about your robot,
you do not need this package or a running system. The core reads rosbag2 MCAP
files directly, with no ROS installation:

```bash
pip install 'lockwire[ros2bag]'          # 4 pure wheels; no ROS, no compiler
lockwire preflight lockwire.yaml --from rosbag2_2026_08_14/ --context bench-idle
```

That works on a laptop. MCAP has been rosbag2's default since Iron, and a bag's
`log_time` is exactly lockwire's receive time, so the mapping is exact rather
than approximate. SQLite3 `.db3` bags are refused by name — re-record with the
mcap plugin or `ros2 bag convert`.

The contract needs `msg_type` and `signals` on the devices you want read;
without them lockwire says so instead of returning an empty result.

## What this package does not do yet

- **No `ros2 bag record` from the capture node.** It writes lockwire's native
  recording, not a bag, because `rosbag2` lives in `ros_base` rather than
  `ros_core` and this package takes nothing outside `ros_core`. If you want a
  bag, record one yourself and use the offline path above.
- **No hardware↔software reconciliation.** Live capture scores the signals; it
  does not close the stage 0/1 boundary. See `docs/gate_coverage.md`.

## Continuous monitoring

`gate_node` checks once, at bringup. `monitor` keeps checking while the robot runs:

```bash
ros2 run lockwire_ros monitor --ros-args -p contract:=lockwire.yaml
ros2 topic echo --qos-durability transient_local --qos-reliability reliable /lockwire/status
```

It subscribes to every topic the contract's drivers declare and, every `period_s`, gates the
last `window_s` seconds of traffic with `lockwire preflight`. Each period it publishes:

- `/lockwire/status`: one `diagnostic_msgs/DiagnosticStatus`. Level OK for GO, WARN for GO
  with notes, WARN for NOT SCORED (not STALE: STALE means "no fresh data", and this verdict is
  fresh, it just covered nothing), ERROR for NO-GO. The message is the verdict line; the
  values are the summary counts plus one entry per finding. The topic is latched (RELIABLE,
  TRANSIENT_LOCAL, depth 1), so a node that subscribes late still gets the last verdict, as
  long as it asks for the same durability.
- `/diagnostics`: the same verdict as a `DiagnosticArray`, the mapping `gate_node` uses.

It never goes quiet. A window with no messages, a field that does not resolve or is not a
number, a missing, crashing or hung core: each publishes ERROR with the reason (a bad field
names the device, topic and field). The node keeps running and publishes ERROR every period
while the fault lasts, so the status clears by itself once the traffic is readable again.

| parameter | default | meaning |
|---|---|---|
| `contract` | `lockwire.yaml` | the lockfile to gate against |
| `window_s` | `10.0` | seconds of traffic each verdict covers |
| `period_s` | `10.0` | seconds between verdicts |
| `context` | `nominal-op` | the recording context (a monitor watches a running robot) |
| `timeout_s` | `0` (= `period_s`) | a gate run that takes longer is an ERROR |
| `status_topic` | `/lockwire/status` | the latched status |
| `diagnostics_topic` | `/diagnostics` | where the array goes |
| `lockwire_executable` | `lockwire` | path to the core |
| `best_effort` | `true` | subscribe BEST_EFFORT, as sensor drivers publish |
| `keep_last` | `0` | window recordings to keep on disk; 0 deletes each after it is gated |

The gate runs on a worker thread so subscriptions keep receiving while it runs. One gate runs
at a time: if it is still running when the next period comes, that period is skipped and
logged. The monitor gates nothing by itself. To hold a node back on a bad verdict, subscribe
to `/lockwire/status` with the latched QoS (`lockwire_ros.monitor_node.STATUS_QOS`) and stop
when the level is ERROR. The start-time gate is still `gate_node`, which refuses to configure.
