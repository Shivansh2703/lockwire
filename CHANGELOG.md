# Changelog

## 0.2.4 — unreleased

New in the free app and library:

- An assistant in the app: ask questions about your robot's lockfile. It is off by default, uses your own key, and asks for consent once per robot.
- `lockwire from-parts` (and `from_parts` in the library): paste a parts list and it is matched to the catalog; with `--out` it also writes a draft lockfile.
- Xacro files are expanded when you start from them, and `package://` mesh paths are searched in sibling packages.
- A new check: an arm's gravity torque against each joint's effort limit.
- A new check: a massless tool or gripper leaf link is flagged.
- A converter's output tolerance is checked against what the load needs.
- `lockwire check --strict` exits 1 on a `GO with notes`; the GitHub Action has a matching `strict` input.
- `lockwire check` lists the checks that passed after the verdict.
- glb and gltf files can be picked as the robot model.
- Gravity reach now scores kinematic trees: arms with grippers, each leg of a quadruped, and humanoid limbs with the base held. A link can carry an optional `payload` (a point mass at the link's origin); the lockfile version is unchanged and files without it stay valid. The starter robots gained centres of mass, so the arm scores all six joints, the quadruped twelve, and the humanoid 28 of 30.
- The assistant shows each proposed change as the diff the editor itself would make, with a visible, editable witness field; Apply stays off until it is filled. The assistant now docks beside the panel for the current mode, so Check findings stay visible.

Fixes you will notice:

- A joint declared continuous that is really a rocker is now caught.
- Closed kinematic chains no longer hang `check`; joint limits are sampled rather than assumed.
- A UTF-16 URDF is refused with a clear message instead of being misread.
- Writes never overwrite a lockfile unless you ask; `start --example` keeps your own .glb.
- Robot-mode (agent) calls can only reach the files they are allowed to.
- A mass budget with an unknown mass is "not scored" instead of summing the unknown as zero.
- Rail current is scored in every power mode, CAN bandwidth is computed on wire bits, and a low-side tolerance is checked.
- Placing a device rotates it into the frame it is placed in.
- Tool failures exit 2 with one line instead of a traceback.
- `review` accepts `--expected-revision`, and `~` is expanded in paths.
- The contract loader now refuses a joint whose parent or child is not a declared link, joint limits with lower >= upper or a negative velocity, and a tolerance rel_pct outside 0-100 or a string percent (exit 2, field named); a missing schema_version means the current version everywhere; an unquoted schema_version: 0.3 is accepted; schema_version is unchanged. Saving a lockfile (lockwire init, including --force) writes it atomically as UTF-8 keeping the file permissions; a forced re-init writes a fresh file and does not keep the old file's comments; declare/add/review still keep comments.
- The `lockwire.api` library docs now list every public function, and no longer show paid-only calls.
- Every command that writes `--out` or `--json` exits 2 with one line when the folder is missing; `from-parts --out` no longer creates missing parent folders. `scene import` refuses the lockfile itself as its changes file, and `health --json` stays inside the allowed folders.
- On Windows, lockfile edits keep the file's line endings, and the Windows CI job runs the whole edit test file.
- The helper that expands xacro files now runs with a minimal, allowlisted environment instead of yours.
- Editing a lockfile keeps its file permissions.

Under the hood: updated the MCP library and pyjwt for security advisories, Electron to 44.9.2, and npm patch releases. The Windows CI job now fails when tests fail, so its badge means what it says.

## 0.2.2 — 2026-10-16

Easier first run, and five starter robots. (0.2.1 was never published; its changes ship here.)

- Five starter robots, each built from public, sourced specs and each with a real problem to find: drone (X500), rover (TurtleBot3 Burger), arm (SO-101), quadruped (Solo-12) and humanoid (ToddlerBot). The app's first screen is "New robot": pick a starter or start blank.
- `lockwire start --example drone` and `lockwire start --example servo-bench` write a ready example lockfile; `lockwire check` then reaches a NO-GO in two commands. Needs Python 3.12 or newer. The old names `x500` and `pi-servos` still work.
- A parts catalog with sourced figures: pick a part, and its supply voltage wires itself; drag it onto the robot's body to place it.
- Fixed the Mac download: the zip is now clean and opens without the "damaged" error. A verify script (`scripts/verify_mac_zip.sh`) checks the zip before release.
- The GitHub Action installs its own code by default. It no longer pulls Lockwire from PyPI unless you pin a version.
- Quieter output: massless frames are folded into one line instead of one warning each.
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
