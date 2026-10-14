import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { decodePackedIndices, decodePackedVectors, decodePartPositions } from "./decode";

// tests/fixtures/scene_roundtrip.json is generated straight from
// lockwire.scene_view._pack_vectors / _pack_indices (Python's own production packer) — see
// the "_comment" field in the fixture itself. Decoding it and comparing to its "raw" arrays
// anchors this test against Python's numbers, not a value this test computed with the same
// formula (the box's decisive review item).
// (Resolved in two steps, not `new URL(rel, import.meta.url)`: Vite statically rewrites that
// exact pattern into a dev-server asset URL, which breaks plain file reads under vitest.)
const here = path.dirname(fileURLToPath(import.meta.url));
const fixturePath = path.resolve(here, "../../../../tests/fixtures/scene_roundtrip.json");
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));

function closeTo(actual: ArrayLike<number>, expected: number[], tolerance: number) {
  expect(actual.length).toBe(expected.length);
  for (let i = 0; i < expected.length; i++) {
    expect(Math.abs(actual[i]! - expected[i]!)).toBeLessThanOrEqual(tolerance);
  }
}

describe("decodePackedVectors", () => {
  it("decodes a packed fixture back to its original positions (Python's own numbers)", () => {
    const { raw, packed } = fixture.positions;
    const decoded = decodePackedVectors(packed);
    // Quantization error is at most scale/2 (docs/wire-format.md).
    closeTo(decoded, raw, packed.scale / 2 + 1e-9);
  });

  it("applies the offset per axis, not per vertex", () => {
    const { packed } = fixture.positions;
    // The fixture's three axes have distinct offsets (0.5, 10.0, -5.0) precisely so a
    // decoder that used a single scalar offset instead of offset[i % 3] cannot pass.
    expect(new Set(packed.offset).size).toBe(3);
    const decoded = decodePackedVectors(packed);
    closeTo(decoded, fixture.positions.raw, packed.scale / 2 + 1e-9);
  });

  it("decodes normals with the implicit 1/32767 scale and zero offset (no scale/offset key)", () => {
    const { raw, packed } = fixture.normals;
    expect(packed.scale).toBeUndefined();
    expect(packed.offset).toBeUndefined();
    const decoded = decodePackedVectors(packed);
    closeTo(decoded, raw, 1 / 32767 / 2 + 1e-9);
  });
});

describe("decodePackedIndices", () => {
  it("reads u16 indices for a part that declares u16", () => {
    const { raw, packed } = fixture.indices_u16;
    expect(packed.type).toBe("u16");
    expect(Array.from(decodePackedIndices(packed))).toEqual(raw);
  });

  it("reads u32 indices when the part declares them", () => {
    const { raw, packed } = fixture.indices_u32;
    expect(packed.type).toBe("u32");
    // 70000 does not fit in u16 — this is exactly why the part declared u32.
    expect(Math.max(...raw)).toBeGreaterThan(65535);
    expect(Array.from(decodePackedIndices(packed))).toEqual(raw);
  });
});

describe("decodePartPositions", () => {
  it("uses translation as a label origin and never applies it to positions", () => {
    const { raw, packed } = fixture.positions;
    const part = { positions: packed, translation: fixture.translation as number[] };
    const decoded = decodePartPositions(part);
    // If translation were (wrongly) added, every decoded value would be off by its axis's
    // translation component (100, -200, 50) — none of which are within the packing
    // tolerance of zero, so this also catches the bug via the tolerance check below.
    closeTo(decoded, raw, packed.scale / 2 + 1e-9);
    const translated = raw.map((v: number, i: number) => v + fixture.translation[i % 3]);
    for (let i = 0; i < translated.length; i++) {
      expect(Math.abs(decoded[i]! - translated[i]!)).toBeGreaterThan(1);
    }
  });
});
