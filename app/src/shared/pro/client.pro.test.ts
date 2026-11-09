import { describe, expect, it } from "vitest";

import type { HealthPayload, MachineDocumentPlugin, PreflightPayload } from "./types";

/** The paid plugin's union members, keyed by their own discriminant, as client.ts does for the free union. */
type ProDocumentFor<C extends MachineDocumentPlugin["artifact_type"]> = Extract<MachineDocumentPlugin, { artifact_type: C }>;

describe("the paid union members are reachable by their discriminant", () => {
  it("test_the_paid_union_members_are_reachable_from_the_client_type", () => {
    const preflight = { artifact_type: "lockwire-preflight-report" } as unknown as ProDocumentFor<"lockwire-preflight-report">;
    const health = { artifact_type: "lockwire-health-report" } as unknown as ProDocumentFor<"lockwire-health-report">;
    const asPreflight: PreflightPayload = preflight;
    const asHealth: HealthPayload = health;
    expect([asPreflight, asHealth].map((r) => r.artifact_type)).toEqual([
      "lockwire-preflight-report",
      "lockwire-health-report",
    ]);
  });
});
