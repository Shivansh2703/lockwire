# Changelog

## Unreleased

## 0.2.6 — 2026-11-09

New:

- The formerly-Pro commands (`preflight`, `baseline`, `baseline-review`, `baseline-check`, `sample`, `health`, `actuator-review-draft`) are now open source under Apache-2.0 and install with `pip install lockwire`.
- `lockwire check-sim` compares a MuJoCo (MJCF) or SDF sim file with the lockfile, value by value: masses, centres of mass, inertia, joint limits, effort, damping, friction and armature. It reads MuJoCo `<default>` classes and multiplies actuator force range by gear. It never runs a simulation, and lists what the sim format cannot express. The MCP server has a matching `check_sim_config` tool.
- An edit hook for Claude Code: `lockwire agent-setup --claude` adds `lockwire hook-check` to the project's settings, run by the same Python that ran agent-setup (not whatever `lockwire` is first on PATH). It checks a proposed edit to a URDF, SDF or lockwire.yaml and blocks one that breaks the locked physics. If the check itself fails on such a file, or the plugin finds no working lockwire, the edit is blocked with one line saying why; edits to other files always go through.
- A Claude Code plugin in `plugin/`: the MCP server, the edit hook, a `/lockwire` skill and a read-only robot reviewer.
- Desktop app: an Agent tab in the side drawer works on the open robot with your own API key.
- Documentation: guides for the command line, CI, AI agents and every check, in `docs/guides/`.
- CI guard: `lockwire accept <finding> --reason ... --witness ...` records a known gap (say, a datasheet mass that counts the battery) in the lockfile's new `accepted:` list; `check` shows it as accepted and fires again if the value changes. `check --adopt <prefixes>` annotates without failing, and `check --baseline` fails only on new findings. An edit to the model after it was locked can never be accepted, adopted or baselined. Lockfiles that use `accepted:` need this Lockwire or newer; older versions refuse them.
- `lockwire check go2` checks a known robot by name: with `pip install 'lockwire[robots]'` it fetches the robot's published URDF and checks it, comparing its mass with the vendor's datasheet when the robot is in the catalog.
- ROS 2 bags: `lockwire bag inspect` lists a bag's topics and fields, and `lockwire bag check` runs preflight on it (`pip install 'lockwire[ros2bag]'`; MCAP bags; `--example` uses a sample bag, no robot needed). Signal fields can index arrays, `effort[0]`, or name a joint, `position[name=knee]`, so `sensor_msgs/JointState` works even when a driver reorders its joints.
- ROS 2 live gate in one command: `ros2 run lockwire_ros preflight lockwire.yaml --seconds 10` captures from the running robot and exits with the verdict (0 GO, 1 NO-GO, 2 could not run). `lockwire_gate.launch.py` can be included in a launch file to gate it.
- The pre-commit hook also runs when a URDF, xacro or SDF file changes, not only `lockwire.yaml`.
- Desktop app: the Check screen opens on the first problem, findings accepted on record show in a muted Accepted section, and Compare with datasheet sits on the verdict card. A new open screen, a fixer for a missing mesh folder, trackpad pan and pinch, and Hide panels to see the whole robot.
- `lockwire start <robot name>` copies the robot's model beside the lockfile, so the folder holds everything the lock needs.
- `lockwire agent-setup --ci` writes only the CI workflow, `.github/workflows/lockwire.yml`.
- Desktop app: the Assistant takes your key and the permission to act in one tap.
- `lockwire check robot.urdf` works on a model file alone, with no lockfile: it opens the URDF or xacro, runs the model checks and prints a verdict. Add `--mass` (with `--source` and `--tolerance-pct`) to compare the model's total mass with a datasheet figure. `lockwire start` now ends by running that check (`--no-check` skips it), and the MCP server has a matching `check_model` tool.
- Ten new checks that need no setup: URDF sanity, inertia validity, placeholder inertia, joint-limit unit mistakes, mass against size, battery shipping class, cold-derating coverage, reserved I2C addresses, a controller limits yaml against the URDF, and a watchdog marker. They run on URDF, xacro and SDF models.
- A datasheet catalog of popular robots, each row with its source. `check` recognises a known robot by `--model`, by its name or by its link names, and compares the model's mass with the vendor's figure. `lockwire catalog robots` lists them; the MCP server has `list_robots`.
- For AI coding agents: a one-line MCP install, `uvx --from "lockwire[mcp]" lockwire mcp`, and `lockwire agent-setup`, which gives an agent the three Lockwire rules, a pre-commit hook and a CI workflow.
- In CI: `lockwire check --format github` prints pull-request annotations and a job summary. The GitHub Action gains `adopt` (annotate but never fail the job), `evidence`, `evidence-root` and `package-root` inputs.
- `lockwire check --json -` prints exactly one JSON document on stdout (the human report goes to stderr). Each finding carries `next_command`, and `next_tool` for agents: what to run next.
- `start --xacro-arg name=value` passes arguments to a xacro model; `.urdf` files that are really xacro templates are expanded.
- SDF models now load like URDF: `<include>` sub-models are followed, ball joints are read, and the lockfile's values are compared against the SDF file.
- Compatibility: lockfiles gain a `ball` joint type. Older lockfiles still load in 0.2.6, but a lockfile containing a ball joint will not load in 0.2.5 or older.
- SDF models get the same limit, controller-limit and shape checks as URDF, and `<include>`d parts are found on the Gazebo model path (`GZ_SIM_RESOURCE_PATH`, `GAZEBO_MODEL_PATH`) and drawn in the 3D view.
- `lockwire check --servo feetech.sts3215` (or a `servo:` line in the lockfile, one id for every joint or per joint pattern, `servo: {"arm_*": feetech.sts3215}`) compares every joint's effort limit with that servo's stall torque from its datasheet; the catalog has the Feetech STS3215 and ST3215 and the Dynamixel XL430-W250. The desktop app colours the over-limit joints.
- Inertia a link's own collision shape cannot hold is flagged (a thin standoff with a placeholder inertia), and the size checks now run on xacro models too.
- Desktop app: a tiny flagged part gets a halo so it stays visible inside the robot.
- Compatibility: lockfiles gain an optional `servo:` line. A lockfile using it will not load in 0.2.5 or older.

