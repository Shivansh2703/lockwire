import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "assistant-"));
vi.mock("electron", () => ({
  app: { getPath: () => dir },
  BrowserWindow: { getAllWindows: () => [] },
  safeStorage: {
    isEncryptionAvailable: () => true,
    encryptString: (s: string) => Buffer.from(`enc:${s}`),
    decryptString: (b: Buffer) => b.toString().slice(4),
  },
}));
const callSidecar = vi.fn();
vi.mock("./sidecar", () => ({ callSidecar: (...a: unknown[]) => callSidecar(...a) }));

const { handleApply, handleSend, handleSettingsGet, handleSettingsSet } = await import("./assistant");

const C = "/tmp/robot/lockwire.yaml";
const KEY = "sk-ant-secret";
const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
const ran = () => callSidecar.mock.calls.filter(([path]) => path === "/v1/run");
const fetchMock = vi.fn();
let extraTools: unknown[] = [];
const tools = (i: number) => JSON.parse(fetchMock.mock.calls[i]![1].body).tools as { name: string; input_schema: { properties: Record<string, unknown>; required?: string[] } }[];
const modelSays = (content: unknown[], stop_reason = "end_turn") => fetchMock.mockResolvedValueOnce(json({ content, stop_reason }));
const propose = () =>
  modelSays([{ type: "tool_use", id: "t1", name: "move", input: { target: "arm", offset: [0, 0, 1], unit: "m", source: "me" } }], "tool_use");

beforeEach(async () => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  callSidecar.mockReset();
  extraTools = [];
  callSidecar.mockImplementation(async (path: string, init?: { body?: string }) => {
    if (path === "/v1/hello") {
      return json({ commands: [{ name: "move", args_schema: { properties: { contract: {}, witness: {}, expected_revision: {}, dry_run: {}, target: {} }, required: ["contract", "witness", "target"] } }, ...extraTools] });
    }
    const a = JSON.parse(init!.body!).args;
    // mirrors the real sidecar: witness is required, even on a dry run
    if (typeof a.witness !== "string" || !a.witness) return json({ version: "1", cli_line: "lockwire move", exit_code: 2, verdict: null, payload: null, refusal: "REFUSED: witness Field required" });
    const refusal = a.expected_revision === "old" && !a.dry_run ? "revision changed; reload first" : a.target === "nope" ? "no such part: nope" : null;
    return json({ version: "1", cli_line: "lockwire move", exit_code: refusal ? 2 : 0, verdict: null, payload: refusal ? null : a.dry_run ? { diff: "-z: 0\n+z: 1\n" } : {}, refusal });
  });
  rmSync(join(dir, "assistant.json"), { force: true });
});

const send = (revision = "r1") => handleSend({ contract: C, revision, mode: "plan", text: "hi" });
const enable = () => handleSettingsSet({ key: KEY, model: "m-1", enableRobot: C });

