import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { fmtQty, tidyNumbers } from "./format";

describe("shared parity table", () => {
  it("matches Python's format_display", () => {
    const table = JSON.parse(readFileSync(resolve(process.cwd(), "../tests/fixtures/number_format_parity.json"), "utf8"));
    expect(table.map((c: { value: number; unit: string; vs: number | null }) => fmtQty(c.value, c.unit, c.vs ?? undefined))).toEqual(table.map((c: { expected: string }) => c.expected));
  });
  it("tidyNumbers leaves 'USB 2.0 A' and mid-word numbers alone", () => {
    expect(tidyNumbers("USB 2.0 A, rev1.25 V, x 275.52 W")).toBe("USB 2.0 A, rev1.25 V, x 275.5 W");
  });
});

describe("fmtQty", () => {
  it("one format: 1 decimal from 10 up, 3 sig below, prefixes, ≈0", () => {
    expect([fmtQty(275.52, "W"), fmtQty(370, "W"), fmtQty(35.2, "W"), fmtQty(0.48, "W")]).toEqual(["275.5 W", "370 W", "35.2 W", "0.48 W"]);
    expect([fmtQty(0.002974, "kg"), fmtQty(3, "kg"), fmtQty(1e-9, "kg"), fmtQty(0.2, "A"), fmtQty(0, "kg")]).toEqual(["2.97 g", "3 kg", "≈0", "200 mA", "0 kg"]);
  });
  it("never rounds a gap away", () => {
    expect(fmtQty(15.64, "W", 15.6)).toBe("15.64 W");
    expect(tidyNumbers("275.52 W rolled-up draw", 275.5)).toBe("275.52 W rolled-up draw");
    expect(tidyNumbers("<= 15.60 W")).toBe("<= 15.6 W");
  });
});
