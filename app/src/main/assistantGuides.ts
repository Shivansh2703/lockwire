// Guides the Assistant loads on demand: the short ones in ./guides, written for it, and Lockwire's own docs/guides pages (the public ones; Pro adds its own).
// Both are bundled as text at build time. A guide is found by its name in a map, never by a path, and no guide is sent with every turn:
// the system text carries only the titles.
import type { LocalResult } from "./assistantTools";

const named = (found: Record<string, string>, prefix: string): [string, string][] =>
  Object.keys(found).sort().map((path) => [prefix + path.split("/").pop()!.replace(/\.md$/, ""), found[path]!]);
const GUIDES = new Map(named(import.meta.glob<string>("./guides/*.md", { query: "?raw", import: "default", eager: true }), ""));
// Lockwire's own pages: only the ones the public tree keeps. The pages the public tree leaves out are Pro's and arrive through `registerDocs`
// from main/pro/guides.ts, so a free build neither bundles nor names them. A Vite glob must be literal, so the stripped names are written once, here;
// a test fails when this line and the list of left-out pages disagree.
const DOCS = new Map(named(import.meta.glob<string>(["../../../docs/guides/*.md", "!../../../docs/guides/{preflight,ros2,ros2-live,ros2-monitor,fleet-drift}.md"], { query: "?raw", import: "default", eager: true }), "docs/"));

/** Pro's pages join the same map, the way a Pro tool joins the tool table (a glob's result: path to text). */
export const registerDocs = (found: Record<string, string>): void => { for (const [name, text] of named(found, "docs/")) DOCS.set(name, text); };

/** Pro's short guides join the free ones (a glob's result: path to text). A name already taken would silently replace a free guide, so it throws. */
export const registerGuides = (found: Record<string, string>): void => {
  for (const [name, text] of named(found, "")) { if (GUIDES.has(name)) throw new Error(`guide "${name}" is registered twice`); GUIDES.set(name, text); }
};

/** A guide's title and its one line: `# Title` then the next line, or a docs page's `title:` and `description:`. */
const head = (text: string): [string, string] => {
  const page = /^---\ntitle: (.*)\ndescription: (.*)\n/.exec(text);
  const [title = "", line = ""] = page ? page.slice(1) : text.split("\n").filter(Boolean);
  return [title.replace(/^# /, ""), line];
};
const lines = (guides: Map<string, string>) => [...guides].map(([name, text]) => `- ${name}: ${head(text).join(". ")}`).join("\n");

/** The titles of the short guides, for the system text. */
export const guideTitles = (): string[] => [...GUIDES.values()].map((text) => head(text)[0]);

export const listGuides = async (): Promise<LocalResult> => ({
  own: true,
  text: `Short guides (name: title. what it covers):\n${lines(GUIDES)}\n\nLockwire's own documentation pages:\n${lines(DOCS)}\n\nCall read-guide with a name.`,
});

export const readGuide = async (input: Record<string, unknown>): Promise<LocalResult> => {
  const name = typeof input.name === "string" ? input.name.trim() : "";
  const text = GUIDES.get(name) ?? DOCS.get(name);
  return text ? { text, own: true } : { text: `There is no guide called "${name.slice(0, 80)}". Call list-guides to see the names.`, error: true, own: true };
};
