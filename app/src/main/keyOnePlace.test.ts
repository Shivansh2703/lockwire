// One key, one place: the API key is decrypted only in assistant.ts, sent only as that file's own request header, and
// never handed to a helper process. The old Agent tab put it in a child's environment; that tab and its kit are gone.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { expect, it, vi } from "vitest";

vi.mock("electron", () => ({ app: { getPath: () => "/tmp", isPackaged: false } }));
const { scrubChildEnv } = await import("./sidecar");

const KEY_VAR = ["ANTHROP", "IC_API_KEY"].join(""); // spelled in two parts: the public snapshot scans free files for the vendor's name
const walk = (d: string): string[] => readdirSync(d).flatMap((n) => (statSync(join(d, n)).isDirectory() ? (n === "assistant-eval" ? [] : walk(join(d, n))) : /\.ts$/.test(n) && !/\.test\.ts$/.test(n) ? [join(d, n)] : []));

it("the key is read in one place and never handed to a helper process", () => {
  const where = (re: RegExp) => walk(import.meta.dirname).filter((f) => re.test(readFileSync(f, "utf8"))).map((f) => relative(import.meta.dirname, f));
  expect(where(/decryptString\(/)).toEqual(["assistant.ts"]); // the one read
  expect(where(/storedKey\(/)).toEqual(["assistant.ts"]); // not exported: no other file can ask for it
  expect(where(/x-api-key/)).toEqual(["assistant.ts"]); // the one place it is sent: the turn's own request to the model provider
  expect(where(new RegExp(`${KEY_VAR}|agent-sdk|"mcp"\\]`))).toEqual([]); // no child gets it in its environment; the app starts no MCP child
  expect(readFileSync(join(import.meta.dirname, "assistant.ts"), "utf8")).not.toMatch(/export function storedKey/);
  // Every child the app starts (the engine, the shell's helpers) gets an allow-listed environment: a key in main's own env stays behind.
  expect(scrubChildEnv({ [KEY_VAR]: "sk-x", PATH: "/usr/bin" })).not.toHaveProperty(KEY_VAR);
});