Changed:

- `lockwire --help` lists a short set of commands; `lockwire help --all` lists every one.
- Hints and next commands say `uvx lockwire` when your shell can't reach `lockwire` directly.
- Writing a lockfile leaves no `.bak` or `.lock` file beside it: the write is an atomic replace and its lock lives in the temp folder. Inside git, git is the undo.
- Desktop app: the verdict is never covered by a notice, examples open without a file dialog, a robot's package folder is found by itself, and a robot opens on Check, Docs and More. Update from model file shows only when the model has drifted.
- The MCP server describes every tool parameter and lists findings worst first, with smaller replies.
- `lockwire check` prints a shorter first verdict: each violation in full, the counts, and what was not compared. Warnings and the checks that passed print with `--all` (and always with `--strict` or `--format github`). `--json` is unchanged.
- **Breaking: lockfile `schema_version` 0.5.** A URDF `<axis/>` with no `xyz` now reads as `1 0 0`, as the URDF spec says (0.4 and older read `0 0 1`). Older lockfiles still load. `lockwire check` on a 0.4 lockfile warns about each joint it recorded the old way instead of reporting drift; re-lock to record the spec axis. `lockwire upgrade` now also takes a 0.4 lockfile and prints the same warning.
- Gravity reach counts devices mounted on links: a device whose `mount_frame` is a link adds its mass at `mount_xyz`. The finding says how many device masses it includes, and names the devices it could not count (no mass, or a frame that is not a link).
- `lockwire check-sim` prints one line when the sim file agrees with the lock (`--verbose` for every value) and finds the lockfile beside the sim file, so it fits as a step before a sim launch.
- Every bundled starter (drone, rover, arm, quadruped, humanoid) opens GO with sourced figures; the gaps that remain are accepted on record. servo-bench is the one example built to fail, and says so.

Verdicts tell the whole truth:

- Editing a link's mass, centre of mass or inertia in the model after the lockfile was written is caught.
- Editing, renaming, rescaling or deleting a mesh file after the lockfile was written is caught. Lockfiles written by this version record mesh files and scales and need this Lockwire or newer; older versions refuse them. Existing lockfiles still load and keep their verdicts.
- Every verdict lists what was not compared yet. `--max-not-scored N` exits 3 when more than N checks went unjudged; `--strict` exits 3 on any.
- A crash exits 2 with one line instead of a traceback, and a plugin that fails to load says so.
- Reversed joint limits and a NaN or negative mass become plain findings instead of errors.
- Arm gravity torque is the worst case over each joint's range, and prismatic joints are scored.
- A `mass_total` budget compares with a small tolerance instead of exact float equality; a thermal limit is met when equal to it; UART and I2C bus load counts framing bits.
- A model that can no longer be read since it was locked is a violation, not a pass.
- The report page's empty-state card is no longer hidden behind the SDF note.

## 0.2.5 — 2026-10-28

- A side drawer in the app, opened with the Assistant button: the Assistant moves into it, and a Terminal tab gives you a real shell that starts in your robot's folder.
- The drone, arm, quadruped, rover and humanoid starters declare sourced sensor limits.
- A `mass_total` budget can declare an optional `nominal` mass and `tolerance_pct`; existing lockfiles stay valid.
- URDF mimic joints with zero limits load again.
- The file-mode test runs only on macOS and Linux, where file modes exist, so Windows CI is green again.
- Fixed: a URDF whose fixed joint carries `lower="0" upper="0"` limits (Kinova's gen3_lite.urdf, for one) opens again. Limits are only checked on moving joints.

## 0.2.4 — 2026-10-21

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
