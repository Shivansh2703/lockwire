# Live-target image: ros:jazzy-ros-core plus bus-enumeration tools.
# Base is digest-pinned so upstream `jazzy-ros-core` tag moves do not silently
# change what the live suite runs against.
FROM ros:jazzy-ros-core@sha256:17142ce4cb561d70d61f4fb485177caf0d6cceca4ccc418704168ff45f3713e5

# The live path records the robot with `ros2 bag record -s mcap`, so the
# image needs rosbag2 with the MCAP storage plugin; sensor-msgs is needed by
# provision.py's IMU node, which publishes a real sensor_msgs/msg/Imu.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        usbutils iproute2 openssh-server \
        ros-jazzy-rosbag2 ros-jazzy-rosbag2-storage-mcap ros-jazzy-sensor-msgs \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh

# The owned-container saved-Test gate reads this identity through docker exec.
# The replay manifest declares the same independently authored six fields.
COPY selected-target.json /var/lockwire/selected-target.json
