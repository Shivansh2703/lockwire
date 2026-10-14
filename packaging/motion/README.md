# Synthetic motion runtime admission

This is a runtime admission fixture, not a robot scenario and not physical
validation. Its only claim is `synthetic_runtime_admission`: a one-joint,
gravity-loaded Gazebo Harmonic plant has measured position, velocity and effort
feedback while an effort `joint_trajectory_controller` tracks a commanded path.
The synthetic assets are baked in; a run has exactly one host bind, its owned
output directory.

The disabled run starts no actuation and keeps `hold_joints: false`; it must show
physical evolution and must not claim tracking. A command receipt or a position
teleport is not evidence.

Stable Noble arm64 metadata is now measured: the base is
`ros:jazzy-ros-core@sha256:17142ce4cb561d70d61f4fb485177caf0d6cceca4ccc418704168ff45f3713e5`;
the signed ROS source is `http://packages.ros.org/ros2/ubuntu noble/main`, with
Open Robotics fingerprint `C1CF6E31E6BADE8868B172B4F42ED6FBAB17C654`. The
direct set is `gz_ros2_control`, `joint_trajectory_controller`, `ros_gz_sim`,
and `gz_sim_vendor` at the exact versions and hashes in `packages.lock`.
`ros-jazzy-ros-gz` was measured but is deliberately not installed: it is a
broader meta-package than the required simulation interface. The vendor pin is
Gazebo Harmonic (`gz-sim8 8.15.0`).

Before installation, the capture build requires its one declared direct
selector set to equal the `packages.lock` pins. It reads every downloaded
archive's Debian control metadata before classifying it: each direct package
must have one canonically named artifact whose control identity, arm64
architecture, byte size, and SHA-256 match its pin. Missing, duplicate,
renamed, extra or mismatched direct inputs stop the build; only control-package
names outside that exact set are scoped as transitive closure archives.

The earlier empty package-policy conclusion is retained as corrected history:
it queried the default apt list path after the update populated `/tmp/lists`.
The fixed probe passed the same list/cache paths to both update and queries.

`packages.lock` still deliberately blocks builds: transitive package versions,
hashes, notices/licenses, full installed `dpkg-query -W` closure, and final
image identity/size must be captured by the sanctioned online build. After it
records that closure, run the admission twice with Docker networking disabled,
then retain raw timestamped topic streams, document JSON, container identity,
hashes, resources and absence proof.

Primary API sources: <https://control.ros.org/jazzy/doc/gz_ros2_control/doc/index.html>
(`hold_joints`) and <https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/userdoc.html>
(measured feedback/action semantics). Those packages use Apache-2.0 licenses;
the measured lock must retain exact source and package license metadata.

The capture closure records the full installed identity as normalized
`Package`, `Version`, and `Architecture` sets. It batches exact
`package=version` selectors for `apt-cache show`, then fails closed unless the
returned normalized set equals `dpkg-query -W`'s normalized installed set. This
replaces the prior one-process-per-package query that exceeded its capture
window; it does not resolve, install, or float packages. Command semantics are
from Debian's primary [`dpkg-query(1)`](https://manpages.debian.org/bookworm/dpkg/dpkg-query.1.en.html)
and [`apt-cache(8)`](https://manpages.debian.org/bookworm/apt/apt-cache.8.en.html)
manuals.
