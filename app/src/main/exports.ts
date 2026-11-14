// File > Export and Save a Copy. Exports run the same `lockwire export` / `lockwire report` the CLI has
// (the sidecar's own binary, never a PATH search); the target comes from a native save dialog.
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, extname, isAbsolute, join, relative, resolve } from "node:path";
import { promisify } from "node:util";

import { BrowserWindow, dialog, shell } from "electron";

import { pickSaveTarget } from "./dialogs";
import { runSidecarCommand } from "./ipcKit";
import { resolveSidecarCommand, scrubChildEnv } from "./sidecar";

/** `suffix` is what follows the lockfile's name in the default file name. */
export interface ExportKind { label: string; fmt: string; suffix: string; mesh?: boolean }
export const EXPORTS: ExportKind[] = [
  { label: "URDF…", fmt: "urdf", suffix: ".urdf" },
  { label: "SDF…", fmt: "sdf", suffix: ".sdf" },
  { label: "MJCF…", fmt: "mjcf", suffix: ".xml" },
  { label: "STL…", fmt: "stl", suffix: "-stl", mesh: true },
  { label: "OBJ…", fmt: "obj", suffix: "-obj", mesh: true },
  { label: "COLLADA…", fmt: "dae", suffix: "-dae", mesh: true },
  { label: "Parts list (CSV)…", fmt: "bom", suffix: ".parts.csv" },
  { label: "Power budget (CSV)…", fmt: "budget", suffix: ".budget.csv" },
  { label: "Report (HTML)…", fmt: "report", suffix: ".report.html" },
  { label: "Report (PDF)…", fmt: "pdf", suffix: ".pdf" },
];

const stem = (contract: string) => basename(contract, extname(contract));

async function fail(title: string, detail: string): Promise<void> {
  await dialog.showMessageBox({ type: "error", message: title, detail });
}

/** The CLI's own HTML report, printed to PDF by a hidden window. Bytes only reach `out` once the PDF exists. */
async function writeReportPdf(contract: string, out: string): Promise<void> {
  let tmp = "";
  let win: BrowserWindow | undefined;
  try {
    tmp = mkdtempSync(join(tmpdir(), "lockwire-pdf-"));
    const html = join(tmp, "report.html");
    win = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
    await promisify(execFile)(resolveSidecarCommand().command, ["report", contract, "--out", html], { env: scrubChildEnv(process.env), timeout: 60_000 });
    await win.loadFile(html);
    writeFileSync(out, await win.webContents.printToPDF({ printBackground: true }));
  } finally {
    win?.destroy();
    if (tmp) rmSync(tmp, { recursive: true, force: true });
  }
}

/** STL/OBJ/COLLADA write one file per link plus an assembly, so the chosen path is a folder; the engine's serve route does it (and keeps it inside the lockfile's folder). */
async function exportMesh(contract: string, kind: ExportKind, out: string): Promise<void> {
  try {
    const reply = await runSidecarCommand({ command: "export", args: { contract, format: kind.fmt, out, force: true } });
    if (reply.refusal !== null) return fail("Could not export", reply.refusal);
  } catch (error) {
    return fail("Could not export", error instanceof Error ? error.message : String(error));
  }
  shell.showItemInFolder(out);
}

export async function exportRobot(contract: string, kind: ExportKind): Promise<void> {
  if (!contract) return fail("No report to save", "Open a robot first, then save its report.");
  const out = await pickSaveTarget(`Export ${kind.label.replace("\u2026", "")}`, join(dirname(contract), stem(contract) + kind.suffix), kind.label, kind.mesh ? "*" : kind.suffix.split(".").pop()!);
  if (out === null) return;
  if (kind.mesh) {
    const folder = dirname(contract);
    const inside = relative(folder, out);
    if (inside.startsWith("..") || isAbsolute(inside)) return fail("Pick a place inside the robot's folder", `Exports must go inside the robot's folder (${folder}). Pick a place there.`);
    if (existsSync(out) && readdirSync(out).length > 0) {
      if (!existsSync(join(out, `assembly.${kind.fmt}`))) return fail("That folder is not empty", `${out} holds files that are not a Lockwire export. Pick an empty folder or a new one.`);
      const { response } = await dialog.showMessageBox({ type: "question", message: `Replace the Lockwire export in ${out}?`, buttons: ["Replace", "Cancel"], defaultId: 1, cancelId: 1 });
      if (response !== 0) return;
    }
    return exportMesh(contract, kind, out);
  }
  if (kind.fmt === "pdf") {
    try {
      await writeReportPdf(contract, out);
    } catch (error) {
      const e = error as { stdout?: string; message: string };
      return fail("Could not save the PDF", (e.stdout || e.message).trim());
    }
    shell.showItemInFolder(out);
    return;
  }
  const args = kind.fmt === "report" ? ["report", contract, "--out", out] : ["export", contract, "--format", kind.fmt, "--out", out, "--force"];
  try {
    await promisify(execFile)(resolveSidecarCommand().command, args, { env: scrubChildEnv(process.env), timeout: 60_000 });
  } catch (error) {
    const e = error as { stdout?: string; message: string };
    return fail("Could not export", (e.stdout || e.message).trim());
  }
  shell.showItemInFolder(out);
}

