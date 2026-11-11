import { expect, it, vi } from "vitest";

const callSidecar = vi.fn();
vi.mock("electron", () => ({ app: { isPackaged: false }, dialog: {}, ipcMain: {} }));
vi.mock("./sidecar", () => ({ callSidecar: (...a: unknown[]) => callSidecar(...a) }));
const bad = () => { throw new Error("contract:page: invalid page payload"); };
vi.mock("./proRegistry", () => ({ proMain: { classes: [], handlers: {}, validators: { page: bad } } }));
process.env.ELECTRON_RENDERER_URL = "http://localhost:5173/";
const { dispatch } = await import("./ipc");

it("a Pro app on the free engine says so, not 'Reinstall'", async () => {
  const j = (v: unknown) => ({ json: () => Promise.resolve(v) });
  callSidecar.mockResolvedValueOnce(j({ version: "1", cli_line: "l", exit_code: 0, verdict: null, payload: { x: 1 }, refusal: null }));
  callSidecar.mockResolvedValueOnce(j({ commands: [{ name: "page" }] }));
  const event = { senderFrame: { url: "http://localhost:5173/" } } as never;
  await expect(dispatch(event, "contract:page", { contract: "c" })).rejects.toThrow("expects the paid engine");
});
