---
title: Every check
description: What each Lockwire check compares, grouped by area. Generated from the code.
---

# Every check

The free `lockwire check` runs every check below except the group marked Lockwire Pro.
A check with nothing to compare says so under "Not compared yet" instead of passing.

<!-- Generated from the check code. Do not edit by hand. -->

## Model

- **`model_checks_skipped`**: One line, not silence, when the URDF-reading checks cannot run on this lock.
- **`urdf_sanity`**: Structural mistakes a tolerant lock accepted: inverted or missing limits, non-unit axes, loops, mimic ranges past the follower's limits, mesh name clashes.
- **`link_values`**: Link values no simulator accepts: a NaN or negative mass, a mass with no inertia, a mesh scale that is NaN, zero or not three numbers.
- **`urdf_drift`**: Compare the links and joints recorded in the lockfile with the model file on disk.
- **`model_values`**: Locked link mass (inside its `mass_tolerance` band when it has one) / COM / principal inertia vs the on-disk URDF or SDF.
- **`mesh_drift`**: Mesh file/scale per link, then recorded mesh bytes, vs the on-disk model.
- **`zero_mass`**: A link with no mass, or a mass of zero or less, read from the link records themselves, so no link is left out of the inertia.
- **`floating_mass`**: Warns about a link with weight that no joint ties to the robot's main body.
- **`unusable_effort`**: A model joint effort of 0 or less is unset, not a limit; warn on a fresh check.
- **`inertia_valid`**: Principal moments must be non-negative and obey a <= b + c.
- **`duplicate_inertial`**: A link carrying the same inertial as the link it is fixed to double-counts its mass.
- **`mirror`**: Joints and links named as left/right twins must agree on effort, velocity, mass, inertia.
- **`sibling_effort`**: Consecutive moving joints of one chain named alike whose effort differs 20x: a note.
- **`inertia_scale`**: Exactly equal round-number principal moments (1, 1e-4 ...): a placeholder?
- **`mass_vs_size`**: Density, CoM and radius of gyration against the link's primitive shapes.
- **`limit_unit_traps`**: Degrees typed as radians, and joint numbers too large for a robot (mm typed as m).
- **`controller_limits`**: A ros2_control/MoveIt joint_limits.yaml wider than the URDF limits.
- **`mechanisms`**: Does a joint declared to spin all the way round actually have the geometry to?
- **`scene_placement`**: Compare a device's declared `mount_xyz` to where the scene actually put it.
- **`control_joint_names`**: Control blocks (ros2_control, transmissions) that name a joint the model lacks, or command a fixed or mimic joint.
- **`optical_frames`**: A camera's optical frame not rotated to z forward, x right, y down (REP-103).
- **`frame_tree`**: Frames that do not form one tree: more or fewer than one root, a link called map or odom, a floating or planar joint below the root. (The parser already refuses a joint that names a missing link.)
- **`mimic_targets`**: A mimic joint that follows a joint the model lacks, follows another mimic, or follows itself in a circle.
- **`safety_limits`**: A joint's safety controller whose soft limits sit outside the joint's hard limits, or are left at their default of zero so the joint cannot move.
- **`datasheet_limit`**: A revolute joint's limit span vs the travel its linked catalog servo's datasheet gives (catalog number `rotation_range`, an angle in deg or rad). No servo, no cited range, no both limits: nothing.
- **`collision_visual`**: A link whose collision primitives' bounding box is far from its visual ones'. Only primitive shapes (box, cylinder, sphere) are read, not meshes; a link needs both.
- **`gazebo_references`**: A `<gazebo reference=...>` block naming a link or joint the model lacks (the simulator drops the block without a word), and two sensors sharing one name.
- **`mesh_scale_agreement`**: One link using the same mesh file for its visual and its collision at two different scales, so what you see is not what collides.
- **`inertia_sanity`**: Warns when a link's declared inertia is far from what its own shapes would give at its mass.
- **`no_material`**: Notes a part that has shape but no mass and no material, so there is nothing to weigh it by.

