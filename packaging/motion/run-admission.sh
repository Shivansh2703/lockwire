#!/usr/bin/env bash
# Runs only inside the fixed E5.9a image. Host isolation is owned by jobs.py.
set -euo pipefail

mode=${1:-enabled}
case "$mode" in enabled|disabled) ;; *) echo "mode must be enabled or disabled" >&2; exit 64;; esac
out=/output
mkdir -p "$out"
stamp() {
  python3 -u -c 'import sys
from datetime import datetime, timezone

stamp = None
for line in sys.stdin:
    line = line.rstrip("\n")
    if stamp is None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(stamp, line)
    if line == "---":
        stamp = None'
}
finish() {
  status=$?
  for pid in ${sim_pid:-} ${state_pid:-} ${controller_pid:-}; do kill "$pid" 2>/dev/null || true; done
  python3 - "$out/document.json" "$mode" "$status" <<'PY'
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

document_path, mode, status = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
out = document_path.parent
state = out / "joint_states.raw"
effort = out / "effort.raw"
tracking = out / "tracking.raw"
document = {
    "label": "synthetic_runtime_admission",
    "mode": mode,
    "exit_code": status,
    "evidence": {name: path.name for name, path in {
        "joint_state": state, "effort": effort, "tracking": tracking}.items()},
    "verdict": "NO-GO",
    "fixture_admission": "UNVERIFIED",
    "baked_inputs": {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in {
        "runner": "/opt/lockwire/run-admission.sh", "packages": "/opt/lockwire/packages.lock",
        "robot": "/workspace/robot.urdf.xacro", "world": "/workspace/world.sdf",
        "controllers": "/workspace/controllers.yaml"}.items()},
    "control_msgs_version": subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", "ros-jazzy-control-msgs"], text=True),
    "controller_version": subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", "ros-jazzy-joint-trajectory-controller"], text=True),
}
# The host MotionJob reads these raw bytes through the shared strict verifier.
# A successful container command or disabled negative alone grants no admission.
fd, name = tempfile.mkstemp(prefix=".document.", dir=out)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(name, document_path)
finally:
    Path(name).unlink(missing_ok=True)
PY
}
trap finish EXIT

# Replacing the official image entrypoint must not silently omit ROS setup.
# This known Jazzy package path is checked, not assumed to exist in an image.
test -r /opt/ros/jazzy/setup.bash || { echo "motion admission: Jazzy ROS setup is missing" >&2; exit 78; }
set +u
source /opt/ros/jazzy/setup.bash
set -u

xacro /workspace/robot.urdf.xacro > /tmp/robot.urdf
gz sim -r /workspace/world.sdf >"$out/gz.raw" 2>&1 & sim_pid=$!
sleep 3
ros2 run ros_gz_sim create -name synthetic_gravity_pendulum -file /tmp/robot.urdf >"$out/spawn.raw" 2>&1
ros2 control load_controller --set-state active joint_state_broadcaster >"$out/controllers.raw" 2>&1

# Both evidence streams are raw timestamped observations. The enabled command is
# an effort-controller trajectory; state feedback, not its publication receipt,
# is what the later verifier evaluates.
extract_effort() {
  awk '/^effort:$/ { print; if (getline) print; print "---"; fflush() }'
}
timeout 10 ros2 topic echo /joint_states | stamp >"$out/joint_states.raw" & state_pid=$!
# Keep each effort key with its numeric scalar; a key-only receipt cannot prove
# that disabled mode had no actuation or that enabled mode applied effort.
timeout 10 ros2 topic echo /joint_states | extract_effort | stamp >"$out/effort.raw" & controller_pid=$!
if [ "$mode" = enabled ]; then
  ros2 control load_controller --set-state active joint_trajectory_controller >>"$out/controllers.raw" 2>&1
  ros2 topic pub --once /joint_trajectory_controller/joint_trajectory trajectory_msgs/msg/JointTrajectory \
    "{joint_names: [pendulum_joint], points: [{positions: [0.8], time_from_start: {sec: 3}}]}" \
    >"$out/command.raw" 2>&1
  timeout 6 ros2 topic echo /joint_trajectory_controller/controller_state | stamp >"$out/tracking.raw" || true
else
  # No controller activation: hold_joints:false in the fixture exposes gravity.
  sleep 6
  : >"$out/tracking.raw"
fi
wait "$state_pid" || true
wait "$controller_pid" || true
