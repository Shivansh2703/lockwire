import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { HookInput } from "@anthropic-ai/claude-agent-sdk";
import { describe, expect, it, vi } from "vitest";

import { PRODUCT_NAME } from "../shared/branding";

const dir = mkdtempSync(join(tmpdir(), "agent-"));
vi.mock("electron", () => ({ app: { getPath: () => dir, isPackaged: false }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
const query = vi.fn();
vi.mock("@anthropic-ai/claude-agent-sdk", () => ({
  query: (...a: unknown[]) => query(...a),
  createSdkMcpServer: (o: object) => ({ type: "sdk", instance: o }),
  tool: (name: string, _d: string, shape: object, handler: unknown) => ({ name, shape, handler }),
}));
// The real Assistant proposal store; only the sidecar is faked (a declare dry run and its what-if compare).
const json = (body: unknown) => ({ json: async () => body });
const callSidecar = vi.fn(async (path: string, init?: { body?: string }) => {
  if (path === "/v1/hello") return json({ commands: [{ name: "declare", args_schema: { type: "object", properties: { contract: {}, witness: {}, path: { type: "string" }, value: {}, source: { type: "string" } }, required: ["contract", "witness", "path", "value", "source"] } }] });
  const { command, args } = JSON.parse(init!.body!);
  const payload = command === "what-if" ? { current: { verdict: "PASS" }, proposed: { verdict: "FAIL" } } : args.dry_run ? { diff: "-mass: 1\n+mass: 2\n" } : {};
  return json({ version: "1", cli_line: "lockwire", exit_code: 0, verdict: null, payload, refusal: null });
});
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), callSidecar: (...a: [string, { body?: string }]) => callSidecar(...a) }));

const { DENY_REASON, PROPOSE_TOOLS, guard, startAgentSession, toEvents, usageLine } = await import("./agent");
const { handleApply, PROPOSAL_RECORDED } = await import("./assistant");
const result = { type: "result", subtype: "success", session_id: "s-1", total_cost_usd: 0.0412, modelUsage: { m: { inputTokens: 11_000, outputTokens: 600, cacheReadInputTokens: 400, cacheCreationInputTokens: 0 } } };
const run = (contract: string, onEvent: (e: unknown) => void = () => {}) =>
  startAgentSession({ prompt: "hi", contract, revision: "r1", apiKey: "sk-ant-test", onEvent, signal: new AbortController().signal });

const ROBOT = "/robots/arm";
const pre = (tool_name: string, tool_input: unknown = {}) =>
  ({ hook_event_name: "PreToolUse", tool_name, tool_input, tool_use_id: "t", session_id: "s", transcript_path: "", cwd: ROBOT }) as HookInput;
const decision = async (name: string, input?: unknown) =>
  ((await guard(ROBOT)(pre(name, input), "t", { signal: new AbortController().signal })) as { hookSpecificOutput?: { permissionDecision: string; permissionDecisionReason: string } }).hookSpecificOutput;

