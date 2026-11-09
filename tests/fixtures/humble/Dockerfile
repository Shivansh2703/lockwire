# The Humble gate image: the binding constraint for `ros/lockwire_ros/`.
#
# Humble ships Python 3.10 (REP 2000) and is supported to May 2027. lockwire's
# core needs >= 3.12, so this image is where the package's whole reason for
# crossing a process boundary gets proven rather than asserted: the node runs on
# the distro's 3.10, and the core it invokes is a separate >= 3.12 interpreter.
#
# What passes here passes on Jazzy/Kilted/Lyrical too — those are 3.12+, a
# strictly easier target — so Humble is the one image that must exist. The
# existing `ros:jazzy-ros-core` image under tests/fixtures/live/ stays for
# B105's stage-0/1 suite and is not replaced by this.
#
# Base is digest-pinned so an upstream `humble-ros-core` tag move cannot silently
# change what this suite ran against — same rule as tests/fixtures/live/Dockerfile.
FROM ros:humble-ros-core@sha256:5e1a11c7e8a4044f382ec13cdeda6fd414d02ccf251a382f5627fae8c2247f81

# python3-venv/pip: the core is installed into its own >= 3.12 environment, which
# is the arrangement under test. python3-pytest runs the package's own suite on
# the distro's 3.10 — the point being that the same assertions that pass on the
# dev machine's 3.12 must pass on the interpreter Humble actually ships.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-venv \
        python3-pytest \
        python3-yaml \
    && rm -rf /var/lib/apt/lists/*

# uv supplies the >= 3.12 interpreter Ubuntu 22.04 does not have. This is not a
# convenience: without it the suite could only ever run the gate against a fake
# `lockwire`, and "the live path is untested" would have to be written on the
# commit. With it, the real core runs beside the 3.10 node exactly as it would on
# a team's robot. Digest-pinned like every other base here.
COPY --from=ghcr.io/astral-sh/uv@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv

# Build the core's environment at image-build time so the test run is not paying
# for an interpreter download inside its own timeout — the mistake B105 already
# made once with a 758 MB base pull.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.12 && uv venv --python 3.12 /opt/lockwire-venv
ENV LOCKWIRE_VENV=/opt/lockwire-venv
