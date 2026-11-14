# Changelog

## 0.5.0 — 2026-11-14

### Get your robot in
- `lockwire check robot.wbt` reads Webots robots (.wbt, .proto), and Webots files get the same model checks as a URDF.
- Webots files work in the edit hook and the MCP tools; the 3D scene says plainly that it needs a URDF, SDF or xacro model.
- A lock started from a MuJoCo model (`model.xml`) gets structure drift, sanity checks and a mass, centre of mass and inertia comparison like a URDF; `check` says when MuJoCo works out the mass itself and names what is not compared (joint anchors, sites, cameras, lights). Bodies, joints and inertials inside a `<frame>` are read as MuJoCo reads them; a frame rotated with `axisangle`, `xyaxes` or `zaxis` is refused.
- Big robot models are no longer drawn with holes: models up to two million triangles are shown whole, larger ones are simplified per part and the view says so ('Checks use the full model').
- `lockwire add link LOCK --name N --mesh PATH --scale mm|m|NUMBER` imports an STL or DAE part. The unit is required and never guessed; the mesh lives inside the lockfile's folder and the lockfile keeps the scale.
- Design a robot from an empty folder: `lockwire start --blank`, then `add link --shape --material` and `add joint --origin`.
- `lockwire rename KIND OLD NEW` renames a part and every reference to it in one write.
- The README's first run checks your own robot file in one command, then a small example arm with power and wiring reaches a real catch.

### Checks
- Joint limits that disagree between URDF, MJCF and SDF versions of the same robot are reported.
- `check --compare-to <base lockfile>` lists only the checks whose verdict changed, as GitHub annotations with `--format github`.
- `check --format csv` or `--format tsv` writes the findings as a table, and `--quiet` prints only the verdict line (the exit code is unchanged).
- `lockwire check` runs every declared variant of a lockfile and exits with the worst result.
- Identical findings and not-compared reasons collapse into one line with a count (like '12 checks: velocity limit ...'), in the text output of `check` and `report` too.
- When `--strict` changes the result, `check` prints the new word (NOT SCORED or NO-GO) with "under --strict"; exit codes are unchanged.
- `lockwire check --plan PATH` checks hold torque at each Plan pose.
- `lockwire plan` warns which tip targets (`requirements[].reach_m`) the arm cannot reach and by how much.
- `lockwire plan` warns when a segment's duration is shorter than the joints' velocity and acceleration limits allow.
- Check shows a bar of each value against its limit, and the saved report carries the same bars.
- In Declare, a changed number previews the verdict a moment after you stop typing, without saving.
- Overlaps panel finds parts that overlap or sit too close, also through each joint's range; pairs you have ignored stay quiet.
- Wire drop and connector heat are checked from the parts catalog.
- Thin wires are checked too: wires of gauges the NEC table lacks (such as 28 AWG) are compared with the published chassis-wiring rating and warn when overloaded; the arm-power example ships with that warning on its 28 AWG servo feed.
- A zero or negative rail limit, rail voltage, battery voltage or battery mass is reported in one plain line instead of crashing check; tip margin says why it was not computed.
- Deleting a part removes it with its joint, and a floating part that has mass is a warning.
- Finding titles in the app state the catch with both numbers (for example the brownout, the fuse, the drifted mass).
- The app says so when a GO has nothing physical compared, and what to do next.
- SDF robots get the same no-inertia warning as URDF robots, because the check now reads each link's mass and inertia from the SDF.

### Power
- Battery sag is worked out from the pack's own voltage curve and resistance, including the cold case, and warns when a rail would drop below its minimum near empty.
- Build a battery pack from a cell (series x parallel: voltage, Ah, mass, max current) and warn when a timed plan needs more energy than the pack holds after its reserve.
- A Power panel lists each rail's volts, amps, efficiency and heat lost, plus light, typical and worst-case load per rail, parts against their ratings, and the battery pack.
- Power budget: light, typical and max load per rail, with a CSV load list (Plan duty when given).
- Stress list: each catalog-linked part's current, voltage and junction rise as a share of its own rating; over-rating warns, 80 to 100% is advice.

