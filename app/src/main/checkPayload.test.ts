import { describe, expect, it } from "vitest";

import type { CheckPayload } from "../shared/api.gen";
import { checkPayloadToFace } from "./checkPayload";

const DOC: CheckPayload = {
  format_version: "1",
  schema_version: "1",
  cli_line: "lockwire check /tmp/lockwire.yaml",
  robot: "Holybro X500 V2 PX4 reference quadrotor",
  verdict: "NO-GO",
  exit_code: 1,
  counts: { violation: 2, warning: 0, not_scored: 9 },
  scored: 10,
  headline: "Don't power it on yet - 2 things do not add up",
  findings: [
    {
      severity: "violation",
      quantity: "budgets/bud.power.pm02-5v",
      observed: "35.20 W",
      expected: "<= 15.60 W",
      owner: "power-integration",
      source: "docs/reference_robot_specs.md",
      witness: null,
      action: "shed load",
    },
  ],
  artifact_type: "lockwire-check-report",
  forces: {
    gravity: { value: 9.81, unit: "m/s^2", kind: "declared" },
    mass_total: { value: 1.2, unit: "kg", kind: "derived-by-compute_system_cg" },
    cg: { value: [0, 0, 0], unit: "m", kind: "derived-by-compute_system_cg" },
    masses: [],
    mounts: [],
    mount_shapes: [],
    not_scored: [],
  },
};

describe("checkPayloadToFace", () => {
  it("test_carries_every_field_lockwire_api_check_actually_emits", () => {
    const face = checkPayloadToFace(DOC, "lockwire check /tmp/lockwire.yaml");
    expect(face.robot).toBe(DOC.robot);
    expect(face.verdict).toBe("NO-GO");
    expect(face.scored).toBe(10);
    expect(face.violations).toBe(2);
    expect(face.warnings).toBe(0);
    expect(face.unchecked).toBe(9);
    expect(face.findings).toBe(DOC.findings);
    expect(face.cli_line).toBe("lockwire check /tmp/lockwire.yaml");
  });

  it("test_reads_the_headline_from_the_payload_rather_than_composing_it", () => {
    // E2-hl: `doc.headline` is `api.check`'s own field (`report.verdict_headline`,
    // shared with `gui.py`'s HTML page) -- this function must read it
    // verbatim, never assemble a sentence from the counts itself.
    const face = checkPayloadToFace(DOC, "lockwire check /tmp/lockwire.yaml");
    expect(face.headline).toBe(DOC.headline);
    expect(face.headline).toBe("Don't power it on yet - 2 things do not add up");
  });

  it("test_leaves_gui_build_payloads_remaining_half_of_the_join_at_its_honest_empty_default", () => {
    // qualifier/cards come from the page payload, which no door joins with a check reply
    // (see checkPayload.ts) -- this function must never fabricate them.
    const face = checkPayloadToFace(DOC, "lockwire check /tmp/lockwire.yaml");
    expect(face.qualifier).toBe("");
    expect(face.cards).toEqual([]);
  });
});