## Budgets

- **`budgets`**: Each budget is the sum of the lockfile's own device and model entries, compared with its limit. Power follows the rail tree, converter efficiency included, so one load counts against its converter, the rails above it, and endurance.
- **`torque_budgets`**: A joint's modeled effort demand must not exceed the paired actuator's continuous geared output; peak torque is not a sustainable design budget.
- **`servo_stall`**: Each revolute/continuous joint's effort limit vs the stall torque of the servo that drives it, named by `check --servo <id>` or the lockfile's `servo:` line.
- **`gravity_reach`**: Static gravity torque at the worst pose must fit each joint's effort limit.
- **`thermal_budget`**: Junction-temperature estimate vs published module thermal limit.
- **`link_budget`**: Compare RF link fade margin at a declared range against the requirement.
- **`battery_shipping_class`**: Pack energy against the IATA lithium-ion watt-hour lines.
- **`cold_derating_coverage`**: A below-freezing declared ambient, an endurance budget, and no derating row for it.
- **`battery_cell_window`**: The pack's cell window (cells x cell min/max for its chemistry) against each pack-fed rail's range.
- **`battery_c_rate`**: Peak pack current (power rollup, worst mode) against capacity x continuous and burst C-rate.
- **`battery_sag`**: Pack nominal minus peak current x pack internal resistance, against the lowest pack-fed rail minimum.
- **`battery_temperature`**: The declared ambient range against the pack's charge and discharge temperature windows.
- **`battery_energy_density`**: Pack Wh per kg above 300, more than any shipping cell: the capacity or mass is wrong.
- **`hold_torque`**: Warns when holding a plan pose against gravity needs more torque than the joint can give.
- **`tip_over`**: NO-GO when the robot would tip over: its centre of mass sits outside the area its wheels or feet touch.
- **`standing_load`**: Warns when a joint cannot carry the robot standing on its declared support contacts.
- **`standing_sweep`**: Each joint's torque with the robot on its feet (collisions with the word foot in their name, else leg-end links named foot or toe), over every pose its leg can reach.

## Electrical

- **`rail_topology`**: Rails must not feed each other in a loop, and voltage cannot rise through a converter the lockfile does not declare: either would hide a wiring mistake.
- **`voltage_compatibility`**: Refuse a device supplied outside its model's declared input range.
- **`ampacity`**: The current a wire carries, computed from its loads, against the published safe current for its gauge.
- **`rail_current`**: A rail's published current_limit against its computed draw (wire limits are `ampacity`).
- **`electrical_segments`**: Logic levels, I2C and CAN wiring against their sourced limits: logic-level margins, one unbuffered I2C segment, and a CAN segment's nodes and end terminations.
- **`fuse_wire`**: A fuse rated above what its wire can carry cannot protect that wire (NO-GO).
- **`rail_source_rules`**: Warns about a rail with no power source, servos powered from the flight controller, an LDO that runs too hot, and supplies with no shared ground.
- **`wire_drop`**: A servo's wire drops its supply below the servo's catalog minimum input voltage at stall.
- **`connector_heat`**: A catalog connector carrying more than its rated current per contact, with the I^2*R heat.
- **`rail_operating_point`**: A rail's loaded voltage (its voltage minus the worst wire drop at nominal draw) below the rail's own declared voltage_range minimum.
- **`servo_brownout`**: NO-GO: every servo on a rail at stall at once pulls a device it feeds below that device's declared minimum input.
- **`bulk_capacitance`**: A servo rail whose declared bulk capacitance is below 470 uF per servo (advice, unsourced).
- **`reverse_polarity`**: A lockfile that declares protection parts, with a multi-cell pack feeding a rail and no reverse-polarity guard on that path, gets one warning (advice only: no maker source read).
- **`tvs_rating`**: A rail with a motor on it that is declared unable to absorb regen (accepts_regen false) needs a TVS naming it, whose standoff is not below the rail's highest voltage. Only a lockfile that declares protection parts is read. Advice only: no maker source read, so no number here is sourced.

