import { afterEach, describe, expect, it, vi } from "vitest";

const { callSidecar } = vi.hoisted(() => ({ callSidecar: vi.fn() }));
vi.mock("./sidecar", () => ({ callSidecar }));

import { limitFor, runSidecarCommand } from "./ipcKit";

const reply = { version: "v", cli_line: "l", exit_code: 0, verdict: null, payload: {}, refusal: null };
afterEach(() => { vi.useRealTimers(); callSidecar.mockReset(); });

describe("runSidecarCommand limit", () => {
  it("rejects with a plain message when the engine never answers, and a normal call is unaffected", async () => {
    vi.useFakeTimers();
    callSidecar.mockResolvedValueOnce({ json: () => Promise.resolve(reply) });
    await expect(runSidecarCommand({ command: "undo-edit" })).resolves.toEqual(reply);
    callSidecar.mockReturnValueOnce(new Promise(() => {}));
    const stuck = runSidecarCommand({ command: "undo-edit" });
    const caught = expect(stuck).rejects.toThrow("Lockwire's engine did not answer in 120 s. Try again.");
    await vi.advanceTimersByTimeAsync(120_000);
    await caught;
  });

  it("every command that loads or builds a full model gets the five-minute tier", () => {
    for (const command of ["start", "start-from-urdf", "wiring-erc", "wiring-plan", "wiring-auto", "assembly-steps"]) expect(limitFor({ command })).toBe(300_000);
  });

  it("a timed-out call rejects, so the caller's busy state can clear", async () => {
    vi.useFakeTimers();
    callSidecar.mockReturnValueOnce(new Promise(() => {}));
    let busy = true;
    const done = runSidecarCommand({ command: "check" }).catch((e: Error) => e.message).finally(() => { busy = false; });
    await vi.advanceTimersByTimeAsync(300_000);
    expect(await done).toMatch(/did not answer in 300 s/);
    expect(busy).toBe(false);
  });

  it("analysis jobs have no limit, model-loading commands five minutes, the rest two", () => {
    expect([limitFor({ command: "preflight" }), limitFor({ command: "check" }), limitFor({ command: "undo-edit" })]).toEqual([undefined, 300_000, 120_000]);
  });
});
