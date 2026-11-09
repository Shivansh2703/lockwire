#!/usr/bin/env bash
# Run the lockwire_ros suite inside the Humble gate image.
#
#   docker build -t lockwire-humble:gate -f tests/fixtures/humble/Dockerfile \
#       tests/fixtures/humble
#   docker run --rm -v "$PWD":/ws:ro \
#       -v "$PWD/tests/fixtures/humble/run_suite.sh":/run.sh:ro \
#       lockwire-humble:gate bash /run.sh
#
# Why this script exists rather than a one-line docker command: three things have
# to be true at once for the run to mean anything, and each has already been got
# wrong once.
#
#  1. PYTHONPATH is *prepended*, never replaced. Overwriting it hides rclpy, and
#     the rclpy-gated cases then SKIP — a run that printed "25 passed" while the
#     load-bearing lifecycle tests never executed. A skip is not a pass.
#  2. The repo is copied to /work rather than used at the read-only /ws mount,
#     because installing the core writes build artifacts.
#  3. LOCKWIRE_REAL_EXECUTABLE points at the genuine core in its own 3.12 venv,
#     which is what turns the live-path cases from skipped to executed.
#
# -rs prints skip reasons, so a silent skip cannot be mistaken for coverage.
set -eo pipefail

# `set -u` must be OFF across the ROS setup script: it reads AMENT_TRACE_SETUP_FILES
# and friends unguarded, so a strict shell dies on line 8 before ROS is even on the
# path. Turn it on afterwards, where it protects our own variables.
set +u
source /opt/ros/humble/setup.bash
set -u

mkdir -p /work
cp -r /ws/. /work/
cd /work

# The core into its own >= 3.12 environment — the arrangement under test.
uv pip install --python /opt/lockwire-venv/bin/python --quiet .

export LOCKWIRE_REAL_EXECUTABLE=/opt/lockwire-venv/bin/lockwire
export LOCKWIRE_CORE_PYTHON=/opt/lockwire-venv/bin/python
export LOCKWIRE_REPO_ROOT=/work
export PYTHONPATH=/work/ros/lockwire_ros:${PYTHONPATH:-}

echo "node interpreter:  $(python3 --version)"
echo "core interpreter:  $(/opt/lockwire-venv/bin/python --version)"
echo "core version:      $(/opt/lockwire-venv/bin/lockwire --version)"
echo

set +e
python3 -m pytest -q -p no:cacheprovider -rs "${@:-/work/ros/lockwire_ros/test/}" \
    2>&1 | tee /tmp/lockwire-ros-suite.log
status=${PIPESTATUS[0]}
set -e
[ "$status" -eq 0 ] || exit "$status"

# Inside this container every case has what it needs — rclpy, ros2, and a real
# core — so a skip here is not "not applicable", it is a case that quietly did
# not run. This guard exists because an early run of this very suite reported
# "25 passed" while every rclpy case skipped on a clobbered PYTHONPATH. Green
# and complete are different claims, and only this line checks the second one.
if [ "${LOCKWIRE_REQUIRE_NO_SKIPS:-0}" = "1" ]; then
    if grep -qE '[0-9]+ skipped' /tmp/lockwire-ros-suite.log; then
        echo "FAIL: cases skipped inside the container — see reasons above" >&2
        grep -E '^SKIPPED' /tmp/lockwire-ros-suite.log >&2 || true
        exit 1
    fi
    echo "no-skip guard: clean"
fi