describe("the Agent session", () => {
  it("passes read-only, isolated options to the SDK and streams text and tools", async () => {
    query.mockImplementation(async function* () {
      yield { type: "system", subtype: "init", tools: ["Read", "mcp__lockwire__check_contract"] };
      yield { type: "assistant", message: { content: [{ type: "text", text: "Checking" }, { type: "tool_use", name: "mcp__lockwire__check_contract" }] } };
      yield result;
    });
    const events: unknown[] = [];
    await run(`${ROBOT}/lockwire.yaml`, (e) => events.push(e));
    const o = query.mock.calls[0]![0].options;
    for (const t of ["Edit", "Write", "Bash"]) {
      expect(o.disallowedTools).toContain(t);
      expect(o.tools).not.toContain(t);
      expect(o.allowedTools).not.toContain(t);
    }
    expect(o).toMatchObject({ cwd: ROBOT, settingSources: [], strictMcpConfig: true, permissionMode: "dontAsk", maxTurns: 12 });
    expect(o.allowedTools).toContain("mcp__propose__declare");
    expect(o.resume).toBeUndefined();
    expect(o.disallowedTools).toContain("mcp__lockwire__review_value");
    expect(o.hooks.PreToolUse[0].onFailure).toBe("block");
    expect(o.env).toMatchObject({ ANTHROPIC_API_KEY: "sk-ant-test", HOME: join(dir, "agent-home"), CLAUDE_CONFIG_DIR: join(dir, "agent-home"), DISABLE_TELEMETRY: "1" });
    expect(o.mcpServers.lockwire.args[0]).toBe("-i");
    expect(o.mcpServers.lockwire.args).toContain(`HOME=${join(dir, "agent-home")}`);
    expect(o.mcpServers.lockwire.args.join(" ")).not.toContain("ANTHROPIC_API_KEY");
    expect(events).toEqual([
      { type: "tools", tools: ["Read", "mcp__lockwire__check_contract"] },
      { type: "text", text: "Checking" },
      { type: "tool", name: "mcp__lockwire__check_contract" },
      { type: "done", usage: "about 12k tokens, ~$0.04 this session" },
    ]);
    expect(usageLine({ total_cost_usd: 0.004, modelUsage: { m: { ...result.modelUsage.m, inputTokens: 300 } } } as never)).toBe("about 1k tokens, under a cent this session");
    expect(usageLine({ total_cost_usd: 0, modelUsage: { m: { ...result.modelUsage.m, inputTokens: 0, cacheReadInputTokens: 0 } } } as never)).toBe("about 600 tokens, under a cent this session");
    expect(toEvents({ ...result, subtype: "error_max_turns" } as never)).toMatchObject([{ error: "I stopped: this needed more than 12 steps. Ask again more narrowly." }]);
    expect(toEvents({ type: "result", subtype: "success", is_error: true, result: "Invalid API key" } as never)).toEqual([{ type: "done", error: "Invalid API key" }]);
  });

  it("a propose tool returns a card from the Assistant's store and writes nothing; the next message resumes", async () => {
    const robot = mkdtempSync(join(tmpdir(), "robot-"));
    writeFileSync(join(robot, "lockwire.yaml"), "mass: 1\n");
    const hash = () => createHash("sha256").update(readdirSync(robot).map((f) => f + readFileSync(join(robot, f), "utf8")).join()).digest("hex");
    const before = hash();
    let reply: unknown;
    query.mockImplementation(async function* ({ options }: { options: { mcpServers: { propose: { instance: { tools: { name: string; handler: (a: unknown) => Promise<unknown> }[] } } } } }) {
      reply = await options.mcpServers.propose.instance.tools.find((t) => t.name === "declare")!.handler({ path: "mass", value: 2, source: "datasheet" });
      yield result;
    });
    const events: { type: string; proposal?: { id: string } }[] = [];
    await run(join(robot, "lockwire.yaml"), (e) => events.push(e as never));
    expect(reply).toEqual({ content: [{ type: "text", text: PROPOSAL_RECORDED }] });
    expect(events[0]).toMatchObject({ type: "proposal", proposal: { command: "declare", revision: "r1", diff: "-mass: 1\n+mass: 2\n" }, check: `${PRODUCT_NAME} check: PASS now, FAIL with this change.` });
    const runs = callSidecar.mock.calls.filter(([p]) => p === "/v1/run").map(([, i]) => JSON.parse(i!.body!));
    expect(runs.map((r) => [r.command, r.args.dry_run])).toEqual([["declare", true], ["what-if", undefined]]);
    expect(hash()).toBe(before);
    expect(await decision(PROPOSE_TOOLS[0]!)).toBeUndefined();
    // the card's id is the Assistant's own held proposal: its signing door takes it with the user's name
    expect(await handleApply({ id: events[0]!.proposal!.id, witness: "Ada" })).toEqual({ ok: true });
    await run(join(robot, "lockwire.yaml"));
    expect(query.mock.calls.at(-1)![0].options.resume).toBe("s-1");
  });

  it("denies writes, mutating lockwire tools and reads outside the robot folder", async () => {
    for (const name of ["Write", "Edit", "Bash", "mcp__lockwire__review_value", "mcp__lockwire__upgrade_contract", "mcp__lockwire__some_paid_tool"]) {
      expect(await decision(name, { file_path: `${ROBOT}/lockwire.yaml` })).toEqual({ hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: DENY_REASON });
    }
    expect((await decision("Read", { file_path: "/Users/me/.ssh/id_rsa" }))?.permissionDecision).toBe("deny");
    expect((await decision("Grep", { path: "../.." }))?.permissionDecision).toBe("deny");
    expect(await decision("Read", { file_path: `${ROBOT}/urdf/arm.urdf` })).toBeUndefined();
    expect(await decision("Glob", { pattern: "**/*.urdf" })).toBeUndefined();
    expect(await decision("mcp__lockwire__check_contract", { contract: `${ROBOT}/lockwire.yaml` })).toBeUndefined();
    for (const [name, input] of [["Glob", { pattern: "/etc/*" }], ["Glob", { pattern: "../*" }], ["Grep", { glob: "../../**" }], ["Glob", { pattern: "/Users/me/.ssh" }]] as const) {
      expect((await decision(name, input))?.permissionDecision).toBe("deny");
    }
    expect(await decision("Glob", { pattern: "meshes/*.stl" })).toBeUndefined();
    expect(await decision("Read", { file_path: `${ROBOT}/..foo/arm.urdf` })).toBeUndefined();
    for (const [name, input] of [["Read", { file_path: "~/.ssh/id_rsa" }], ["Glob", { pattern: "~/*" }], ["Grep", { pattern: "x", path: "/etc" }]] as const) expect((await decision(name, input))?.permissionDecision).toBe("deny");
    for (const input of [{ pattern: "/usr/bin" }, { pattern: "a/.." }]) expect(await decision("Grep", input)).toBeUndefined();
    expect(await decision("Read", { file_path: "meshes/a~b.stl" })).toBeUndefined();
    expect((await guard(ROBOT)(null as never, "t", { signal: new AbortController().signal }) as { hookSpecificOutput: { permissionDecisionReason: string } }).hookSpecificOutput.permissionDecisionReason).not.toBe(DENY_REASON);
    expect((await guard(ROBOT)(null as never, "t", { signal: new AbortController().signal }) as { hookSpecificOutput: { permissionDecision: string } }).hookSpecificOutput.permissionDecision).toBe("deny");
  });

  it("refuses a symlink in the robot folder that points outside it", async () => {
    const robot = join(dir, "robot");
    mkdirSync(join(robot, "urdf"), { recursive: true });
    symlinkSync(tmpdir(), join(robot, "out"));
    const ask = async (name: string, input: unknown) =>
      ((await guard(robot)(pre(name, input), "t", { signal: new AbortController().signal })) as { hookSpecificOutput?: { permissionDecision: string } }).hookSpecificOutput;
    expect((await ask("Read", { file_path: join(robot, "out", "secret") }))?.permissionDecision).toBe("deny");
    expect((await ask("Glob", { pattern: "out/*" }))?.permissionDecision).toBe("deny");
    expect(await ask("Read", { file_path: join(robot, "urdf", "arm.urdf") })).toBeUndefined();
  });

  it("drops a stale resume id and retries once; no retry without one; a down engine gets a plain message", async () => {
    const bad = { type: "result", subtype: "success", is_error: true, result: "no such session", session_id: "s-bad" };
    const c = "/r/stale/lockwire.yaml", events: unknown[] = [];
    query.mockReset().mockImplementationOnce(async function* () { yield result; }).mockImplementationOnce(async function* () { yield bad; }).mockImplementationOnce(async function* () { yield result; });
    await run(c); await run(c, (e) => events.push(e));
    const o = query.mock.calls.map((x) => x[0].options.resume);
    expect(o).toEqual([undefined, "s-1", undefined]);
    expect(events).toEqual([{ type: "done", usage: "about 12k tokens, ~$0.04 this session" }]);
    query.mockReset().mockImplementation(async function* () { yield { ...bad, session_id: undefined }; });
    const fresh: unknown[] = [];
    await run("/r/fresh/lockwire.yaml", (e) => fresh.push(e));
    expect(query).toHaveBeenCalledTimes(1);
    expect(fresh).toEqual([{ type: "done", error: "no such session" }]);
    callSidecar.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    const down: unknown[] = [];
    await run("/r/down/lockwire.yaml", (e) => down.push(e));
    expect(down).toEqual([{ type: "done", error: expect.stringContaining("engine is not responding") }]);
  });
});
