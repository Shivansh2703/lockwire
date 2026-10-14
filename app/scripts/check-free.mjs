#!/usr/bin/env node
// `npm run check:free` -- proves the free app stands on its own.
//
// Copies app/ without any `pro/` folder, then in the copy: typecheck, build (electron-vite), vitest,
// and a grep proving no paid identifier is left in the copy's src or in its built bundles. If a
// paid import is planted in a free file the typecheck, the tests or the grep goes red.
//
// The copy lives beside app/ (`<repo>/.app-free-<pid>`), not in the OS temp dir, so the tests that
// reach the repo root through `..` still resolve. It is removed at the end.
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, readFileSync, readdirSync, rmSync, symlinkSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..");
const COPY = join(APP, "..", `.app-free-${process.pid}`);

// Identifiers a free tree must not contain (case-sensitive).
export const PAID = [
  "run:start", "run:poll", "run:cancel", "contract:preflight", "choose-recording",
  "RunPanel", "TestFace", "TestInput", "PreflightPayload", "RunStatePayload", "HealthSummary",
  "health_words", "actuator-review-draft", "analysis:start",
];
// The one string the free app may carry about the paid half (owner one dim line, no link).
// The product name comes from build metadata, never a second authored literal (branding.test.ts).
const PRODUCT = JSON.parse(readFileSync(join(APP, "package.json"), "utf8")).build.productName;
export const ALLOWED_STRING = `Test / Run: ${PRODUCT} Pro`;
// Command-class strings that must not survive into a free build's bundles.
const BUNDLE_PAID = ["run:start", "run:poll", "run:cancel", "contract:preflight", "file:choose-recording", "analysis:start"];

// Known exceptions, each with its reason. Remove an entry when its reason goes away.
const SKIP_FILES = new Map([
]);
const ALLOWED_IN = new Map([
]);

function run(cmd, args, cwd, label) {
  console.log(`\n== ${label}: ${cmd} ${args.join(" ")}`);
  const r = spawnSync(cmd, args, { cwd, stdio: "inherit" });
  if (r.status !== 0) {
    console.error(`check:free FAILED at: ${label}`);
    process.exitCode = 1;
    return false;
  }
  return true;
}

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    return e.isDirectory() ? walk(p) : [p];
  });
}

/** Paid identifiers found in `text`, with line numbers; the allowed sentence is blanked out first. */
export function findPaid(text, ids = PAID) {
  const hits = [];
  text.split("\n").forEach((line, i) => {
    const clean = line.split(ALLOWED_STRING).join("");
    for (const id of ids) if (clean.includes(id)) hits.push({ line: i + 1, id });
  });
  return hits;
}

function grepSrc() {
  console.log("\n== grep: paid identifiers in the free copy's src");
  const root = join(COPY, "src");
  let bad = 0;
  for (const file of walk(root)) {
    const rel = relative(COPY, file).split(sep).join("/");
    if (SKIP_FILES.has(rel) || !/\.(tsx?|css|json|html|mjs)$/.test(file)) continue;
    const allowed = ALLOWED_IN.get(rel) ?? new Set();
    for (const { line, id } of findPaid(readFileSync(file, "utf8"))) {
      if (allowed.has(id)) continue;
      console.error(`  ${rel}:${line}  paid identifier "${id}"`);
      bad += 1;
    }
  }
  if (bad) {
    console.error(`check:free FAILED: ${bad} paid identifier(s) in free code`);
    process.exitCode = 1;
  } else console.log("  clean");
}

function grepBundles() {
  console.log("\n== grep: paid command classes in the free build's bundles");
  const out = join(COPY, "out");
  let bad = 0;
  for (const file of walk(out).filter((f) => f.endsWith(".js"))) {
    for (const { id } of findPaid(readFileSync(file, "utf8"), BUNDLE_PAID)) {
      console.error(`  ${relative(COPY, file)}  carries "${id}"`);
      bad += 1;
    }
  }
  if (bad) {
    console.error(`check:free FAILED: ${bad} paid command(s) in the free build`);
    process.exitCode = 1;
  } else console.log("  clean");
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (!existsSync(join(APP, "node_modules"))) throw new Error("run `npm ci` in app/ first");
  rmSync(COPY, { recursive: true, force: true });
  try {
    cpSync(APP, COPY, {
      recursive: true,
      filter: (src) => {
        const parts = relative(APP, src).split(sep);
        return !parts.some((p) => ["node_modules", "out", "dist", "build", "pro"].includes(p));
      },
    });
    if (walk(COPY).some((f) => f.split(sep).includes("pro"))) throw new Error("a pro/ folder leaked into the copy");
    symlinkSync(join(APP, "node_modules"), join(COPY, "node_modules"), "dir");
    console.log(`free copy: ${COPY}`);

    // Typecheck first: a planted paid import fails here, with the file named.
    if (run("npx", ["tsc", "--noEmit"], COPY, "typecheck") &&
        run("npx", ["electron-vite", "build"], COPY, "build") &&
        // The packaged-app and launched-Electron e2e files need a packaged/launched app; app-packaged covers them.
        run("npx", ["vitest", "run", "--exclude", "**/*.e2e.test.ts", "--exclude", "scripts/build.test.ts", "--exclude", "**/node_modules/**"], COPY, "vitest")) {
      grepBundles();
    }
    grepSrc();
  } finally {
    if (!process.env.CHECK_FREE_KEEP) rmSync(COPY, { recursive: true, force: true });
  }
  console.log(process.exitCode ? "\ncheck:free: RED" : "\ncheck:free: green");
}