### Controls
- Each motor is checked for Kv versus Kt agreement, encoder rate against top speed, and wheeled bases for tyre slip, as warnings only and only when the fields are present.
- Controls panel shows each motor's card with efficiency and speed curves, and each joint's step response from declared gains, rotor inertia and gear ratio.
- Phase margin, crossover against loop rate (with half-sample delay) and step effort against stall torque, as advice.

### Wiring
- A Wiring panel draws each board with its pins and every wire, lights up a pin's whole net, shows a pin card per board (CSV export), and lets you click two pins to add a wire.
- While wiring, a wire that would short a supply to ground is flagged before it is saved.
- Pin rules: voltage-level mismatch, 5 V into 3.3 V inputs, output-to-output (also across several wires), per-pin and per-board current from 12 board pin cards. A power input pin is judged against its own voltage range, not the card's signal limit, so 5 V into an ESP32 5V pin no longer warns (a GPIO pin at 5 V still does).
- The saved report gains a printable wiring build sheet with a clickable diagram.
- `lockwire describe` shows the bus address map.
- `lockwire add` takes rail `--dropout/--min-input/--regulator-kind`, bus `--length/--twisted`, battery `--cells-parallel/--reserve`, with units; `add wire --awg --installation --length --net-class --connector --from --to`; `add connector --device --type [--placement]` names pins from the connector type.
- A wire with no gauge prints no gauge; a pin-to-pin signal wire may omit its gauge and installation, but a wire naming a rail still needs them.
- Connector types ship in the lockfile shape with cited pin orders, including SparkFun's 226 mA Qwiic limit.

### CAD and parts
- Add a box, cylinder or sphere sized by a named dimension; change the size in the app and the link and the torque check follow.
- A joint origin or shape dimension may name a declared param; an undeclared estimated link with geometry and a material gets its mass and centre of mass from its volume (in memory only).
- A mass Lockwire works out from shapes says so and names its density source.
- The material picker shows where each density comes from, and printed plastics note that infill changes the real density.
- `lockwire materials compare PATH --link NAME` shows the link's mass, the robot's mass and the worst torque margin in every library material.
- Design shows total mass, centre of mass and inertia with a marker in 3D, and a placement shows roll, pitch and yaw.
- In Design, joints are coloured by how much torque they have to spare, with the number beside each, and the support polygon is drawn.
- In Design, type G, an axis and a number to move the selected part that many millimetres; Move and Rotate work from where the part is.
- Optional `groups`, `patterns` (`add pattern`, `add duplicate`), `variants`, `topology.belts` (`add belt`) and stock shapes in the lockfile.
- Two example robots with resizable parts: a rover and a hexapod (18 ST3215 servos from the library) with masses from cited aluminium density.
- Catalog parts now name their maker, and the Feetech ST3215, STS3215, Here3+ and PM02 V3 masses cite the maker's own page.
- A Parts list panel lists parts, power rails and data buses, and saves as CSV; the parts-list export gains a Maker column.
- `lockwire catalog parts` browses the parts library; `lockwire add device --part <id>` adds a catalog part with no numbers typed.
- The MCP `list_parts` tool lists the catalog's stock shapes after the parts, each with its recipe, dimensions, whether it takes named dimensions and how its mass is worked out.

### Export
- `lockwire export <lockfile> --format urdf|sdf|mjcf` writes the model (never over your source; `--force` to overwrite); `--format mjcf` says its checks are narrower.
- `lockwire export --format stl|obj|dae` writes meshes (`--units mm` for millimetres); STEP says plainly it is not built.
- `lockwire export --format bom` writes a parts list (CSV), with weights from the catalog on an exact match, a note that compares the listed weight with the model's, and a notes column on each row: no cited mass, mass from an unverified catalog entry, and listed twice (merge into one row with qty 2); `--format budget [--plan]` writes the power budget CSV.
- SDF export keeps continuous joints as continuous.
- MuJoCo export derives a part's inertia from its shapes when mass is declared but no inertia is.
- The parts list counts a battery pack once when the pack and its source device both name it.

