# Upstream fixes

Errors Lockwire found in public robot models, and the pull requests sent to fix them in their
own repositories. States were read from GitHub on 2026-11-12; dates are UTC.

| Project | What was off | Link | State | Date |
| --- | --- | --- | --- | --- |
| OnRobot ROS 2 grippers | URDF link masses below the datasheet weights: RG2 0.717 vs 0.78 kg, RG6 1.095 vs 1.25 kg, 3FG15 1.035 vs 1.15 kg (-8 to -12 %) | [onrobot-ros2#13](https://github.com/OnRobot/onrobot-ros2/pull/13) | open | opened 2026-11-02 |
| Robotec.ai OTTO robots gem | OTTO 600 chassis 512 kg against the 250 kg spec sheet; OTTO 1500 357 kg against 627 kg; placeholder 1.0 inertias | [o3de-otto-robots-gem#24](https://github.com/RobotecAI/o3de-otto-robots-gem/pull/24) | open | opened 2026-11-02 |
| RealSense ROS | Every D4xx camera xacro carried the same placeholder 0.072 kg; the datasheet gives 0.058 to 0.116 kg | [realsense-ros#3589](https://github.com/realsenseai/realsense-ros/pull/3589) | accepted: a maintainer committed it as [`b30f4eac`](https://github.com/realsenseai/realsense-ros/commit/b30f4eac) on the `ros2-development` branch (the PR page reads closed) | committed 2026-11-04 |
| AUBO i5 | The description URDF sums to 9.883 kg; the Gazebo model of the same arm sums to 24.724 kg | [aubo_robot#115](https://github.com/AuboRobot/aubo_robot/pull/115) | open | opened 2026-11-04 |
| Seeed reBot arm (URDF) | reBot_B601_DM.urdf link masses summed to 2.74 kg against 4.93 kg in the DevArm DM file | [reBot-Isaacsim#27](https://github.com/Seeed-Projects/reBot-Isaacsim/pull/27) | merged | 2026-11-04 |
| Seeed reBot arm (USD) | physics.usda link masses summed to 2.74 kg against 4.93 kg in the DM URDF | [reBot-Isaacsim#28](https://github.com/Seeed-Projects/reBot-Isaacsim/pull/28) | merged | 2026-11-05 |
| ROBOTIS OpenMANIPULATOR-X | URDF link masses sum to 0.5941 kg against the 0.70 kg specification (-15.1 %); adds a weight check | [open_manipulator#396](https://github.com/ROBOTIS-GIT/open_manipulator/pull/396) | open | opened 2026-11-04 |
| ROBOTIS TurtleBot3 | Waffle Pi URDF sums to 1.5539 kg against the 1.8 kg e-Manual weight (-13.7 %); adds a weight check | [turtlebot3#1156](https://github.com/ROBOTIS-GIT/turtlebot3/pull/1156) | open | opened 2026-11-05 |
| MiR100 (DFKI mir_robot) | Model sums to 84.2 kg against the 67 kg data sheet weight without load | [mir_robot#148](https://github.com/DFKI-NI/mir_robot/pull/148) | merged | 2026-11-07 |
| SO-101 arm (TheRobotStudio) | Every joint limit set to 10 N·m and 10 rad/s; the STS3215 7.4 V servo is rated 1.91 N·m stall and 5.45 rad/s (about 5x too strong) | [SO-ARM100#195](https://github.com/TheRobotStudio/SO-ARM100/pull/195) | open | opened 2026-11-11 |
| Fashion Star Star-Arm 102-FL | URDF link masses sum to 485 g against the 865 g assembled arm | [Star-Arm-102#13](https://github.com/servodevelop/Star-Arm-102/pull/13) | open | opened 2026-11-11 |
