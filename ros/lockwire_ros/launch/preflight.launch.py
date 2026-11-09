"""Standalone bringup gate: check the robot, refuse the launch if it disagrees.

    ros2 launch lockwire_ros preflight.launch.py contract:=lockwire.yaml live:=true capture_s:=10
    ros2 launch lockwire_ros preflight.launch.py contract:=lockwire.yaml recording:=recordings/bench-idle

Exits nonzero on a refusal — see ``lockwire_ros.launch_gate`` for why that takes
deliberate effort (ros2/launch#586). Same arguments as ``lockwire_gate.launch.py``.
"""

from lockwire_ros.launch_gate import gate_launch_description


def generate_launch_description():
    return gate_launch_description()