### App
- A real application menu: File gets Open Recent, Save a Copy and an Export submenu (now with Report (PDF)), Edit gets Undo and Redo, View gets zoom, full screen and light/dark, Help links to the shortcuts, docs and About.
- Open robot and drag-and-drop take MuJoCo (.xml, .mjcf) and Webots (.wbt, .proto) files; a Webots file shows its check result and says it has no 3D view.
- Opening a robot shows it in 3D while the check runs.
- Free edits and saves no longer ask for your name and record none unless you save one; the Declare form keeps its Checked by field.
- Print this part on a selected link exports its STL in millimetres and opens it, and says why it is disabled when the link has no shape.
- The start screen has a quiet 'Ask the Agent to design one' that opens a blank robot and the Agent tab.
- One Panels button opens every side panel; panels without data for your robot stay hidden. The Assistant is one tap.
- A Graph view shows what connects to what; pick a check to light the parts it uses, pick a part to see what it feeds, drives and carries; the saved report includes the graph.
- Hide a part, show only some, see through, wireframe, frames and axes from one View menu; Show everything puts it back.
- Pressing F frames the robot in the part of the view you can see; a read-only settings folder gets one plain sentence saying what to do.
- Screens share one type scale, spacing and header style, with one main action each; the Open screen's examples are a tidy list with Start from blank under it.
- The Agent can propose a part from a datasheet PDF with the page and a quote behind every number.
- Type a supply spec such as '4S, 5 V 3 A, 12 V 1 A' into the Agent tab and it proposes power trees built only from parts in the library, as cards you accept or reject.
- A batch the Agent proposes leaves no hidden files in your folder and is one undo step; a NO-GO robot is said in plain words.
- Shape-based examples show the robot you design: they no longer ship a stand-in scene, and shapes declared in the lockfile take precedence over a scene file found next to it.
- Adding a shape in Design no longer hides a robot's mesh model: the mesh stays and the new shape is drawn with it.
- Every Assistant entry point ends in a next step: with no key set it leads to the key screen, and a greyed Assistant says why.
- A failed 3D view no longer blanks the window.
- The 3D view keeps your camera across edits to the same robot and refits only for a new robot or Fit.
- Undo and Redo stay in Design and refresh in place, a value you declare shows at once, and a late reply for one robot never lands on another.
- A file that will not open shows one plain line saying what is wrong, with the raw text under Details; Retry opening appears only where a retry can succeed.
- A datasheet weight typed with a decimal comma (1,5) is read as 1.5, so Compare works.
- The start card puts the desk arm and the built-to-fail servo bench first, and each example says what its check finds.

