import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PlanPayload, RobotJoint } from "../shared/api.gen";
import { PlanFace } from "./PlanFace";
import { PlanInput } from "./PlanInput";

const PAYLOAD: PlanPayload = {
  format_version: "2",
  schema_version: "1",
  robot: "X500",
  verdict: "NOT_SCORED",
  exit_code: 3,
  counts: { violation: 0, warning: 0, not_scored: 1 },
  scored: 0,
  findings: [],
  artifact_type: "lockwire-plan-report",
  steps: [
    {
      path: "plan/steps/0/joint_target/joint",
      status: "UNSCORED",
      not_scored_reason: "declare the joint before validating this target",
    },
  ],
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const JOINTS = [{ name: "elbow", parent: "upper", child: "forearm" }] as RobotJoint[];
const FINDING: PlanPayload["findings"][number] = {
  severity: "not_scored", quantity: "plan/steps/0/elbow/joint",
  observed: "joint missing", expected: "declared joint", action: "declare joint",
  source: "model/arm.urdf", witness: "model owner",
  alternatives: [{ text: "use elbow joint", value: 1, unit: "count", kind: "model" }],
};

function render(payload: PlanPayload = PAYLOAD, extra: Partial<Parameters<typeof PlanFace>[0]> = {}) {
  act(() => root.render(<PlanFace payload={payload} joints={JOINTS} {...extra} />));
}
const openRow = () => act(() => container.querySelector<HTMLButtonElement>(".kit-finding")!.click());

describe("PlanFace", () => {
  it("shows each step by a plain name, its status word, and the unscored reason as visible text", () => {
    render();
    const row = container.querySelector(".plan-outcome-row")!;
    expect(row.querySelector(".plan-outcome-path")!.textContent).toBe("Joint target");
    expect(row.querySelector(".kit-pill")!.textContent).toBe("Not scored");
    expect(row.querySelector(".plan-outcome-reason")!.textContent).toBe("declare the joint before validating this target");
  });

  it("titles a finding with the engine's label and chips its typed targets; the path moves to the detail", () => {
    const labels = [{ label: "Rail not declared", targets: [{ kind: "rail" as const, id: "rail.servo-motor", label: "servo-motor" }] }];
    render({ ...PAYLOAD, findings: [FINDING], finding_labels: labels });
    const row = container.querySelector(".kit-finding")!;
    expect(row.querySelector(".kit-finding-title")!.textContent).toBe("Rail not declared");
    expect(row.querySelector(".kit-chip")!.textContent).toBe("servo-motor");
    expect(row.querySelector(".kit-chip")!.getAttribute("title")).toBe("rail.servo-motor");
    openRow();
    expect(container.querySelector(".finding-path")!.textContent).toBe(FINDING.quantity);
  });

  it("shows a not-scored step once, as its step row with the reason visible", () => {
    render({ ...PAYLOAD, findings: [{ ...FINDING, quantity: PAYLOAD.steps![0]!.path }] });
    expect(container.querySelector(".kit-finding")).toBeNull();
    expect(container.querySelectorAll(".plan-outcome-row")).toHaveLength(1);
    expect(container.querySelector(".plan-outcome-reason")?.textContent).toBe(PAYLOAD.steps![0]!.not_scored_reason);
  });

  it("a failed step its violation names is one Fails row whose detail holds the path and numbers (packaged e2e reads this)", () => {
    const path = "plan/steps/0/rail_load/draw";
    render({ ...PAYLOAD, verdict: "NO-GO", exit_code: 1, counts: { violation: 1, warning: 0, not_scored: 0 }, scored: 1,
      findings: [{ severity: "violation", quantity: path, observed: "501.00 W rolled-up draw on rail.12v", expected: "<= 20.00 W capacity", action: "shed load", alternatives: [] }],
      steps: [{ path, status: "FAIL", not_scored_reason: null, targets: [] }],
      finding_labels: [{ label: "Draw", targets: [], whole: false, instead: [], fix: "Use less power on 12 V, or raise its limit" }] });
    expect(container.querySelector(`.plan-outcome-row[title="${path}"]`)).toBeNull();
    const row = container.querySelector(".plan-finding")!;
    expect(row.querySelector(".kit-finding-title")?.textContent).toBe("Draw");
    expect(row.querySelector(".kit-finding-word")?.textContent).toBe("Fails");
    openRow();
    expect(row.querySelector(".finding-path")?.textContent).toBe(path);
    expect(row.textContent).toContain("501.00 W");
    expect(row.textContent).toContain("20.00 W");
  });

  it("lists findings as the same rows Check uses, chipped with the joint they name", () => {
    render({ ...PAYLOAD, findings: [FINDING] });
    const row = container.querySelector(".kit-finding")!;
    expect(row.textContent).toContain("plan/steps/0/elbow/joint");
    expect(row.querySelector(".kit-chip")?.textContent).toBe("elbow");
    expect(container.querySelector(".kit-kv")).toBeNull();
  });

  it("opens a row to its citations, evidence class and alternatives", () => {
    render({ ...PAYLOAD, findings: [FINDING] });
    openRow();
    const text = container.querySelector(".plan-finding")?.textContent ?? "";
    for (const part of ["joint missing", "declared joint", "model/arm.urdf", "model owner", "declare joint", "use elbow joint"]) expect(text).toContain(part);
    expect(container.querySelector(".plan-finding [data-kind]")?.getAttribute("data-kind")).toBe("model");
    render({ ...PAYLOAD, findings: [{ ...FINDING, source: null, witness: null }] });
    expect(container.querySelector(".plan-finding")?.textContent).toContain("source unknown");
    expect(container.querySelector(".plan-finding")?.textContent).toContain("witness unknown");
  });

  it("jumps to the docs only through the quiet View citation button", () => {
    const onSelectFinding = vi.fn();
    render({ ...PAYLOAD, findings: [FINDING] }, { onSelectFinding });
    openRow();
    const cite = [...container.querySelectorAll("button")].find((b) => b.textContent === "View citation")!;
    expect(cite.className).toContain("kit-btn-quiet");
    act(() => cite.click());
    expect(onSelectFinding).toHaveBeenCalledWith(FINDING);
  });
});

describe("PlanInput", () => {
  const picked = { path: "/plans/pick-and-place.yaml", payload: PAYLOAD };

  it("one Panel: Choose plan shows the file chip, the verdict pill and the rows", async () => {
    const onPlan = vi.fn();
    act(() => root.render(<PlanInput onChoosePlan={async () => picked} payload={null} onPlan={onPlan} />));
    expect(container.querySelector(".kit-panel-title")?.textContent).toBe("Plan");
    expect(container.querySelector(".kit-filepick .kit-cli")).toBeNull();
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    expect(onPlan).toHaveBeenCalledWith(PAYLOAD);
    expect(container.querySelector(".kit-filepick .kit-cli-text")?.textContent).toBe("pick-and-place.yaml");
    act(() => root.render(<PlanInput onChoosePlan={async () => picked} payload={PAYLOAD} onPlan={onPlan} />));
    expect(container.querySelector(".verdict-word .kit-pill")?.textContent).toBe("Not scored");
    expect(container.querySelector(".plan-outcome-reason")?.textContent).toBe(PAYLOAD.steps?.[0]?.not_scored_reason);
    expect(container.querySelector(".titlebar, .mode-tabs")).toBeNull();
  });

  it("removing the file chip clears the result", async () => {
    const onPlan = vi.fn();
    act(() => root.render(<PlanInput onChoosePlan={async () => picked} payload={null} onPlan={onPlan} />));
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    act(() => container.querySelector<HTMLButtonElement>('[title="Remove file"]')!.click());
    expect(onPlan).toHaveBeenLastCalledWith(null);
    expect(container.querySelector(".kit-filepick .kit-cli")).toBeNull();
  });

  it("shows a rejection as an error Notice and stays ready to retry", async () => {
    const onChoosePlan = vi.fn().mockRejectedValueOnce(new Error("plan refused")).mockResolvedValue(picked);
    const onPlan = vi.fn();
    act(() => root.render(<PlanInput onChoosePlan={onChoosePlan} payload={null} onPlan={onPlan} />));
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    expect(container.querySelector(".kit-notice-error")?.textContent).toContain("plan refused");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    expect(onPlan).toHaveBeenCalledWith(PAYLOAD);
    expect(container.querySelector(".kit-notice-error")).toBeNull();
  });

  it("keeps the picker and no verdict when selection is cancelled", async () => {
    const onChoosePlan = vi.fn().mockResolvedValue(null);
    act(() => root.render(<PlanInput onChoosePlan={onChoosePlan} payload={null} onPlan={vi.fn()} />));
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    expect(container.querySelector(".kit-filepick button")?.textContent).toBe("Choose plan");
    expect(container.querySelector(".verdict-word")).toBeNull();
  });
});