/** model.source of a lockfile when it is a plain relative file path (the only kind that breaks in another folder). */
function relativeModelSource(text: string): string | null {
  const block = /^model:[ \t]*\r?\n((?:[ \t]+\S.*\r?\n?|[ \t]*\r?\n)*)/m.exec(text)?.[1] ?? "";
  const indent = /^([ \t]+)\S/m.exec(block)?.[1] ?? " ";
  const src = new RegExp(`^${indent}source:[ \\t]*["']?([^"'#\\r\\n]*?)["']?[ \\t]*(?:#.*)?$`, "m").exec(block)?.[1] ?? "";
  const plain = src !== "" && src !== "lockfile" && !src.includes(" ") && !src.startsWith("~") && !src.startsWith("\\") && !isAbsolute(src) && !/^([A-Za-z][A-Za-z0-9+.-]+|[A-Za-z]):/.test(src);
  return plain ? src : null;
}

/** The witness on the lockfile's model provenance, read by the engine (`lockwire blame --json`); null when none.
 *  Any failure is null too: blame stops on a model provenance with no witness, which is that same case. */
// What `lockwire blame` prints for a provenance with no witness; it is a placeholder, not a name to carry into the copy.
const NO_NAME_ON_RECORD = "no name on record";
async function modelWitness(contract: string): Promise<string | null> {
  try {
    const { stdout } = await promisify(execFile)(resolveSidecarCommand().command, ["blame", contract, "--json"], { env: scrubChildEnv(process.env), timeout: 60_000 });
    const row = (JSON.parse(stdout) as { path: string; witness?: unknown }[]).find((r) => r.path === "model");
    return typeof row?.witness === "string" && row.witness.trim() && row.witness !== NO_NAME_ON_RECORD ? row.witness : null;
  } catch {
    return null;
  }
}

const REPOINTED = "The model path was updated so the copy still finds its model.";
const NEWER_FORMAT = " The copy is saved in the newer 0.7 format, which Lockwire 0.2.8 and older cannot open.";

/** Copy of the open lockfile, for any robot. Into another folder, a relative model path is re-pointed (via `lockwire declare`) at the original model. */
export async function saveCopy(contract: string): Promise<void> {
  const out = await pickSaveTarget("Save a copy", join(dirname(contract), `${stem(contract)} copy${extname(contract)}`), "Lockfile", "yaml");
  if (out === null || resolve(out) === resolve(contract)) return;
  let copied = false;
  let repointed: string | null = null;
  try {
    copyFileSync(contract, out);
    copied = true;
    const src = dirname(resolve(out)) === dirname(resolve(contract)) ? null : relativeModelSource(readFileSync(contract, "utf8"));
    if (src !== null) {
      const path = relative(dirname(resolve(out)), resolve(dirname(contract), src)).split("\\").join("/");
      // the same model file, moved: keep whoever vouched for it; with nobody on record, record no name (never the computer login)
      const witness = await modelWitness(contract);
      const reply = await runSidecarCommand({ command: "declare", args: { contract: out, path: "model/source", value: path, source: "Save a Copy", ...(witness === null ? {} : { witness }), expected_revision: createHash("sha256").update(readFileSync(out)).digest("hex") } });
      if (reply.refusal !== null) throw new Error(reply.refusal);
      repointed = witness === null ? REPOINTED + NEWER_FORMAT : REPOINTED;
    }
  } catch (error) {
    if (copied) rmSync(out, { force: true }); // never leave a copy that cannot find its model
    const e = error as { stdout?: string; message: string };
    return fail("Could not save the copy", (e.stdout || e.message).trim());
  }
  if (repointed !== null) await dialog.showMessageBox({ type: "info", message: "Saved a copy", detail: repointed });
  shell.showItemInFolder(out);
}

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** Save picture: write the 3D view's PNG where a save dialog says. Nothing is written on cancel; true when written. */
export async function savePicture(name: string, png: Buffer): Promise<boolean> {
  if (!png.subarray(0, 8).equals(PNG_SIGNATURE)) throw new Error("The picture could not be made. Try again.");
  const out = await pickSaveTarget("Save picture", `${name.replace(/[\\/:*?"<>|]/g, "_") || "robot"}.png`, "Picture", "png");
  if (out === null) return false;
  try {
    writeFileSync(out, png);
  } catch (error) {
    await fail("Could not save the picture", error instanceof Error ? error.message : String(error));
    return false;
  }
  shell.showItemInFolder(out);
  return true;
}