describe("the Assistant", () => {
  it("a proposal carries the sidecar's own dry-run diff and writes nothing", async () => {
    await enable();
    propose();
    modelSays([{ type: "text", text: "proposed" }]);
    const turn = await send();
    expect(turn.proposals).toMatchObject([{ command: "move", revision: "r1", diff: "-z: 0\n+z: 1\n" }]);
    expect(ran()).toHaveLength(1);
    expect(JSON.parse(ran()[0]![1].body).args).toMatchObject({ dry_run: true, contract: C, expected_revision: "r1" });
    expect(JSON.parse(ran()[0]![1].body).args.witness).toBe("your name");
    const tool = JSON.parse(fetchMock.mock.calls[1]![1].body).tools[0];
    expect(Object.keys(tool.input_schema.properties)).toEqual(["target"]);
    expect(fetchMock.mock.calls[1]![1].body).toContain("<<<UNTRUSTED_CONTRACT_TEXT-");
  });

  it("the dry run's witness is main's, never the model's", async () => {
    await enable();
    modelSays([{ type: "tool_use", id: "t1", name: "move", input: { target: "arm", witness: "Mallory" } }], "tool_use");
    modelSays([{ type: "text", text: "ok" }]);
    const turn = await send();
    expect(turn.proposals).toHaveLength(1);
    expect(JSON.parse(ran()[0]![1].body).args.witness).toBe("your name");
  });

  it("apply runs one command with the user's witness and the held revision", async () => {
    await enable();
    propose();
    modelSays([{ type: "text", text: "ok" }]);
    const { proposals } = await send();
    expect(await handleApply({ id: proposals[0]!.id, witness: "Ada" })).toEqual({ ok: true });
    expect(ran()).toHaveLength(2); // the dry run, then the signed write
    expect(JSON.parse(ran()[1]![1].body).args).toMatchObject({ contract: C, witness: "Ada", expected_revision: "r1" });
  });

  it("shows a stale-revision refusal as the card error", async () => {
    await enable();
    propose();
    modelSays([{ type: "text", text: "ok" }]);
    const { proposals } = await send("old");
    expect(await handleApply({ id: proposals[0]!.id, witness: "Ada" })).toEqual({ ok: false, error: "revision changed; reload first" });
  });

  it("never fetches for a robot that is not enabled, or was turned off", async () => {
    await handleSettingsSet({ key: KEY });
    expect((await send()).error).toBeTruthy();
    await enable();
    await handleSettingsSet({ disableRobot: C });
    expect((await send()).error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never returns the key from settings-get", async () => {
    await enable();
    const got = await handleSettingsGet();
    expect(got).toEqual({ hasKey: true, model: "m-1", enabledRobots: [C] });
    expect(JSON.stringify(got)).not.toContain(KEY);
  });

  const readTools = [
    { name: "report", args_schema: { properties: { contract: {}, scene: {}, out: {}, json_out: {}, cache_dir: {}, x_path: {} }, required: ["contract", "out"] } },
    { name: "describe", args_schema: { properties: { contract: {}, out: {} } } },
    { name: "what-if", args_schema: { properties: { changes: { type: "array", items: { properties: { witness: {}, target: {} }, required: ["witness", "target"] } } } } },
  ];

  it("read tools never show the model a path arg, nor a nested main-owned one", async () => {
    extraTools = readTools;
    await enable();
    modelSays([{ type: "text", text: "hi" }]);
    await send();
    const byName = Object.fromEntries(tools(0).map((t) => [t.name, t.input_schema]));
    expect(Object.keys(byName.report!.properties)).toEqual(["scene"]);
    expect(byName.report!.required).toEqual([]);
    expect(Object.keys(byName.describe!.properties)).toEqual([]);
    expect(JSON.stringify(byName["what-if"])).not.toContain("witness");
  });

  it("a model-chosen out is replaced by a main temp path; report writes only under it", async () => {
    extraTools = readTools;
    await enable();
    modelSays([{ type: "tool_use", id: "t1", name: "report", input: { out: "/Users/victim/.zshrc", json_out: "/x" } }], "tool_use");
    modelSays([{ type: "text", text: "done" }]);
    await send();
    const args = JSON.parse(ran()[0]![1].body).args;
    expect(args.out.startsWith(dir)).toBe(true);
    expect(args.out).not.toContain("victim");
    expect(args.json_out).toBeUndefined();
  });

  it("past the tool cap the loop stops and makes no further API post", async () => {
    await enable();
    for (let i = 0; i < 30; i++) propose();
    const turn = await send();
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(13);
    expect(turn.text).toMatch(/limit/i);
  });

  it("no default model; Send fails when none is set", async () => {
    await handleSettingsSet({ key: KEY, enableRobot: C });
    expect((await handleSettingsGet()).model).toBe("");
    expect((await send()).error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("the API call has a deadline and a timeout is a plain error", async () => {
    await enable();
    vi.spyOn(AbortSignal, "timeout").mockReturnValue(AbortSignal.abort(new DOMException("t", "TimeoutError")));
    fetchMock.mockImplementationOnce(async (_u: string, init: { signal: AbortSignal }) => { throw init.signal.reason; });
    expect((await send()).error).toBe("The Assistant took too long to answer: try again");
    vi.restoreAllMocks();
  });

  it("a max_tokens stop with a tool_use leaves a tool_result so the next turn is valid", async () => {
    await enable();
    modelSays([{ type: "tool_use", id: "cut", name: "move", input: {} }], "max_tokens");
    await send();
    modelSays([{ type: "text", text: "ok" }]);
    await send();
    const sent = JSON.parse(fetchMock.mock.calls[1]![1].body).messages;
    expect(JSON.stringify(sent)).toContain('"tool_use_id":"cut"');
  });

  it("a model dry_run never reaches apply", async () => {
    await enable();
    modelSays([{ type: "tool_use", id: "t1", name: "move", input: { target: "arm", dry_run: true } }], "tool_use");
    modelSays([{ type: "text", text: "ok" }]);
    const { proposals } = await send();
    await handleApply({ id: proposals[0]!.id, witness: "Ada" });
    expect(JSON.parse(ran()[1]![1].body).args.dry_run).toBeUndefined();
  });

  it("a proposal whose dry run is refused is no card; the model is told why", async () => {
    await enable();
    modelSays([{ type: "tool_use", id: "t1", name: "move", input: { target: "nope" } }], "tool_use");
    modelSays([{ type: "text", text: "sorry" }]);
    const turn = await send();
    expect(turn.proposals).toEqual([]);
    expect(fetchMock.mock.calls[1]![1].body).toContain("no such part: nope");
  });
});