## Buses

- **`estop_wiring`**: A dual-channel safe-torque-off driver needs both channels on the e-stop chain, on separate contacts. Warnings only; nothing is emitted without connector safety data.
- **`bus_identities`**: Bus-local IDs must be unique, and duplicate serials cannot prove instance identity. These checks prevent addressing collisions and clone USB devices from being mistaken for distinct physical units.
- **`bus_bandwidth`**: Declared traffic must fit below a conservative share of raw bitrate; protocol overhead and contention make 100% raw utilization unusable.
- **`i2c_reserved_address`**: A 7-bit I2C address in a reserved group (0x00-0x07, 0x78-0x7F).
- **`servo_bus`**: Warns about two servos sharing an ID, a servo set to the broadcast ID, and a servo power adapter too small for the servos on it.
- **`bus_wiring_rules`**: Warns about bus cabling risks, only for buses that declare phy / length_m.

## Evidence and records

- **`device_owners`**: Every device must name the person or team accountable for it.
- **`reference_integrity`**: Every cross-entry reference must resolve. A dangling ref is a violation: it means the contract describes a robot that doesn't exist in the records.
- **`source_portability`**: A source is a file beside the lockfile, a URL or a cited title.
- **`compared_from`**: Check a device mass copied from a catalog part still matches that part.
- **`datasheet_agreement`**: A recognised robot's numbers from its model against its maker's datasheet.
- **`torque_claims`**: Each joint's URDF effort limit vs the published torque (N*m) the user typed.
- **`edge_tolerance`**: A dependency's declared tolerance against a fresh reading; the free check has no live reading, so it lists these as not compared (readings come from Lockwire Pro).
- **`envelopes_vs_hard_limits`**: A signal envelope declared outside its device's hard limits (envelopes come from Lockwire Pro recordings): the hard limits win, and the envelope must be measured again.
- **`driver_watchdog_marker`**: Actuators with no observed reset/watchdog marker: a disclosure, never a pass or fail.
- **`cross_file_agreement`**: Warns when the same joint limit is written differently in two files the lock names.

## Charging, wiring and settings

- **`connector_mating`**: A wire whose two connector ends are different declared families cannot mate.
- **`back_power`**: A board with two declared power inputs and no reverse-polarity/ideal-diode guard on it.
- **`qwiic_budget`**: The summed current of a whole Qwiic chain against SparkFun's 226 mA cable figure.
- **`port_budget`**: The summed current of everything downstream of a connector against the max_current_a declared on that connector (e.g. a flight-controller port). Nothing without the rating.
- **`charger`**: Declared charger setpoint, float mode and current against the pack's chemistry.
- **`battery_cutoff`**: Declared low-voltage cutoff against the chemistry's lowest safe cell voltage.
- **`connector_rating`**: Worst rail current through a wire against its connector's maker-rated current.
- **`threadlocker_on_print`**: Anaerobic threadlocker declared on a 3D-printed (thermoplastic) part.
- **`print_bed_fit`**: A printed part longer than its printer's bed on any axis (one finding per part).
- **`belt_length`**: A belt's declared teeth or length that disagrees with its pulleys and centre distance.
- **`autopilot_failsafe`**: Declared flight-controller parameters that leave a failsafe off or set below the pack.
- **`radio_legal_power`**: 2.4 GHz transmit power against the declared region's licence-free limit.

## Recordings and actuator cases (Lockwire Pro)

- **`condition_envelopes`**: Refuse to extrapolate beyond the conditions supported by healthy baselines.
- **`actuator_case_conformance`**: One selected actuator case against its evidence; it runs only when you pick that case.
- **`actuator_case_simulation_validity`**: A simulator's prediction for one actuator case against held-out physical measurements.

## Control loops

- **`margin_findings`**: Advice, never a verdict: bandwidth vs loop rate, phase margin and effort, for each controller that declares those inputs.
