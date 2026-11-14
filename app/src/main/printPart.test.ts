import { beforeEach, describe, expect, it, vi } from "vitest";

const shell = vi.hoisted(() => ({ openPath: vi.fn(), showItemInFolder: vi.fn() }));
vi.mock("electron", () => ({ shell }));
const run = vi.hoisted(() => vi.fn());
vi.mock("./ipcKit", () => ({ runSidecarCommand: (r: unknown) => run(r) }));

import { printPart } from "./printPart";

const ok = { refusal: null, payload: { written: ["/o/lockwire-stl/arm.stl", "/o/lockwire-stl/hand.stl", "/o/lockwire-stl/assembly.stl"] } };

beforeEach(() => {
  vi.resetAllMocks();
  run.mockResolvedValue(ok);
  shell.openPath.mockResolvedValue("");
});

describe("print this part", () => {
  it("opens exactly the selected link's STL, asking for millimetres", async () => {
    await printPart("/o/lockwire.yaml", "hand");
    expect(run).toHaveBeenCalledWith({ command: "export", args: expect.objectContaining({ format: "stl", units: "mm" }) });
    expect(shell.openPath).toHaveBeenCalledOnce();
    expect(shell.openPath).toHaveBeenCalledWith("/o/lockwire-stl/hand.stl");
  });

  it("shows the file in its folder, and says so, when no app opens .stl", async () => {
    shell.openPath.mockResolvedValue("No application knows how to open it");
    const line = await printPart("/o/lockwire.yaml", "arm");
    expect(shell.showItemInFolder).toHaveBeenCalledWith("/o/lockwire-stl/arm.stl");
    expect(line).toMatch(/No app is set up/);
  });

  it("returns an export refusal as its sentence and opens nothing", async () => {
    run.mockResolvedValue({ refusal: "/o holds the source model; export into a different folder", payload: null });
    expect(await printPart("/o/lockwire.yaml", "arm")).toBe("/o holds the source model; export into a different folder");
    expect(shell.openPath).not.toHaveBeenCalled();
    expect(shell.showItemInFolder).not.toHaveBeenCalled();
  });
});