### CLI
- `lockwire add link / add joint` and `lockwire delete` edit the robot from the CLI, with the engine's plain refusals; `from-parts` shows 'could be: ...' on a NO MATCH row.
- `lockwire add param`, `add shape` and `add attach` take flags for the same edits the MCP tools make (`--name --value --unit --min --max`; `--link --shape`; `--parent --part --frame --frame-id` with the joint's type, axis, limits and name).
- `lockwire blame`, `describe`, `diff` and the report print 'no name on record' instead of crashing on an edit saved without a name.
- `lockwire undo` / `redo` step one edit at a time without losing uncommitted edits; batch-edit is one undo step per accepted batch; compare with an older revision.
- Refusals print your file names and values literally (no stray colours or emoji); swap previews print part and joint names literally.
- Opening a robot file never runs code from it: a `.xacro`, URDF or SDF file that uses `$(eval ...)` is refused in one plain line. `$(env)`, `$(optenv)`, includes or YAML loads that reach outside the robot's package, a `$(find x)/../..` escape, a DOCTYPE or ENTITY declaration in an included file, and any expression over 10000 characters are refused the same way.
- Lockwire does not run code from robot files. A file built to waste memory can still make a check slow or fail; open files from people you trust.
- `lockwire export` never writes over the model's own source, assets or meshes, even with `--force`.
- The agent's `upgrade_contract` tool writes only inside the contract's folder, to a `.yaml` or `.yml` file.

### Changes to results
- MuJoCo files are now scored: torque checks are scored from the actuator strength written in the file, where before they were not compared at all. A MuJoCo file that was NOT SCORED can now be GO or NO-GO.
- Coming from 0.2.8: a bare MuJoCo file (no lockfile) that 0.2.8 called NOT SCORED (exit 3) now gets a verdict, often GO with notes (exit 0).
- Coming from 0.2.8: `check --json` reports `schema_version` "0.7" instead of "0.6", and under `forces` the `mass_total.value` and `cg.value` that were null are numbers wherever Lockwire can now work them out.
- Coming from 0.2.8: the agent tool server lists 24 tools instead of 13. The 13 released tools are unchanged; the 11 new ones include editors (proposals by default, writing only with `apply=true`) and tools that write files but refuse to overwrite.
- A robot whose servos, all stalling at once, would pull a rail below its minimum is now a NO-GO (all-servo brownout check, only where the catalog has stall data for the servos; none of the 42 example and fixture robots measured changed verdict).
- A closed loop in an SDF model (a five-bar leg, a parallel linkage) is no longer a NO-GO. `check` solves a planar loop and scores each driven joint's holding torque through it, and names the loops it cannot solve as not scored (not planar, a joint moving above or below the loop, too many or too few driven joints). The torque is for the base held still, not for the robot standing. URDF is unchanged: two parent joints on one link is still a failure there.
- Three part ratings were lowered to the cautious printed figure where a source prints two, and one part name was corrected. `check` and `--strict` output is unchanged on all 9 example and template files.
- Catalog numbers were re-read blind against their sources: 807 numbers, 0 wrong (2 pages refused the re-read and keep their status).
- The lockfile format is now 0.7 (pin-to-pin wires and an optional rail change what a file means); 0.6 and older files load unchanged and `lockwire upgrade` on a 0.6 file rewrites only the version line. Lockwire 0.2.8 and older refuse a file that uses new fields and name the field.
- Lockfiles can describe more of the power and wiring (all optional): wire length and net class; rail source, regulator, dropout, minimum input, capacitance, regen and thermal fields; battery parallel cells, reserve, voltage curve, R1/C1; bus phy, length, protocol, twisted; a `topology.protection` list (fuses, breakers, TVS diodes, reverse-polarity guards); connectors with pins; a `safety` block; tolerance bands, `assertions`, `modes`; params, link geometry and collision shapes, mimic joints.

## 0.2.8 — 2026-11-11

- `lockwire check model.xml` now reads MuJoCo MJCF models (masses, joints, ranges) instead of refusing them, and says what it did not compare (like actuators and meshes).
- `lockwire check` now notes when two neighbouring joints of one limb that share a name prefix (like `head_yaw` and `head_pitch`) have efforts 20 or more times apart, since one may be a typo.
- `lockwire check` now warns when a left/right pair of joints or links in one model differs by more than 10% in effort, velocity, mass or inertia (like a hip whose inertia is 25% off its twin).
- `lockwire check` now warns when a link carries the same inertial as the link it is fixed to (like a thigh guard that copies the thigh), because its mass is counted twice.
- `lockwire check` now warns when a continuous joint sits in the middle of a limited leg chain (like a thigh with no position limit between hip and calf); wheels and turntables are left alone.
- The robot catalog now recognises Unitree Go2-W, B2, B2-W, B1 and Aliengo and checks mass (and joint torque where Unitree publishes it) against the maker's page.
- `lockwire check robot.urdf --payload '2 kg' --at <link>` checks gravity with that load on that link, without editing a lockfile.
- `lockwire check robot.urdf --torque 'knee=120 N*m' --source <url>` compares that joint's URDF effort limit with the torque on the maker's page and warns when it is more than 10% off. Repeat `--torque` for more joints.
- `lockwire check <robot> --lock <path>` on a path that already exists still refuses, but now says to run `lockwire check <path>` to check against it, or pick a new name.
- `lockwire check` no longer warns about velocity limits on a mimic follower joint (like a gripper finger); it still warns when the mimic target is missing.
- `--model unitree-go2` now warns when the URDF's largest joint effort is more than 10% off the maker's published max joint torque (about 45 N·m).
- `lockwire check` on a xacro model now resolves `$(find pkg)` from the model's own checkout (no ROS needed) instead of exiting 2.
- `lockwire diff` takes two robot files (URDF, xacro, SDF): it lists the mass and the changed limits, and pairs left/right joints.
- `lockwire serve` without the serve extra now says to run `pip install "lockwire[serve]"`.
- `lockwire plan` says its rail total includes the plan's own added loads, so it can read higher than `check`.
- xacro `$(find pkg)` without a git folder no longer searches above the model's own workspace.

## 0.2.7 — 2026-11-10

Fixed:

- `pip install lockwire` works again on a clean install: 0.2.6 pulled in typer 0.29, which broke the command line. Lockwire now asks for typer below 0.29, and also below the next major of pydantic, ruamel-yaml and mcp, so a new major cannot break a clean install.
- `lockwire check` no longer crashes on a Windows console that can't print symbols like Δ; characters the console can't show print as `?`. `python -m lockwire` gets the same fix.
- A 0.5 lockfile gets the full structure comparison again, so a changed joint axis is NO-GO. Only 0.2 to 0.4 lockfiles skip it.
- A swap (`lockwire swap --use`) no longer changes the lockfile's version line, so a swapped 0.5 file still opens in 0.2.6.
- `lockwire upgrade` on a file with Windows (CRLF) line endings changes one line, the version, and keeps the line endings.
- A private part in `~/.lockwire/parts.yaml` is refused unless its torque is in N*m and its voltage in V, and every private-parts error names the entry and the field. The kg*cm conversion hint appears only for a torque. The same unit check now applies to any parts file you pass with `--catalog`, and a mass must be in kg or g.
- Part Swap flags a servo whose highest voltage is below your supply, or that is on a different bus, and ranks a servo whose voltage range does not cover your supply below the rest, then a different bus below the same bus. It now uses the supply voltage your lockfile declares (the rail of the device that drives the joint, or the only rail with a voltage) and says which supply it used, instead of always assuming the current servo's maximum.
- `lockwire record-servos` refuses a rate of 0 or below, keeps the samples it read if the bus drops mid-recording, and defaults Dynamixel to its factory 57600 baud. It also refuses a reversed `--ids` range (such as 6-1) and the broadcast ID 254 before opening the port, and no longer leaves an empty output folder when no servo answers.
- `lockwire plan --json -` now prints only JSON on stdout.
- `lockwire bench` without the source repo's corpus says the corpus ships only in the source repo and to pass your own with `--corpus`, instead of a vague "file not found".
- `check` and `start` say "file not found" with the path when a model path is mistyped or missing.
- The servo hint no longer points at a lockfile that does not exist.
- `lockwire check --servo <id>` on a lockfile with no `servo:` line prints the line to add to keep that servo.
- The model-value checks read the model file next to your lockfile only, never a same-named file in the folder you happen to be in.
- Lockwire reads and writes its own text files as UTF-8, so the bundled parts data and your lockfiles work on machines whose locale is not UTF-8.
- `agent-setup` run through `uvx` writes a hook that calls `lockwire` (or `uvx lockwire`) instead of a Python path inside uv's cache that disappears.
- The datasheet mass row sums the model file as it is now, not the masses that were locked.
- `lockwire mcp` prints an install command that works outside the repo.
- The refusal message for importing a manifest no longer says it waits for a vendor to publish a standard.
- `lockwire start --example` writes format 0.6 lockfiles, and the example file is otherwise copied exactly as shipped: only the version line changes.
- Typing a Pro command name in the free package prints the waitlist line.
- `lockwire check` on a lockfile refuses `--mass`, `--source`, `--witness`, `--xacro-arg` and `--lock` in one line instead of silently ignoring them.
- `lockwire check --json -` sends every error to stderr, so stdout stays pure JSON.
- The "scored N" line adds up: accepted counts only scored findings that did not pass.
- A YAML syntax error in a lockfile prints one line naming the file, line and column.
- `lockwire start` on a missing file exits 2, like `check`. `check .` says it is a folder, `start x.yaml` says it is not a model file, and `swap` on an unknown joint lists the joints.
- A lock started from a `.xacro` is compared again on `check`: the model is expanded the same way `start` expanded it, so a mass edit is NO-GO instead of passing.
- `lockwire start --xacro-arg name=value` is recorded in the lockfile (`xacro_args`, optional, so older lockfiles still load) and `check` replays it when it re-expands the model. The re-lock hint in a finding repeats those `--xacro-arg` flags.
- `check robot.urdf` with no lockfile finds meshes beside the model, not in a temp folder.
- A broken model gets a NO-GO that names the joint or link instead of a warning or a crash: a NaN or negative mass, a negative inertia, reversed joint limits, a kinematic loop or more than one root link, and a mimic joint that follows a missing joint. A file with no links or a link with an empty name is refused in one line.
- Odd public URDFs get a verdict, never a crash: a negative velocity limit, or a limit of `inf` or 1.8e308, is read as not stated with one warning per joint.
- A joint that names a missing link, or a duplicate link or joint name, is a NO-GO in `check` and a one-line refusal in `start`.
- A comma decimal such as `0,5` in a model prints one line naming the file, link and attribute.
- Every plain model load error names the file as you typed it, not its resolved absolute path.
- The MCP `check_model` tool and `lockwire check --json -` now give the same verdict and exit code as the command line, including warnings from the model loader and duplicate or missing links.
- MCP tools print the CLI's plain line for a file error instead of raw OS error text, and say "is a folder" or "is a robot model, not a lockfile; use `check_model`" when given the wrong path.
- `compare_proposal` refusals name its own arguments (witness and change), not CLI flags.
- Edit refusals name your lockfile, never the temporary file used to stage the edit.
- A looping or dangling symlink as a model path says so instead of "file not found".
- The refusal for an XML file with a DOCTYPE says XML, not URDF, so an SDF is not called a URDF.
- A rail `capacity` of 0 or below is refused ("must be greater than 0").
- A failed `lockwire start` leaves no new lockfile behind, and a failed `start --force` leaves the old lockfile untouched.
- `--mass` must be a positive mass and `--tolerance-pct` 0 or more. A bare `--xacro-arg word` is refused in one line instead of being dropped.
- A typed `--mass` that breaks the limit no longer hints a re-run with the catalog figure.
- `lockwire swap` on a missing path prints one plain line, and `swap --use` repeats the new servo's warnings (such as "different bus") as the last line.
- Cloning a robot by name no longer fills a pipe with download progress bars.
- The check hook no longer prints the `--force` re-lock command; it says the model changed and needs a person's review.
- Gravity reach: a slide joint that no transmission names is treated as passive and not compared.
- Gravity reach: an over-effort joint behind a transmission with a reduction above 1 warns that its effort limit may be motor-side, instead of failing.
- Mass budget: a link with no `<inertial>` counts as a 0 kg frame, so the total mass is scored, and the output names those links.
- A link with mass and an all-zero inertia tensor is a NO-GO that names the link.
- A link whose inertia is too small for its mass (smaller than a solid ball of the densest metal at that mass) gets a warning.
- A moving joint with a velocity limit of 0 gets a warning.
- `--catalog` pointed at a file that is not a parts catalog says so in one line and exits 2, instead of a YAML error.
- Text and JSON verdicts now agree on a model file with nothing compared: NOT SCORED, exit 3.
- Unwritable folders, missing files, files that are not lockfiles, `--mass abc` and other non-number values print one plain line instead of a Python error.
- A zero or negative charger or cutoff value is refused.
- The app's unreadable-file screen offers "Open a different robot".
- The datasheet badge says "accepted difference (mass)", in grey, after `lockwire accept` instead of a green "matches".

New:

- Part Swap: `lockwire swap lockwire.yaml wrist` ranks catalog servos for a joint by its gravity load and shows what each swap does to the joints above it. It only shows by default; `--use <servo>` writes the change. The MCP server has a matching `swap` tool, and the desktop app has a Swap panel with the same preview and apply. A servo whose stall torque is rated at a higher voltage than your supply is flagged and ranked after same-supply servos.
- A cited servo parts list: the catalog gains servo mass, voltage range, bus and stall torque for Part Swap (five new rows, five filled in). Every part is labelled cited (all numbers from the maker's datasheet, each with its URL) or unverified, and the label is worked out from the numbers.
- Private parts: put your own parts in `~/.lockwire/parts.yaml` and they stay on your machine. A broken parts file stops the command with one plain line naming the file.
- Servo recorders: `lockwire record-servos` records the stored settings and live readings (position, goal position, speed, load, voltage, temperature, gains) of Feetech STS/SCS or Dynamixel X servos on a serial bus (`pip install 'lockwire[bus]'`, `--kind dxl` for Dynamixel). Read-only: it sends only ping and read packets and never writes a register. Tested on recorded bytes, not yet on a live bus by us.
- servo-bench, the bundled example, gains a 3-joint arm whose wrist servo is built to fail the servo check.
- Guides for the parts list, part swap and the servo recorder in `docs/guides/`.
- Six checks beyond mass and power, each run only when you declare its lines: charger setpoint, float mode and charge current against the pack's chemistry (`charger_voltage`, `charger_current`, `charger_float`); low-voltage cutoff against the chemistry's lowest safe cell voltage (`cutoff_voltage`); rail current against the connector's maker-rated current (`connector: xt60` or `jst-xh`); threadlocker declared on a 3D-printed part (`threadlocker: true`); ArduPilot or PX4 failsafe parameters left off or set below the pack (`autopilot:`); and 2.4 GHz transmit power against the licence-free limit where the robot runs (`region: eu`, `us` or `in`). A lockfile without these lines is checked exactly as before. Listed in `docs/guides/checks.md`.
- `link_values` check: a NaN or negative mass is a NO-GO; a mass with no inertia and a mesh scale that is NaN, zero or not three numbers are warnings. Listed in `docs/guides/checks.md`.
- Four checks on the model file's structure, with nothing to declare: a `ros2_control` or `transmission` block that names a joint the model does not have, or commands a fixed or mimic joint; a camera optical frame not rotated to z forward, x right, y down (REP-103); a model with more than one root link, a link named `map` or `odom`, or a floating or planar joint below the root; and a mimic joint that follows a missing joint or another mimic.
- Three more on the model file, also with nothing to declare: a safety controller whose soft limits sit outside the joint's hard limits or were left at zero; a `<gazebo reference>` block naming a link or joint the model lacks, and two sensors sharing a name; and one mesh used at two different scales for a link's visual and collision.

Changed:

- Lockfile format 0.6. New files are written at 0.6. 0.5 lockfiles still load, and `lockwire upgrade` moves a 0.5 file to 0.6 by changing its version line only. Files written at 0.6 do not open in 0.2.6 or older. A swap leaves the version line alone.
- The servo check's fix text shows the exact edit to the model (the joint's effort limit, with the real joint name and the lockfile by name) and a runnable `lockwire swap` hint for a better-fitting servo.
- A mistake in a lockfile prints one plain line per error (file, path, message) and exits 2, instead of a validation dump or "bug in lockwire".
- A lockfile over 512 KiB is refused up front with exit 2, in under a second. The worst hostile file under the cap parses in about 9 seconds.

Moved:

- These commands were in the public 0.2.6 package and are not in 0.2.7: `preflight`, `baseline`, `baseline-review`, `baseline-check`, `sample`, `health`, `actuator-review-draft` and `bag`. They no longer install with `pip install lockwire`. Running one prints a waitlist line pointing to https://lockwirerobotics.com and exits non-zero. `lockwire help --all` still lists the Pro commands. The MCP tools `preflight`, `health` and `prepare_actuator_review_draft` are no longer in the free package either; they were in public 0.2.6. The ROS 2 package that runs preflight on a live robot is Pro. The check, check-sim, Part Swap, catalog, servo recorder, desktop app and MCP server stay free.

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
