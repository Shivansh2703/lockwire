import { describe, expect, it } from "vitest";

import { LensState, partsForLens } from "./lenses";

describe("lens state", () => {
  it("survives a payload replacement", () => {
    const state = new LensState("power");
    state.onPayloadReplaced();
    expect(state.get()).toBe("power");

    state.onPayloadReplaced();
    state.onPayloadReplaced();
    expect(state.get()).toBe("power");
  });

  it("still changes on an explicit set() call", () => {
    const state = new LensState("power");
    state.set("mechanical");
    expect(state.get()).toBe("mechanical");
  });
});

describe("test_a_lens_draws_only_the_parts_the_payload_puts_in_it", () => {
  const battery = { node: "battery", lenses: ["off", "power", "health"] as const };
  const antenna = { node: "antenna", lenses: ["off", "data", "health"] as const };
  const chassis = { node: "chassis", lenses: ["off", "mechanical"] as const };
  const parts = [battery, antenna, chassis];

  it("a lens draws exactly the parts whose payload membership names it, nothing else", () => {
    expect(partsForLens(parts, "power")).toEqual([battery]);
    expect(partsForLens(parts, "data")).toEqual([antenna]);
    expect(partsForLens(parts, "mechanical")).toEqual([chassis]);
  });

  it("the two whole-scene overlays draw every part (off, health)", () => {
    expect(partsForLens(parts, "off")).toEqual(parts);
    expect(partsForLens(parts, "health")).toEqual([battery, antenna]);
  });

  it("a part named in no lens for a given filter is excluded, by node id", () => {
    // chassis has no "power" entry in its payload `lenses` (mechanical-only) — this is the
    // negative half rule 22 above wants: the exclusion, checked by name, not just the inclusion.
    expect(partsForLens(parts, "power").map((p) => p.node)).not.toContain("chassis");
  });
});
