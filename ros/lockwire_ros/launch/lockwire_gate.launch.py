"""Include this to put the lockwire gate in front of your own bringup.

    IncludeLaunchDescription(PythonLaunchDescriptionSource(<share>/launch/lockwire_gate.launch.py),
        launch_arguments={"contract": "lockwire.yaml", "live": "true",
                          "bringup": "<your bringup.launch.py>"}.items())

``bringup`` starts only after a GO; any refusal makes `ros2 launch` exit nonzero.
"""

from lockwire_ros.launch_gate import gate_launch_description


def generate_launch_description():
    return gate_launch_description()
