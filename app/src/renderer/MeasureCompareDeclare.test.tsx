import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MeasureCompareDeclare, type MeasureCompareObservation } from "./MeasureCompareDeclare";

let container: HTMLDivElement;
let root: Root;

const OBSERVATION: MeasureCompareObservation = {
  path: "devices/dev.x500/physical/mount_offset",
  value: [0.1, 0.2, 0.3],
  source: "check_scene_placement",
  witness: "Shivansh",
};
const DOC = { contractPath: "/tmp/robot/lockwire.yaml", expectedRevision: "a".repeat(64) };

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const compareButton = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Compare") as HTMLButtonElement;

describe("MeasureCompareDeclare", () => {
  it("sends the proposal to the read-only what-if bridge and shows before and after verdicts", async () => {
    const compareContractWhatIf = vi.fn(async () => ({
      current: { verdict: "NO-GO", scored: 1, findings: [{}] }, proposed: { verdict: "GO", scored: 2, findings: [] }, diff: "capacity changes",
    }));
    window.lockwire = { compareContractWhatIf } as unknown as typeof window.lockwire;
    act(() => root.render(<MeasureCompareDeclare observation={OBSERVATION} {...DOC} />));
    await act(async () => compareButton().click());

    expect(compareContractWhatIf).toHaveBeenCalledWith({
      contract: "/tmp/robot/lockwire.yaml",
      expected_revision: "a".repeat(64),
      change: { path: OBSERVATION.path, value: "0.1,0.2,0.3", source: "check_scene_placement", witness: "Shivansh" },
    });
    expect([...container.querySelectorAll(".kit-pill")].map((p) => p.textContent)).toEqual(["NO-GO", "GO"]);
    expect(container.querySelector(".kit-disclosure")?.textContent).toContain("capacity changes");
  });

  it("will not compare a proposal without source and witness", () => {
    act(() => root.render(<MeasureCompareDeclare observation={{ ...OBSERVATION, source: null }} {...DOC} />));
    expect(compareButton().disabled).toBe(true);
  });

  it("shows a refusal as an error notice", async () => {
    window.lockwire = { compareContractWhatIf: vi.fn().mockRejectedValue(new Error("stale revision")) } as unknown as typeof window.lockwire;
    act(() => root.render(<MeasureCompareDeclare observation={OBSERVATION} {...DOC} />));
    await act(async () => compareButton().click());
    expect(container.querySelector(".kit-notice-error")?.textContent).toBe("stale revision");
  });
});
