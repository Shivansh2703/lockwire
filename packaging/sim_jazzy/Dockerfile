# CI-only image for E5.9/E5.10 (the simulator runs on GitHub, never on the
# owner's Mac). Deliberately separate from packaging/motion/Dockerfile: that file
# is arm64-pinned, guarded behind an authorized-capture build, and bakes an
# unrelated synthetic-pendulum admission fixture via a fixed ENTRYPOINT. This
# image instead mounts the live repo read-only at /ws and runs
# packaging/sim_jazzy/run_e5_10.sh, which drives the real
# tests/fixtures/e5_10_real_scenarios/ fixtures (X500, ANYmal).
#
# ubuntu-latest GitHub runners are amd64, so the base and every apt package
# below are pinned to their amd64 build/digest, fetched directly from
# registry-1.docker.io and packages.ros.org on 2026-09-28 (measured, not
# assumed — see the worker report for how).
#
# ponytail: this drops the elaborate dpkg-deb/apt-cache-show receipt
# machinery in packaging/motion/Dockerfile. That machinery exists there to
# make packages.lock a physical-assumption-grade lockfile for a shipped
# product gate; this image only needs to fail loudly if a CI runner's apt
# mirror serves a different byte-for-byte package, which `apt-get install
# pkg=version` plus the base digest pin already does. Upgrade path: reuse
# the motion Dockerfile's verify-then-install Python block here if this
# image is ever promoted to a lockfile-grade artifact.
FROM ros:jazzy-ros-core@sha256:ff387faa0c8fe671e57ff948e79ea3f06b6446fdf3483c18534b02b797801d9f

# Cycle 1: ANYmal only, no PX4 build. LOCKWIRE_PX4=1 (cycle 2) turns the
# clone/setup/make RUNs below back on; Docker has no conditional instructions,
# so each is wrapped in a shell `if`, always run, no-op when the arg is 0.
ARG LOCKWIRE_PX4=0

