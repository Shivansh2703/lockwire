import { describe, expect, it } from "vitest";

// @ts-expect-error -- plain .mjs script, no types
import { ALLOWED_STRING, findPaid } from "./check-free.mjs";

describe("check:free's grep", () => {
  it("finds a paid identifier and names its line", () => {
    expect(findPaid("ok\nimport { RunPanel } from './x';")).toEqual([{ line: 2, id: "RunPanel" }]);
  });

  it("lets the one dim Pro sentence through and nothing else", () => {
    expect(findPaid(`<span>${ALLOWED_STRING}</span>`)).toEqual([]);
    expect(findPaid(`<span>${ALLOWED_STRING}</span> run:start`)).toEqual([{ line: 1, id: "run:start" }]);
  });
});
