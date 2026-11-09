import { beforeEach, describe, expect, it, vi } from "vitest";

const exposed: Record<string, unknown> = {};
const invokeCalls: unknown[][] = [];

vi.mock("electron", () => ({
  contextBridge: {
    exposeInMainWorld: (name: string, api: Record<string, unknown>) => {
      exposed[name] = api;
    },
  },
  webUtils: { getPathForFile: () => "/x" },
  ipcRenderer: {
    on: () => undefined,
    removeListener: () => undefined,
    invoke: (...args: unknown[]) => {
      invokeCalls.push(args);
      return Promise.resolve({ accepted: true });
    },
  },
}));

beforeEach(() => {
  invokeCalls.length = 0;
});

describe("the paid preload methods (pro/)", () => {
  it("are exactly the nine named methods, merged into the one bridge object", async () => {
    await import("../index");
    const names = Object.keys(exposed.lockwire as object);
    const pro = [
      "cancelAnalysis", "cancelRun", "checkRecording", "chooseRecording", "pollAnalysis",
      "pollRun", "prepareActuatorReviewDraft", "startAnalysis", "startRun",
    ];
    for (const name of pro) expect(names).toContain(name);
    // 37 free methods (the free bridge is asserted exactly in ../index.test.ts)
    expect(names).toHaveLength(37 + pro.length);
  });

  it("each forwards to the single RUN_CHANNEL with its own hardcoded command class", async () => {
    await import("../index");
    const bridge = exposed.lockwire as Record<string, (a?: unknown) => Promise<unknown>>;
    const analysisRequest = {
      contract: "c",
      expected_revision: "a".repeat(64),
      procedure_id: "static",
      evidence_root: "/evidence",
      package_manifest: "/package.json",
    };
    await bridge.prepareActuatorReviewDraft!({ contract: "c", case_id: "elbow.nominal" });
    await bridge.checkRecording!({ contract: "c", recording: "r" });
    await bridge.startRun!({ contract: "c", recording: "r" });
    await bridge.pollRun!();
    await bridge.cancelRun!();
    await bridge.startAnalysis!(analysisRequest);
    await bridge.pollAnalysis!();
    await bridge.cancelAnalysis!();
    await bridge.chooseRecording!();
    expect(invokeCalls).toEqual([
      ["lockwire:run", "contract:actuator-review-draft", { contract: "c", case_id: "elbow.nominal" }],
      ["lockwire:run", "contract:preflight", { contract: "c", recording: "r" }],
      ["lockwire:run", "run:start", { contract: "c", recording: "r" }],
      ["lockwire:run", "run:poll", undefined],
      ["lockwire:run", "run:cancel", undefined],
      ["lockwire:run", "analysis:start", analysisRequest],
      ["lockwire:run", "analysis:poll", undefined],
      ["lockwire:run", "analysis:cancel", undefined],
      ["lockwire:run", "file:choose-recording", undefined],
    ]);
  });
});