# Same four direct packages as packaging/motion/Dockerfile (gz Harmonic +
# ros2_control over gz-ros2-control), amd64 versions instead of arm64, plus
# joint-state-broadcaster: not a transitive dependency of the other four
# (checked against packages.ros.org noble/binary-amd64, 2026-09-28) and the
# ANYmal judge's evidence needs it publishing /joint_states.
# No ros-jazzy-ros-gz meta package: it pulls bridge/demo/image extras this
# job does not use.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
      ros-jazzy-gz-ros2-control=1.2.20-1noble.20260905.083607 \
      ros-jazzy-joint-trajectory-controller=4.42.1-1noble.20260905.073033 \
      ros-jazzy-ros-gz-sim=1.0.24-1noble.20260905.085151 \
      ros-jazzy-gz-sim-vendor=0.0.13-1noble.20260905.073557 \
      ros-jazzy-joint-state-broadcaster=4.42.1-1noble.20260905.074155 \
      python3-yaml \
 && rm -rf /var/lib/apt/lists/*

# X500 flies on the stack its fixture pins -- PX4 SITL, not a hand-written
# motor loop. tests/fixtures/e5_10_real_scenarios/sources.json pins PX4-Autopilot
# release v1.16.2 (commit 54f0455...); that release's own docs pair it with
# Gazebo Harmonic on Ubuntu 24.04 (docs.px4.io/v1.16/en/sim_gazebo_gz), which is
# exactly the gz version the ros-jazzy-*-vendor packages above already provide.
# Built here, at `docker build` time (network available); the run step stays
# --network none. --no-sim-tools skips PX4's own Gazebo/jMAVSim install -- a
# second gz-harmonic from OSRF's apt alongside the ROS vendor one is ABI-
# incompatible, not just redundant. --no-nuttx: this image only ever runs
# px4_sitl_default.
#
# libopencv-dev: src/modules/simulation/gz_plugins/optical_flow/{optical_flow.cmake,
# CMakeLists.txt} both `find_package(OpenCV REQUIRED ...)` -- --no-sim-tools
# skips ubuntu.sh's own OpenCV-adjacent gazebo_packages, and nothing else in
# this image pulls libopencv-dev in, so px4_sitl_default's cmake configure
# fails there (build 36510985618, "Could not find a package configuration
# file provided by OpenCV") before it ever reaches the other three gz_plugins.
# Checked every find_package/pkg_check_modules across all four plugins
# (optical_flow, template_plugin, gstreamer, moving_platform_controller) at
# v1.16.2: the other three only need the gz-transport/gz-sim/gz-sensors/
# gz-plugin components the ros-jazzy-*-vendor packages above already provide
# (all four confirmed "-- found" in that same build log before the OpenCV
# failure). gstreamer's plugin uses `pkg_check_modules(... gstreamer-1.0)`
# without REQUIRED -- PX4's own CMakeLists.txt treats it as optional (WARNING,
# not a configure error, and gz_plugins' ALL target only depends on
# GstCameraSystem `if (TARGET GstCameraSystem)`); the X500 takeoff/hover/land
# scenario needs no camera streaming, so it's left out on purpose rather than
# adding libgstreamer1.0-dev/libgstreamer-plugins-base1.0-dev for a plugin
# nothing here exercises.
RUN if [ "$LOCKWIRE_PX4" = 1 ]; then \
      apt-get update \
   && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        sudo git ca-certificates build-essential cmake ninja-build \
        python3-pip python3-venv python3-dev pkg-config rsync libopencv-dev \
   && rm -rf /var/lib/apt/lists/* ; \
    fi

# build 36512201335 (measured): px4_sitl_default's configure step ran PX4's own
# src/lib/version/px_update_git_header.py, which crashed IndexError generating
# build_git_version.h. Read that script at v1.16.2 (github raw) to see why: its
# *top-level* `git describe --tags` call is unguarded but harmless on a shallow
# tag clone (`--branch v1.16.2` already creates a local `refs/tags/v1.16.2` at
# the checked-out commit, so describe resolves with zero distance); the one
# unguarded `[...][-1]` index is in its NuttX section
# (`platforms/nuttx/NuttX/nuttx`), which only runs `if
# os.path.exists('platforms/nuttx/NuttX/nuttx/.git')` -- true here because
# --recurse-submodules checks that submodule out too, even though
# px4_sitl_default (no NuttX firmware) never needs it. A shallow submodule
# clone has no tags, so `re.findall(r'nuttx-...')[-1]` on an empty list is
# exactly the crash. Fix: remove the NuttX submodule outright so
# px_update_git_header.py's own os.path.exists check skips that section --
# the actual crash site.
#
# Deliberately no `git fetch --unshallow --tags` here any more (build
# 36523151150): it tried to unshallow every submodule too and choked with
# "Errors during submodule fetch" / "not our ref" / "cd to 'pymavlink'
# failed" on a shallow submodule clone. It was also never load-bearing --
# re-read px_update_git_header.py at v1.16.2 line by line: every git call it
# makes is either `--always` (never fails), wrapped in its own try/except
# (symbolic-ref, the oem-tag describe), a plain `rev-parse --verify HEAD`
# (works at any depth), or the NuttX block this RUN already deinits. The one
# unguarded call, `git describe --exclude ext/* --always --tags --dirty
# [--exact-match]`, resolves HEAD to v1.16.2 with zero distance from the
# clone's own local tag ref -- no unshallow needed.
RUN if [ "$LOCKWIRE_PX4" = 1 ]; then \
      git clone --branch v1.16.2 --depth 1 --recurse-submodules --shallow-submodules \
        https://github.com/PX4/PX4-Autopilot.git /opt/px4-autopilot \
   && cd /opt/px4-autopilot \
   && test "$(git rev-parse HEAD)" = "54f0455ffcd755534539a7cf33a09a20bf71d29d" \
   && git submodule deinit -f -- platforms/nuttx/NuttX/nuttx \
   && rm -rf .git/modules/platforms/nuttx/NuttX/nuttx ; \
    fi

# ubuntu.sh's own `pip install -r requirements.txt` (numpy>=1.13, unpinned)
# tries to upgrade the numpy Debian already installed as python3-numpy (a
# ros-jazzy-*-vendor dependency); pip's uninstall step then fails with
# "Cannot uninstall numpy ..., RECORD file not found" because dpkg-installed
# files carry no pip RECORD. PIP_IGNORE_INSTALLED makes pip install the
# newer wheel alongside/over the apt one instead of uninstalling it first --
# scoped to this RUN only, so it doesn't affect any other pip call in the
# image. Debian's python3-numpy itself is untouched (ROS depends on it).
RUN if [ "$LOCKWIRE_PX4" = 1 ]; then \
      export PIP_IGNORE_INSTALLED=1 \
   && bash /opt/px4-autopilot/Tools/setup/ubuntu.sh --no-nuttx --no-sim-tools \
   && pip3 install --break-system-packages --no-cache-dir pymavlink ; \
    fi

# CMAKE_PREFIX_PATH for the gz-*/gz-transport13 etc. cmake configs comes from
# sourcing ROS's setup.bash (the ros-jazzy-*-vendor packages install them under
# /opt/ros/jazzy), which is why this runs inside a `.`-sourced shell rather than
# a plain RUN.
RUN if [ "$LOCKWIRE_PX4" = 1 ]; then \
      /bin/bash -lc 'set -euo pipefail; \
        set +u; source /opt/ros/jazzy/setup.bash; set -u; \
        cd /opt/px4-autopilot && make px4_sitl_default' ; \
    fi

# Cycle 1's baked runner + fixtures (Lockwire's own jobs.py launches and
# judges this; no repo mount at run time). Build context is the repo root
# (ci.yml), so these paths are relative to it.
COPY packaging/sim_jazzy/run-e5-10-admission.sh /opt/lockwire/run-e5-10-admission.sh
COPY packaging/sim_jazzy/anymal_runner.py /opt/lockwire/anymal_runner.py
COPY packaging/sim_jazzy/x500_runner.py /opt/lockwire/x500_runner.py
COPY tests/fixtures/e5_10_real_scenarios /opt/lockwire/e5_10_real_scenarios
RUN chmod +x /opt/lockwire/run-e5-10-admission.sh

ENTRYPOINT ["/bin/bash"]
