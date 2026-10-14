#!/usr/bin/env bash
# Runs inside the sim-jazzy CI container only. Host isolation (--network none,
# --read-only /ws) is owned by .github/workflows/ci.yml, not this script.
#
# Expects:
#   /ws   the repo, read-only bind mount
#   /out  a writable bind mount for traces and JSON verdicts
#
# Produces in /out: network.json, assets_verify.json,
# x500_idle_pose.jsonl (E5.9 no-motion), x500_thrust_pose.jsonl,
# anymal_joint_states.raw, anymal_controller_state.raw (E5.10 motion).
set -euo pipefail

test -r /opt/ros/jazzy/setup.bash || { echo "run_e5_10: Jazzy ROS setup missing" >&2; exit 78; }
set +u
source /opt/ros/jazzy/setup.bash
set -u

exec python3 -u /ws/packaging/sim_jazzy/run_e5_10.py
