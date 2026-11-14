import { afterEach, describe, expect, it } from "vitest";
// @ts-expect-error plain .mjs driver
import { witnessEnv } from "./e2e-drive.mjs";

const saved = process.env.LOCKWIRE_WITNESS_CONFIG;
afterEach(() => {
  if (saved === undefined) delete process.env.LOCKWIRE_WITNESS_CONFIG;
  else process.env.LOCKWIRE_WITNESS_CONFIG = saved;
});

describe("witnessEnv", () => {
  it("defaults to a file inside the user-data dir", () => {
    delete process.env.LOCKWIRE_WITNESS_CONFIG;
    expect(witnessEnv({}, "/tmp/ud").LOCKWIRE_WITNESS_CONFIG).toBe("/tmp/ud/witness.toml");
  });
  it("keeps a caller-provided value, from env option or process.env", () => {
    delete process.env.LOCKWIRE_WITNESS_CONFIG;
    expect(witnessEnv({ LOCKWIRE_WITNESS_CONFIG: "/x" }, "/tmp/ud").LOCKWIRE_WITNESS_CONFIG).toBe("/x");
    process.env.LOCKWIRE_WITNESS_CONFIG = "/y";
    expect(witnessEnv({}, "/tmp/ud").LOCKWIRE_WITNESS_CONFIG).toBeUndefined();
  });
});
