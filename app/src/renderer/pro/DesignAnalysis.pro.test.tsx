import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AnalysisJobStatePayload } from "../../shared/pro/types";
import { resetDesignView } from "../scene/designView";

vi.mock("../scene/Canvas", () => ({ Canvas: () => null, useScene: () => ({}) }));
const { DesignPanel, DesignTools } = await import("../DesignPanel");

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  resetDesignView();
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const Page = ({ contract, expectedRevision }: { contract?: string; expectedRevision?: string }) => (
  <>
    <DesignTools payload={null} canAnalyse={!!contract && !!expectedRevision} />
    <DesignPanel payload={null} contract={contract} expectedRevision={expectedRevision} />
  </>
);

describe("structural analysis on the Design page (pro)", () => {
  it("keeps the Analysis tool disabled without a loaded revision", () => {
    act(() => root.render(<Page />));
    expect(container.querySelector<HTMLButtonElement>('button[title="Analysis needs a scene"]')).not.toBeNull();
  });

  it("starts the revision-bound analysis and renders its two independent scores", async () => {
    const completed = {
      status: "completed",
      exit_code: 0,
      rows_written: 0,
      output: "",
      errors: "",
      error: null,
      document: {
        document_version: "1",
        kind: "analysis_result",
        status: "COMPLETED",
        solver_started: true,
        contract_revision: "a".repeat(64),
        procedure_id: "static",
        procedure_kind: "structural",
        runtime_profile: "calculix-linear-static",
        package_manifest_sha256: "b".repeat(64),
        solver: { backend: "calculix", version: "2.23" },
        deck_template: "direct-b31-square-v1",
        artifacts: [],
        physical_conformance: "NOT_SCORED",
        simulation_validity: "GO",
        output_sha256: "c".repeat(64),
        results: [{ kind: "U", node_set: "TIP", node: 5, values: [0, -0.19, 0], unit: "m" }],
        comparisons: [{ metric: "b31_tip_uy", passed: true }],
        reason: "analytic comparison passed; physical conformance remains unscored",
      },
    } as unknown as AnalysisJobStatePayload;
    const startAnalysis = vi.fn(async () => completed);
    Object.defineProperty(window, "lockwire", {
      configurable: true,
      value: { startAnalysis, pollAnalysis: vi.fn(), cancelAnalysis: vi.fn() },
    });
    act(() => root.render(<Page contract="robot.yaml" expectedRevision={"a".repeat(64)} />));
    act(() => container.querySelector<HTMLButtonElement>('button[title="Analysis"]')!.click());

    const setInput = (label: string, value: string) => {
      const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, value);
      act(() => input.dispatchEvent(new Event("input", { bubbles: true })));
    };
    setInput("Analysis procedure ID", "static");
    setInput("Analysis evidence root", "/evidence");
    setInput("Analysis package manifest", "/package.json");
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".analysis-start")!.click();
    });

    expect(startAnalysis).toHaveBeenCalledWith({
      contract: "robot.yaml",
      expected_revision: "a".repeat(64),
      procedure_id: "static",
      evidence_root: "/evidence",
      package_manifest: "/package.json",
    });
    expect(container.textContent).toContain("Physical conformanceNOT_SCORED");
    expect(container.textContent).toContain("Simulation validityGO");
    expect(container.textContent).toContain("Output SHA-256");
    expect(container.querySelector(".analysis-status")?.textContent).toContain("completed");
    expect(container.textContent).not.toContain("raw CCX output");
    Reflect.deleteProperty(window, "lockwire");
  });
});
