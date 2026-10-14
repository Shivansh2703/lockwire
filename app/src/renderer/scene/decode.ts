/**
 * Decode the packed geometry `lockwire`'s sidecar ships for a scene (docs/wire-format.md).
 *
 * `PackedVectors` (positions, normals): little-endian int16, `value[i] = int16[i] * scale +
 * offset[i % 3]`. Normals omit `scale`/`offset` on the wire (both `null`/absent) — their
 * implicit scale is `1/32767` and offset `[0, 0, 0]`, since they already lie in [-1, 1].
 *
 * `PackedIndices`: little-endian, unsigned, `type` names the width (`"u16"` | `"u32"`) — a
 * decoder must branch on it, never assume one.
 *
 * `translation` is a label-placement ORIGIN, never a transform: positions are already
 * world-space. `decodePartPositions` below decodes only `positions` and never reads
 * `translation` at all — that is what keeps the bug (applying it) out of this file rather
 * than merely untested.
 */

const NORMAL_SCALE = 1 / 32767;

export interface PackedVectors {
  type: "i16";
  data: string;
  scale?: number;
  offset?: [number, number, number];
}

export interface PackedIndices {
  type: "u16" | "u32";
  data: string;
}

function base64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** `int16[i] * scale + offset[i % 3]`, per docs/wire-format.md. Normals (no `scale`/`offset`
 * on the wire) fall back to the implicit `1/32767` scale and `[0, 0, 0]` offset. */
export function decodePackedVectors(packed: PackedVectors): Float64Array {
  const bytes = base64ToBytes(packed.data);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const count = bytes.byteLength / 2;
  const scale = packed.scale ?? NORMAL_SCALE;
  const offset = packed.offset ?? [0, 0, 0];
  const out = new Float64Array(count);
  for (let i = 0; i < count; i++) {
    out[i] = view.getInt16(i * 2, true) * scale + offset[i % 3]!;
  }
  return out;
}

/** Branches on `type` — `"u16"` is `Uint16Array`-shaped, `"u32"` is `Uint32Array`-shaped. */
export function decodePackedIndices(packed: PackedIndices): Uint32Array {
  const bytes = base64ToBytes(packed.data);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const stride = packed.type === "u32" ? 4 : 2;
  const count = bytes.byteLength / stride;
  const out = new Uint32Array(count);
  for (let i = 0; i < count; i++) {
    out[i] = packed.type === "u32" ? view.getUint32(i * stride, true) : view.getUint16(i * stride, true);
  }
  return out;
}

/** A part's decoded vertex positions. `translation` is a label origin (docs/wire-format.md)
 * and is never applied here — the positions on the wire are already world-space. */
export function decodePartPositions(part: { positions: PackedVectors }): Float64Array {
  return decodePackedVectors(part.positions);
}
