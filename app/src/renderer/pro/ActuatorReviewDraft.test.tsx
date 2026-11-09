import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CheckPayload } from "../../shared/api.gen";
import type { ActuatorCase } from "../../shared/pro/types";
import { ActuatorReviewDraft } from "./ActuatorReviewDraft";

const actuatorCase: ActuatorCase = {
  case_id: "elbow.nominal",
  evidence: null,
  conformance: { label: "physical conformance", verdict: "NOT_SCORED", exit_code: 3, scored: null, gaps: [],
    context: { case_id: "elbow.nominal", claim_id: "conformance", contract_revision: null,
      selected_evidence: null, evidence_class: "unverified", source_witnesses: [],
      uncertainty: "unknown", dependencies: [], freshness: "unverified",
      validation_reference: null, reference_held_out: null, calibration_artifacts: [], draft_binding: null } },
  simulation_validity: { label: "simulation validity", verdict: "NOT_SCORED", exit_code: 3, scored: null, gaps: [],
    context: { case_id: "elbow.nominal", claim_id: "simulation_validity", contract_revision: null,
      selected_evidence: null, evidence_class: "unverified", source_witnesses: [],
      uncertainty: "unknown", dependencies: [], freshness: "unverified",
      validation_reference: null, reference_held_out: null, calibration_artifacts: [], draft_binding: null } },
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("ActuatorReviewDraft", () => {
  it("requires explicit case and evidence identity and exposes no apply action", async () => {
    const onPrepare = vi.fn();
    root = createRoot(container);
    act(() => {
      root.render(
        <ActuatorReviewDraft
          contract="/tmp/arm/lockwire.yaml"
          expectedRevision={"a".repeat(64)}
          cases={[actuatorCase]}
          onPrepare={onPrepare}
        />,
      );
    });
    const select = container.querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("");
    expect(container.textContent).not.toMatch(/apply/i);
    await act(async () => {
      container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(onPrepare).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Select a declared case");
  });

  it("keeps the engine draft visible when a later shared-route refusal occurs", async () => {
    const prepared: { document: CheckPayload; cliLine: string } = {
      document: {
        actuator_review_draft: {
          base_raw_revision: "b".repeat(64),
          discrepancy: "tracking differs",
          evidence: [{ artifact: { path: "captures/elbow.json", sha256: "a".repeat(64) },
            provenance: "held-out bench", result_id: "elbow.nominal", claim_id: "simulation_validity" }],
          alternatives: ["repeat capture"],
          proposed_patch: { kind: "physical", patch: "inspect linkage" },
          rationale: "hash-bound capture", correction_category: "physical",
          affected_results: [{ result_id: "elbow.nominal", claim_ids: ["conformance"] }],
          required_recheck: ["rerun elbow.nominal"],
        },
        actuator_review_next_step: "Engineer review required; this draft has not been applied.",
      } as unknown as CheckPayload,
      cliLine: "lockwire actuator-review-draft /tmp/arm/lockwire.yaml elbow.nominal",
    };
    const onPrepare = vi.fn()
      .mockResolvedValueOnce(prepared)
      .mockRejectedValueOnce(new Error("base_raw_revision is stale"));
    root = createRoot(container);
    act(() => {
      root.render(
        <ActuatorReviewDraft
          contract="/tmp/arm/lockwire.yaml"
          expectedRevision={"a".repeat(64)}
          cases={[actuatorCase]}
          onPrepare={onPrepare}
        />,
      );
    });
    const selects = container.querySelectorAll("select");
    const inputs = container.querySelectorAll("input");
    const textareas = container.querySelectorAll("textarea");
    function setValue(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) {
      const prototype = element instanceof HTMLInputElement
        ? HTMLInputElement.prototype
        : element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLTextAreaElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }
    await act(async () => {
      setValue(selects[0]!, "elbow.nominal");
      setValue(inputs[0]!, "/tmp/arm/evidence");
      setValue(inputs[1]!, "runs/elbow.mcap");
      setValue(inputs[2]!, "c".repeat(64));
      setValue(textareas[0]!, "bench capture");
      setValue(textareas[1]!, "tracking differs");
      setValue(textareas[2]!, "repeat capture");
      setValue(selects[1]!, "physical");
      setValue(textareas[3]!, "inspect linkage");
      setValue(textareas[4]!, "hash-bound capture");
      setValue(selects[2]!, "physical");
      setValue(textareas[5]!, "rerun elbow.nominal");
    });
    const form = container.querySelector("form")!;
    await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(container.textContent).toContain("Draft base raw revision: " + "b".repeat(64));
    const result = container.querySelector(".actuator-review-draft-result");
    expect(result?.textContent).toContain("Correction category: physical");
    expect(result?.textContent).toContain("repeat capture");
    expect(result?.textContent).toContain("elbow.nominal / simulation_validity: held-out bench");
    expect(result?.textContent).toContain("captures/elbow.json");
    expect(result?.textContent).toContain("a".repeat(64));
    expect(container.textContent).toContain("rerun elbow.nominal");
    expect(container.textContent).toContain("Engineer review required; this draft has not been applied.");
    await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("stale");
    expect(container.textContent).toContain("Draft base raw revision: " + "b".repeat(64));
  });
});
