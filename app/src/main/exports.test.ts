import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pick = vi.fn();
const execFile = vi.fn();
const showMessageBox = vi.fn();
const showItemInFolder = vi.fn();
const runSidecar = vi.fn();
vi.mock("./ipcKit", () => ({ runSidecarCommand: (r: unknown) => runSidecar(r) }));
vi.mock("./dialogs", () => ({ pickSaveTarget: (...a: unknown[]) => pick(...a) }));
vi.mock("./sidecar", () => ({ resolveSidecarCommand: () => ({ command: "/venv/bin/lockwire", args: ["serve"] }), scrubChildEnv: (e: object) => e }));
vi.mock("node:child_process", () => { const mod = { execFile: (...a: unknown[]) => execFile(...a) }; return { ...mod, default: mod }; });
const winOpts = vi.fn();
const printToPDF = vi.fn();
const loadFile = vi.fn();
class FakeWindow { webContents = { printToPDF: (o: unknown) => printToPDF(o) }; loadFile = (f: string) => loadFile(f); destroy = vi.fn(); }
vi.mock("electron", () => ({ BrowserWindow: function (o: unknown) { winOpts(o); return new FakeWindow(); }, dialog: { showMessageBox: (o: unknown) => showMessageBox(o) }, shell: { showItemInFolder: (p: string) => showItemInFolder(p) } }));

import { EXPORTS, exportRobot, saveCopy, savePicture } from "./exports";

let dir = "";
beforeEach(() => { dir = mkdtempSync(join(tmpdir(), "exports-")); vi.clearAllMocks(); execFile.mockImplementation((...a: unknown[]) => (a[3] as (e: null, r: object) => void)(null, { stdout: "", stderr: "" })); });
afterEach(() => rmSync(dir, { recursive: true, force: true }));
const kind = (fmt: string) => EXPORTS.find((k) => k.fmt === fmt)!;

describe("Save a copy", () => {
  it("writes a byte-identical file for the user's own lockfile", async () => {
    const own = join(dir, "lockwire.yaml");
    const bytes = Buffer.from([0x61, 0x3a, 0x20, 0x31, 0x0d, 0x0a, 0x00, 0xff, 0x62]);
    writeFileSync(own, bytes);
    pick.mockResolvedValue(join(dir, "copy.yaml"));
    await saveCopy(own);
    expect(readFileSync(join(dir, "copy.yaml")).equals(bytes)).toBe(true);
    expect(showItemInFolder).toHaveBeenCalledWith(join(dir, "copy.yaml"));
  });

  it("writes nothing when the dialog is cancelled or names the same file", async () => {
    const own = join(dir, "lockwire.yaml");
    writeFileSync(own, "keep");
    pick.mockResolvedValueOnce(null).mockResolvedValueOnce(own);
    await saveCopy(own);
    await saveCopy(own);
    expect([readFileSync(own, "utf8"), existsSync(join(dir, "copy.yaml")), showItemInFolder.mock.calls.length]).toEqual(["keep", false, 0]);
  });
});

describe("Export", () => {
  it.each([["urdf", ["export", "/r/lockwire.yaml", "--format", "urdf", "--out", "/o/x", "--force"]], ["budget", ["export", "/r/lockwire.yaml", "--format", "budget", "--out", "/o/x", "--force"]], ["report", ["report", "/r/lockwire.yaml", "--out", "/o/x"]]])("%s runs the CLI's own command on the chosen path", async (fmt, args) => {
    pick.mockResolvedValue("/o/x");
    await exportRobot("/r/lockwire.yaml", kind(fmt));
    expect(execFile.mock.calls[0]!.slice(0, 2)).toEqual(["/venv/bin/lockwire", args]);
    expect(showItemInFolder).toHaveBeenCalledWith("/o/x");
  });

  it("proposes a file beside the lockfile named for the format", async () => {
    pick.mockResolvedValue(null);
    await exportRobot("/r/lockwire.yaml", kind("bom"));
    expect([pick.mock.calls[0]![1], pick.mock.calls[0]![3]]).toEqual(["/r/lockwire.parts.csv", "csv"]);
    expect(execFile).not.toHaveBeenCalled();
  });

  it("a refused export says why instead of reporting success", async () => {
    pick.mockResolvedValue("/o/x");
    execFile.mockImplementation((...a: unknown[]) => (a[3] as (e: object) => void)(Object.assign(new Error("exit 2"), { stdout: "export failed: the lockfile holds no model to export.\n" })));
    await exportRobot("/r/lockwire.yaml", kind("urdf"));
    expect(showMessageBox.mock.calls[0]![0].detail).toBe("export failed: the lockfile holds no model to export.");
    expect(showItemInFolder).not.toHaveBeenCalled();
  });

  it.each(["stl", "obj", "dae"])("%s asks the engine's export route for that format into the chosen folder", async (fmt) => {
    pick.mockResolvedValue("/r/out");
    runSidecar.mockResolvedValue({ refusal: null, payload: { written: ["/r/out/assembly." + fmt] } });
    await exportRobot("/r/lockwire.yaml", kind(fmt));
    expect(runSidecar).toHaveBeenCalledWith({ command: "export", args: { contract: "/r/lockwire.yaml", format: fmt, out: "/r/out", force: true } });
    expect([execFile.mock.calls.length, showItemInFolder.mock.calls[0]![0]]).toEqual([0, "/r/out"]);
  });

  it("a refused mesh export shows the engine's sentence and reveals nothing", async () => {
    pick.mockResolvedValue("/r/out");
    runSidecar.mockResolvedValue({ refusal: "refusing to write outside the robot's folder", payload: null });
    await exportRobot("/r/lockwire.yaml", kind("stl"));
    expect(showMessageBox.mock.calls[0]![0].detail).toBe("refusing to write outside the robot's folder");
    expect(showItemInFolder).not.toHaveBeenCalled();
  });

  it("a mesh folder outside the robot's folder is refused before the engine is asked", async () => {
    pick.mockResolvedValue("/elsewhere/out");
    await exportRobot("/r/lockwire.yaml", kind("stl"));
    expect(runSidecar).not.toHaveBeenCalled();
    expect(showMessageBox.mock.calls[0]![0].detail).toBe("Exports must go inside the robot's folder (/r). Pick a place there.");
  });

  it("a mesh folder with other files is refused, not offered for replacing", async () => {
    mkdirSync(join(dir, "meshes"));
    writeFileSync(join(dir, "meshes", "base.stl"), "mine");
    pick.mockResolvedValue(join(dir, "meshes"));
    await exportRobot(join(dir, "lockwire.yaml"), kind("stl"));
    expect(showMessageBox.mock.calls[0]![0].detail).toBe(`${join(dir, "meshes")} holds files that are not a Lockwire export. Pick an empty folder or a new one.`);
    expect(runSidecar).not.toHaveBeenCalled();
  });

  it("a folder holding Lockwire's own assembly asks before replacing, and Cancel exports nothing", async () => {
    writeFileSync(join(dir, "assembly.stl"), "old");
    pick.mockResolvedValue(dir);
    showMessageBox.mockResolvedValue({ response: 1 });
    await exportRobot(join(dir, "lockwire.yaml"), kind("stl"));
    expect(showMessageBox.mock.calls[0]![0].message).toBe(`Replace the Lockwire export in ${dir}?`);
    expect(runSidecar).not.toHaveBeenCalled();
  });

  it("a cancelled mesh export asks the engine for nothing", async () => {
    pick.mockResolvedValue(null);
    await exportRobot("/r/lockwire.yaml", kind("obj"));
    expect(runSidecar).not.toHaveBeenCalled();
  });
});

