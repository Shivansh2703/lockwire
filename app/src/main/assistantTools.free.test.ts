// A free build: the tool table and the guides as main has them without pro/ (nothing here imports it), and no file of main outside pro/ imports pro/.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it, vi } from "vitest";

vi.mock("electron", () => ({ app: { getPath: () => "/nowhere" }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));

const { TOOLS } = await import("./assistantTools");
const { capabilities } = await import("./assistantKnowhow");
const { listGuides } = await import("./assistantGuides");

it("a free build lists none of the Agent tab's Pro tools, nor their guides, and main outside pro/ never imports pro/", async () => {
  const paid = ["search-drive", "sweep-dimension", "propose-mechanism", "render-step", "add-wire", "place-fuse", "run-erc", "auto-wire"];
  const names = TOOLS.map((t) => t.name);
  for (const n of paid) expect(names, n).not.toContain(n);
  const can = capabilities(TOOLS, new Set(), "part of Pro");
  for (const n of paid) expect(can, n).not.toContain(`- ${n}:`);
  const guides = (await listGuides()).text;
  for (const g of ["building-a-mechanism", "finding-a-drive", "reviewing-the-robot", "which-pack-fits", "why-it-browned-out"]) expect(guides, g).not.toContain(`- ${g}:`);
  // The one sanctioned door is proRegistry.ts's glob (a build without pro/ still compiles); no import statement names pro/.
  for (const f of readdirSync(import.meta.dirname).filter((x) => x.endsWith(".ts"))) {
    expect(readFileSync(join(import.meta.dirname, f), "utf8"), f).not.toMatch(/(?:from|import)\s*\(?\s*["']\.\/pro\//);
  }
});
