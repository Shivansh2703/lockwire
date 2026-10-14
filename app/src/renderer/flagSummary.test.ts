import { describe, expect, it } from "vitest";
import { flagSummary } from "./FindingList";

const T = (id: string, label: string) => ({ kind: "device", id, label, parts: [id] }) as never;

describe("flagSummary (the 3D view's text twin)", () => {
  it("names the flagged parts in plain words, at most three, deduplicated", () => {
    const targets = ["a", "b", "c", "d"].map((x) => T(x, x.toUpperCase()));
    expect(flagSummary([], targets)).toBe("");
    expect(flagSummary(["a", "a"], targets)).toBe("1 part flagged: A");
    expect(flagSummary(["a", "b", "c", "d"], targets)).toBe("4 parts flagged: A, B, C and 1 more");
  });
});
