import { describe, expect, it } from "vitest";
import { witnessWords } from "./witness";

describe("witnessWords", () => {
  it("says what the tool says", () => {
    expect(witnessWords("w0")).toBe("none named (declared, not measured)");
    expect(witnessWords("w2")).toBe("the model file");
    expect(witnessWords("w5")).toBe("none named");
    expect(witnessWords("human")).toBe("a person, not named");
    expect(witnessWords("live")).toBe("live reading");
    expect(witnessWords(null)).toBe("no name on record");
    expect(witnessWords(undefined)).toBe("no name on record");
    expect(witnessWords("")).toBe("no name on record");
    expect(witnessWords("Maya")).toBe("Maya");
    expect(witnessWords("w0 Maya")).toBe("w0 Maya");
  });
});
