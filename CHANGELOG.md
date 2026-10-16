# Changelog

## 0.2.2 — 2026-10-16

Easier first run, and five starter robots. (0.2.1 was never published; its changes ship here.)

- Five starter robots, each built from public, sourced specs and each with a real problem to find: drone (X500), rover (TurtleBot3 Burger), arm (SO-101), quadruped (Solo-12) and humanoid (ToddlerBot). The app's first screen is "New robot": pick a starter or start blank.
- `lockwire start --example drone` and `lockwire start --example servo-bench` write a ready example lockfile; `lockwire check` then reaches a NO-GO in two commands. Needs Python 3.12 or newer. The old names `x500` and `pi-servos` still work.
- A parts catalog with sourced figures: pick a part, and its supply voltage wires itself; drag it onto the robot's body to place it.
- Fixed the Mac download: the zip is now clean and opens without the "damaged" error. A verify script (`scripts/verify_mac_zip.sh`) checks the zip before release.
- The GitHub Action installs its own code by default. It no longer pulls Lockwire from PyPI unless you pin a version.
- Quieter output: massless frames are folded into one line instead of one warning each.
- A servo that declares only a peak (stall) torque is now checked against the joint's effort limit: if even the peak can't reach it, Check flags it, and Plan scores it the same way.
- The scored line is honest: warnings and model checks are counted in their own buckets, so the numbers add up.
- The robot name comes from the URDF and gets its own row. Parts, joints and rails read as plain names.
- One number format everywhere; rounding never hides a gap.
- WASD moves the camera in the 3D view, and the robot is framed in the open part of the window on every page.
- Connection views in Design: a Power, Data and Mechanical tree beside the robot, with the connections drawn on the robot; the overloaded rail shows red.
- Almost everything on the five pages is clickable and leads to the part, its numbers or its finding. Findings come with plain-word notes, and "Instead" lines say the move and the watts it frees.
- "Instead" advice never suggests a rail of a different voltage.
- Test and Run show as greyed Pro tabs.
- A new "Ways to use" page in Docs: CLI, app, GitHub Action, MCP for AI agents, plan/what-if, Python.
- The drone example's Jetson input is corrected to NVIDIA's carrier-board spec (5 V).
- A new logo and app icon.
- A Windows CI check. Windows CLI file locking is fixed.
- Cleaner `--help` output.
- A release workflow that only creates draft releases; publishing stays a manual step.
- Download the app from the latest release. macOS (Apple silicon): unzip and open. The first open shows a warning because the app is not notarized: System Settings, Privacy & Security, scroll down, click "Open Anyway".
- Linux: `chmod +x Lockwire-*.AppImage`, then run it. Ubuntu 22.04 needs `sudo apt install libfuse2`; Ubuntu 24.04 needs `sudo apt install libfuse2t64`.
- Windows app: not yet.

## 0.2.0 — the free desktop app

The app's planning half is now free.

- A desktop app for planning a robot before it is powered: Open a robot description, Design it,
  Declare what it must hold, Plan changes, and Check them against the contract. The first run
  needs no setup beyond opening your robot.
- Downloads for macOS (Apple silicon, unsigned) and Linux (x64 AppImage), plus the source to
  build them. Windows follows.
- The local server behind the app (`lockwire serve`) and the generated types (`lockwire api-types`,
  `lockwire api-schema`) are part of the free package.
- A CI job that packages the Linux app and drives it end to end.

Lockwire Pro keeps the powered preflight gate, recording analysis and health.

## 0.1.0 — first public release

- The physics lockfile: a checked-in YAML contract for a robot's devices, frames, power and
  signals, validated by a strict schema.
- Deterministic checks, no simulation needed: power, thermal, bus, link and torque budgets.
- A static check of each device's declared envelope against its limits.
- Commands to change a contract (`add`, `place`, `move`, `declare`, `apply`), read it back in
  plain words (`describe`), and score a plan or a proposed change before anything moves (`plan`,
  `what-if`).
- An MCP server so an agent can read the contract and score plans over MCP.
- A self-contained HTML report.
- A GitHub Action and a pre-commit hook to verify the lockfile in CI.

The powered preflight gate, recording analysis and the desktop app are Lockwire Pro.
