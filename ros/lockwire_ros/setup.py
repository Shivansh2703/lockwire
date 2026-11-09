from setuptools import setup

package_name = "lockwire_ros"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", ["launch/preflight.launch.py", "launch/lockwire_gate.launch.py"]),
    ],
    # No install_requires: every dependency is a ROS package declared in
    # package.xml, and the lockwire core is deliberately NOT a Python dependency
    # of this package — it is invoked out of process so this package can run on
    # Humble's Python 3.10. See lockwire_ros/core.py.
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Shivansh",
    maintainer_email="shivansh@lockwirerobotics.com",
    description="The lockwire preflight gate as a ROS 2 lifecycle node.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "gate_node = lockwire_ros.gate_node:main",
            "capture_node = lockwire_ros.capture_node:main",
            "relay_node = lockwire_ros.relay_node:main",
            "preflight = lockwire_ros.preflight_cli:main",
            "monitor = lockwire_ros.monitor_node:main",
        ],
    },
)
