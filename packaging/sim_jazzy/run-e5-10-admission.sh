#!/usr/bin/env bash
# Runs only inside the fixed E5.10 sim-jazzy image. Host isolation (--network
# none, --read-only, --tmpfs /tmp /root) is owned by jobs.py's
# build_motion_argv, not this script.
set -euo pipefail

# document.json must exist even when nothing
# after this point ever runs -- a missing evidence_dir/output.txt/errors.txt
# read as a bare FileNotFoundError on the host with no clue why. `finish` is
# defined and trapped first thing, before scenario validation, before the
# fixture copy, before anything that can fail. It is defensive on purpose:
# a missing scenarios.json (its hash) or a failed atomic write both fall back
# rather than raise, so the trap itself can never be the reason the document
# is missing again.
out=/output
scenario=${1:-}
if [ "$scenario" = px4_x500_takeoff_hover_land ]; then
  evidence_json='{"ulog": "x500.ulg"}'
else
  evidence_json='{"joint_state": "joint_states.raw", "tracking": "tracking.raw", "controllers": "controllers.raw"}'
fi

finish() {
  status=$?
  for pid in ${sim_pid:-} ${state_pid:-} ${tracking_pid:-}; do kill "$pid" 2>/dev/null || true; done
  python3 - "$out/document.json" "$scenario" "$status" "${evidence_json:-null}" <<'PY'
import hashlib, json, os, sys, tempfile, traceback
from pathlib import Path

document_path, scenario, status, evidence_raw = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
out = document_path.parent
try:
    evidence = json.loads(evidence_raw)
except Exception:
    evidence = None
try:
    scenarios_sha = hashlib.sha256((out / "scenarios.json").read_bytes()).hexdigest()
except Exception:
    scenarios_sha = None
document = {
    "label": "e5_10_sim_jazzy_admission",
    "scenario": scenario,
    "exit_code": status,
    "evidence": evidence,
    "verdict": "NO-GO",
    "fixture_admission": "UNVERIFIED",
    "baked_inputs": {"scenarios": scenarios_sha},
}
try:
    fd, name = tempfile.mkstemp(prefix=".document.", dir=out)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, document_path)
    finally:
        Path(name).unlink(missing_ok=True)
except Exception:
    # Last resort: a plain write beats none. Whatever broke the atomic path
    # (still not writable, disk full, ...) is printed to stderr, which
    # docker run already forwards into the host's errors.txt evidence.
    traceback.print_exc()
    try:
        document_path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        traceback.print_exc()
PY
}
trap finish EXIT

fixtures=/opt/lockwire/e5_10_real_scenarios
case "$scenario" in
  anymal_d_effort_squat|anymal_d_controller_disabled_negative|px4_x500_takeoff_hover_land) ;;
  *) echo "scenario must be anymal_d_effort_squat, anymal_d_controller_disabled_negative or px4_x500_takeoff_hover_land" >&2; exit 64;;
esac
mkdir -p "$out"
cp "$fixtures/scenarios.json" "$out/scenarios.json"

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

test -r /opt/ros/jazzy/setup.bash || { echo "sim_jazzy: Jazzy ROS setup is missing" >&2; exit 78; }
set +u
source /opt/ros/jazzy/setup.bash
set -u
# Loopback-only DDS discovery inside --network none (never exercised before
# this cycle); a hang here would otherwise surface as an opaque 600s timeout.
export ROS_LOCALHOST_ONLY=1

if [ "$scenario" = px4_x500_takeoff_hover_land ]; then
  scratch=/tmp/sim_jazzy
  mkdir -p "$scratch"
  python3 /opt/lockwire/x500_runner.py prepare "$fixtures" "$scratch"
  # gz needs GZ_SIM_RESOURCE_PATH to resolve the x500 model's `model://x500_base`
  # include; the ANYmal path spawns a single merged URDF with no such include.
  export GZ_SIM_RESOURCE_PATH="$scratch/models"
  gz sim -s -r -v 1 "$fixtures/scenario_world.sdf" >"$out/gz.raw" 2>&1 & sim_pid=$!
  sleep 5
  ros2 run ros_gz_sim create -name x500 -file "$scratch/models/x500/model.sdf" \
    -x 0 -y 0 -z 0.1 >"$out/spawn.raw" 2>&1
  sleep 2
  python3 /opt/lockwire/x500_runner.py fly "$fixtures" /opt/px4-autopilot /tmp/px4_wd "$out"
  exit "$?"
fi

python3 /opt/lockwire/anymal_runner.py prepare "$fixtures" /tmp/anymal_merged.urdf
gz sim -s -r -v 1 "$fixtures/scenario_world.sdf" >"$out/gz.raw" 2>&1 & sim_pid=$!
sleep 5
ros2 run ros_gz_sim create -name anymal -file /tmp/anymal_merged.urdf -x 0 -y 0 -z 0.6 >"$out/spawn.raw" 2>&1
sleep 2
timeout 30 ros2 control load_controller --set-state active joint_state_broadcaster >"$out/controllers.raw" 2>&1
timeout 30 ros2 control load_controller --set-state active anymal_effort_controller >>"$out/controllers.raw" 2>&1

# Settle the collapsed spawn pose (hold_joints:false, no controller since
# spawn) at the fixture's nominal pose before any capture starts, so neither
# scenario's judged trace includes that transient.
python3 /opt/lockwire/anymal_runner.py hold-trajectory "$fixtures/scenarios.json" /tmp/hold.yaml
ros2 topic pub --once /anymal_effort_controller/joint_trajectory trajectory_msgs/msg/JointTrajectory \
  -f /tmp/hold.yaml >"$out/command.raw" 2>&1
sleep 5

if [ "$scenario" = anymal_d_effort_squat ]; then
  python3 /opt/lockwire/anymal_runner.py trajectory "$fixtures/scenarios.json" /tmp/squat.yaml
  timeout 20 ros2 topic echo /joint_states | stamp >"$out/joint_states.raw" & state_pid=$!
  timeout 20 ros2 topic echo /anymal_effort_controller/controller_state \
    | python3 -u /opt/lockwire/anymal_runner.py extract-tracking | stamp >"$out/tracking.raw" & tracking_pid=$!
  sleep 1
  ros2 topic pub --once /anymal_effort_controller/joint_trajectory trajectory_msgs/msg/JointTrajectory \
    -f /tmp/squat.yaml >>"$out/command.raw" 2>&1
else
  : >"$out/tracking.raw"
  timeout 10 ros2 topic echo /joint_states | stamp >"$out/joint_states.raw" & state_pid=$!
  sleep 1
  timeout 30 ros2 control set_controller_state anymal_effort_controller inactive >>"$out/controllers.raw" 2>&1
  sleep 2
  timeout 30 ros2 control list_controllers >>"$out/controllers.raw" 2>&1
fi
wait "$state_pid" || true
wait "${tracking_pid:-0}" 2>/dev/null || true
