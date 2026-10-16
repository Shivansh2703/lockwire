import type { CheckFacePayload } from "./checkFacePayload";

// Measured 2026-09-08: `uv run python -c "from lockwire import api, gui; ..."` against
// `tests/fixtures/x500/contract.yaml` (there is no `tests/fixtures/x500/lockwire.yaml` --
// only `contract.yaml` and `contract_corrected.yaml` exist in that directory; the box
// brief's accept-line path does not exist on disk). `verdict`/`counts` are
// `lockwire.api.check`'s; `headline`/`qualifier`/`cards`/
// `cli_line` are the page builder's, run against the same contract. This is
// real, measured output, not invented data.
export const X500_CHECK_PAYLOAD: CheckFacePayload = {
  robot: "Holybro X500 V2 PX4 reference quadrotor",
  verdict: "NO-GO",
  headline: "Don't power it on yet - 2 things do not add up",
  qualifier: "8 things add up · 9 things I couldn't check",
  scored: 10,
  violations: 2,
  warnings: 0,
  unchecked: 9,
  findings: [
    {
      severity: "violation",
      quantity: "budgets/bud.power.pm02-5v",
      observed: "35.20 W (draw mode: nominal)",
      expected: "<= 15.60 W",
      owner: "power-integration",
      source: "docs/reference_robot_specs.md",
      witness: null,
      action: "shed load on rail 'rail.pm02-5v' or raise its capacity with provenance",
    },
    {
      severity: "violation",
      quantity: "budgets/bud.power.pm02-5v.7w-mode",
      observed: "17.20 W (draw mode: 7W)",
      expected: "<= 15.60 W",
      owner: "power-integration",
      source: "docs/reference_robot_specs.md",
      witness: null,
      action: "shed load on rail 'rail.pm02-5v' or raise its capacity with provenance",
    },
    {
      severity: "not_scored",
      quantity: "devices/dev.companion.jetson-orin-nano/physical/mass",
      observed: "mass not declared",
      expected: "a positive mass with units",
      owner: "airframe-integration",
      source: null,
      witness: null,
      action: "declare the device's mass, or accept it excluded from this budget",
    },
    {
      severity: "not_scored",
      quantity: "devices/dev.camera.oak-d-lite/physical/mass",
      observed: "mass not declared",
      expected: "a positive mass with units",
      owner: "airframe-integration",
      source: null,
      witness: null,
      action: "declare the device's mass, or accept it excluded from this budget",
    },
    {
      severity: "not_scored",
      quantity: "devices/dev.magnetometer.ist8310/physical/mass",
      observed: "mass not declared",
      expected: "a positive mass with units",
      owner: "airframe-integration",
      source: null,
      witness: null,
      action: "declare the device's mass, or accept it excluded from this budget",
    },
    {
      severity: "not_scored",
      quantity: "topology/buses/bus.can.dronecan/bandwidth",
      observed: "no declared traffic on this bus",
      expected: "at least one driver on the bus declaring nominal_rate_hz and payload_bytes",
      owner: null,
      source: "docs/reference_robot_specs.md",
      witness: "w0",
      action: "declare the traffic each driver puts on this bus, or accept that its bandwidth is unchecked",
    },
    {
      severity: "not_scored",
      quantity: "topology/buses/bus.uart.telemetry/bandwidth",
      observed: "no declared traffic on this bus",
      expected: "at least one driver on the bus declaring nominal_rate_hz and payload_bytes",
      owner: null,
      source: "https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf",
      witness: "w0",
      action: "declare the traffic each driver puts on this bus, or accept that its bandwidth is unchecked",
    },
    {
      severity: "not_scored",
      quantity: "topology/buses/bus.uart.companion/bandwidth",
      observed: "no declared traffic on this bus",
      expected: "at least one driver on the bus declaring nominal_rate_hz and payload_bytes",
      owner: null,
      source: "https://docs.px4.io/main/en/peripherals/mavlink_peripherals.html",
      witness: "w0",
      action: "declare the traffic each driver puts on this bus, or accept that its bandwidth is unchecked",
    },
    {
      severity: "not_scored",
      quantity: "topology/buses/bus.usb.companion/bandwidth",
      observed: "no declared traffic on this bus",
      expected: "at least one driver on the bus declaring nominal_rate_hz and payload_bytes",
      owner: null,
      source:
        "https://github.com/luxonis/oak-hardware/blob/master/DM9095_OAK-D-LITE_DepthAI_USB3C/Datasheet/OAK-D-Lite_Datasheet.pdf",
      witness: "w0",
      action: "declare the traffic each driver puts on this bus, or accept that its bandwidth is unchecked",
    },
    {
      severity: "not_scored",
      quantity: "topology/buses/bus.i2c.external/bandwidth",
      observed: "bitrate unrated; bandwidth not scored",
      expected: "a positive bitrate so utilization can be scored",
      owner: null,
      source: "docs.holybro.com/autopilot/pixhawk-6c/technical-specification",
      witness: "w0",
      action: "lock a cited bitrate, or accept that this bus is excluded from the bandwidth gate",
    },
    {
      severity: "not_scored",
      quantity: "model/structure_hash",
      observed: "no URDF provenance; structure drift not scored",
      expected: "a URDF path in model provenance so structure can be scored",
      owner: null,
      source: "holybro.com/products/x500-v2-kits",
      witness: "w0",
      action: "lock model provenance to the on-disk URDF, or accept that URDF drift is unchecked",
    },
  ],
  cards: [
    {
      severity: "violation",
      tag: "Stop",
      title: "Pm02 5 V rail overloaded",
      body:
        "It can give 15.60 W, and what's plugged into it wants 35.20 W. On it: camera.oak-d-lite, " +
        "companion.jetson-orin-nano and radio.rfd900x. Power this on and everything on that rail browns out together.",
      fix: "shed load on rail 'rail.pm02-5v' or raise its capacity with provenance",
      raw:
        "VIOLATION budgets/bud.power.pm02-5v\n  observed   35.20 W (draw mode: nominal)\n  expected   <= 15.60 W\n" +
        "  delta      +19.60 W (+126%)\n  dependent  dev.camera.oak-d-lite, dev.companion.jetson-orin-nano, " +
        "dev.radio.rfd900x\n  owner      power-integration\n  source     docs/reference_robot_specs.md\n" +
        "  action     shed load on rail 'rail.pm02-5v' or raise its capacity with provenance",
      hits: ["rail.pm02-5v", "dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "dev.radio.rfd900x"],
      faults: ["rail.pm02-5v"],
      affected: ["dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "dev.radio.rfd900x"],
      targets: [],
      count: 1,
      count_figure: { value: 1, unit: "findings", kind: "derived", by: "gui._cards" },
      observed: { value: 35.2, unit: "W", kind: "derived", by: "checks.check_budgets" },
      expected: { value: 15.6, unit: "W", kind: "declared", by: "schema.Contract.budgets.limit" },
      group_title: "",
      instead: [{ text: "Add a regulator with 19.60 W more capacity", kind: "projected" }],
      alternatives: [
        {
          text: "add a 19.60 W regulator fed from rail 'battery'",
          value: 19.6,
          unit: "W",
          kind: "projected",
          mark: "projected",
        },
      ],
      figure_note: "Observed is the check's sum of your declared draws; Expected is your declared limit.",
      shape: "budgets/*",
      label: "Check",
      also_in_run: false,
    },
    {
      severity: "violation",
      tag: "Stop",
      title: "Pm02 5 V rail overloaded in the 7W mode",
      body:
        "It can give 15.60 W, and what's plugged into it wants 17.20 W in the 7W mode. On it: camera.oak-d-lite, " +
        "companion.jetson-orin-nano and radio.rfd900x. Power this on and everything on that rail browns out together.",
      fix: "shed load on rail 'rail.pm02-5v' or raise its capacity with provenance",
      raw:
        "VIOLATION budgets/bud.power.pm02-5v.7w-mode\n  observed   17.20 W (draw mode: 7W)\n  expected   <= 15.60 W\n" +
        "  delta      +1.60 W (+10%)\n  dependent  dev.camera.oak-d-lite, dev.companion.jetson-orin-nano, " +
        "dev.radio.rfd900x\n  owner      power-integration\n  source     docs/reference_robot_specs.md\n" +
        "  action     shed load on rail 'rail.pm02-5v' or raise its capacity with provenance",
      hits: ["rail.pm02-5v", "dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "dev.radio.rfd900x"],
      faults: ["rail.pm02-5v"],
      affected: ["dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "dev.radio.rfd900x"],
      targets: [],
      count: 1,
      count_figure: { value: 1, unit: "findings", kind: "derived", by: "gui._cards" },
      observed: { value: 17.2, unit: "W", kind: "derived", by: "checks.check_budgets" },
      expected: { value: 15.6, unit: "W", kind: "declared", by: "schema.Contract.budgets.limit" },
      group_title: "",
      instead: [{ text: "Add a regulator with 1.60 W more capacity", kind: "projected" }],
      alternatives: [
        {
          text: "add a 1.60 W regulator fed from rail 'battery'",
          value: 1.5999999999999996,
          unit: "W",
          kind: "projected",
          mark: "projected",
        },
      ],
      figure_note: "Observed is the check's sum of your declared draws; Expected is your declared limit.",
      shape: "budgets/*",
      label: "Check",
      also_in_run: false,
    },
    {
      severity: "not_scored",
      tag: "Couldn't check",
      title: "3 parts have no weight recorded",
      body:
        "camera.oak-d-lite, companion.jetson-orin-nano and magnetometer.ist8310 — missing from your weight " +
        "total and your centre of gravity.",
      fix: "declare the device's mass, or accept it excluded from this budget",
      raw:
        "NOT_SCORED devices/dev.camera.oak-d-lite/physical/mass\n  observed   mass not declared\n" +
        "  expected   a positive mass with units\n  owner      airframe-integration\n" +
        "  action     declare the device's mass, or accept it excluded from this budget\n\n" +
        "NOT_SCORED devices/dev.companion.jetson-orin-nano/physical/mass\n  observed   mass not declared\n" +
        "  expected   a positive mass with units\n  owner      airframe-integration\n" +
        "  action     declare the device's mass, or accept it excluded from this budget\n\n" +
        "NOT_SCORED devices/dev.magnetometer.ist8310/physical/mass\n  observed   mass not declared\n" +
        "  expected   a positive mass with units\n  owner      airframe-integration\n" +
        "  action     declare the device's mass, or accept it excluded from this budget",
      hits: ["dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "dev.magnetometer.ist8310"],
      faults: ["dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "dev.magnetometer.ist8310"],
      affected: [],
      targets: [],
      count: 3,
      count_figure: { value: 3, unit: "findings", kind: "derived", by: "gui._cards" },
      observed: null,
      expected: null,
      group_title: "",
      instead: [],
      alternatives: [],
      figure_note: null,
      shape: "devices/*/physical/mass",
      label: "Check",
      also_in_run: false,
    },
    {
      severity: "not_scored",
      tag: "Couldn't check",
      title: "I can't tell if your CAD changed",
      body: "No URDF file is recorded against this lockfile, so if someone moves a mount or swaps the frame, nothing here notices.",
      fix: "lock model provenance to the on-disk URDF, or accept that URDF drift is unchecked",
      raw:
        "NOT_SCORED model/structure_hash\n  observed   no URDF provenance; structure drift not scored\n" +
        "  expected   a URDF path in model provenance so structure can be scored\n" +
        "  source     holybro.com/products/x500-v2-kits\n" +
        "  action     lock model provenance to the on-disk URDF, or accept that URDF drift is unchecked",
      hits: [],
      faults: [],
      affected: [],
      targets: [],
      count: 1,
      count_figure: { value: 1, unit: "findings", kind: "derived", by: "gui._cards" },
      observed: null,
      expected: null,
      group_title: "",
      instead: [],
      alternatives: [],
      figure_note: null,
      shape: "model/structure_hash",
      label: "Check",
      also_in_run: false,
    },
    {
      severity: "not_scored",
      tag: "Couldn't check",
      title: "4 cables have nothing declared on them",
      body:
        "can.dronecan, uart.companion, uart.telemetry and usb.companion — a bitrate is written down, but " +
        "nothing says what's actually sent over them, so I can't tell you whether it fits. That's a blind spot, not a fault.",
      fix: "declare the traffic each driver puts on this bus, or accept that its bandwidth is unchecked",
      raw:
        "NOT_SCORED topology/buses/bus.can.dronecan/bandwidth\n  observed   no declared traffic on this bus\n" +
        "  expected   at least one driver on the bus declaring nominal_rate_hz and payload_bytes\n" +
        "  source     docs/reference_robot_specs.md\n" +
        "  action     declare the traffic each driver puts on this bus, or accept that its bandwidth is unchecked",
      hits: ["bus.can.dronecan", "bus.uart.companion", "bus.uart.telemetry", "bus.usb.companion"],
      faults: ["bus.can.dronecan", "bus.uart.companion", "bus.uart.telemetry", "bus.usb.companion"],
      affected: [],
      targets: [],
      count: 4,
      count_figure: { value: 4, unit: "findings", kind: "derived", by: "gui._cards" },
      observed: null,
      expected: null,
      group_title: "",
      instead: [],
      alternatives: [],
      figure_note: null,
      shape: "topology/buses/*/bandwidth",
      label: "Check",
      also_in_run: false,
    },
    {
      severity: "not_scored",
      tag: "Couldn't check",
      title: "1 cable has no speed written down",
      body:
        "i2c.external — without a bitrate I can't tell you whether the messages you're sending fit down them. " +
        "That's a blind spot, not a fault.",
      fix: "lock a cited bitrate, or accept that this bus is excluded from the bandwidth gate",
      raw:
        "NOT_SCORED topology/buses/bus.i2c.external/bandwidth\n  observed   bitrate unrated; bandwidth not scored\n" +
        "  expected   a positive bitrate so utilization can be scored\n" +
        "  source     docs.holybro.com/autopilot/pixhawk-6c/technical-specification\n" +
        "  action     lock a cited bitrate, or accept that this bus is excluded from the bandwidth gate",
      hits: ["bus.i2c.external"],
      faults: ["bus.i2c.external"],
      affected: [],
      targets: [],
      count: 1,
      count_figure: { value: 1, unit: "findings", kind: "derived", by: "gui._cards" },
      observed: null,
      expected: null,
      group_title: "",
      instead: [],
      alternatives: [],
      figure_note: null,
      shape: "topology/buses/*/bandwidth",
      label: "Check",
      also_in_run: false,
    },
  ],
  cli_line: null,
};