describe("Report (PDF)", () => {
  const pdf = kind("pdf");
  const writesHtml = () => execFile.mockImplementation((...a: unknown[]) => { writeFileSync((a[1] as string[])[3]!, "<html>report</html>"); (a[3] as (e: null, r: object) => void)(null, { stdout: "", stderr: "" }); });

  it("writes a real PDF (not the HTML) where the dialog says, defaulting to <robot>.pdf", async () => {
    writesHtml();
    printToPDF.mockResolvedValue(Buffer.from("%PDF-1.7 body"));
    pick.mockResolvedValue(join(dir, "arm.pdf"));
    await exportRobot("/r/arm.yaml", pdf);
    expect(readFileSync(join(dir, "arm.pdf")).subarray(0, 4).toString()).toBe("%PDF");
    expect([pick.mock.calls[0]![1], showItemInFolder.mock.calls.length]).toEqual(["/r/arm.pdf", 1]);
  });

  it("prints the HTML the CLI's report command wrote", async () => {
    writesHtml();
    printToPDF.mockResolvedValue(Buffer.from("%PDF"));
    pick.mockResolvedValue(join(dir, "a.pdf"));
    await exportRobot("/r/arm.yaml", pdf);
    expect([execFile.mock.calls[0]![1].slice(0, 2), loadFile.mock.calls[0]![0]]).toEqual([["report", "/r/arm.yaml"], execFile.mock.calls[0]![1][3]]);
  });

  it("prints in a hidden, sandboxed window with no node access", async () => {
    writesHtml();
    printToPDF.mockResolvedValue(Buffer.from("%PDF"));
    pick.mockResolvedValue(join(dir, "a.pdf"));
    await exportRobot("/r/arm.yaml", pdf);
    expect(winOpts).toHaveBeenCalledWith({ show: false, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  });

  it("refuses in plain words when no report is loaded, before any dialog", async () => {
    await exportRobot("", pdf);
    expect(showMessageBox.mock.calls[0]![0].detail).toBe("Open a robot first, then save its report.");
    expect(pick).not.toHaveBeenCalled();
  });

  it("writes nothing when cancelled", async () => {
    pick.mockResolvedValue(null);
    await exportRobot("/r/arm.yaml", pdf);
    expect([execFile.mock.calls.length, printToPDF.mock.calls.length]).toEqual([0, 0]);
  });
});

describe("Save picture", () => {
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

  it("writes the PNG bytes where the dialog says, defaulting to <robot>.png", async () => {
    pick.mockResolvedValue(join(dir, "arm.png"));
    expect(await savePicture("Arm", png)).toBe(true);
    expect(readFileSync(join(dir, "arm.png")).equals(png)).toBe(true);
    expect(pick.mock.calls[0]![1]).toBe("Arm.png");
  });

  it("writes nothing when cancelled", async () => {
    pick.mockResolvedValue(null);
    expect(await savePicture("Arm", png)).toBe(false);
    expect([existsSync(join(dir, "arm.png")), showItemInFolder.mock.calls.length]).toEqual([false, 0]);
  });

  it("refuses bytes that are not a PNG, before any dialog", async () => {
    await expect(savePicture("Arm", Buffer.from([0xff, 0xd8, 0xff, 0xe0]))).rejects.toThrow("could not be made");
    expect(pick).not.toHaveBeenCalled();
  });
});
