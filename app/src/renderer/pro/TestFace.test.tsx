import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RobotJoint } from "../../shared/api.gen";
import type { PreflightPayload } from "../../shared/pro/types";
import { TestFace } from "./TestFace";
import { TestInput } from "./TestInput";

const PAYLOAD: PreflightPayload = {
  format_version: "2",
  schema_version: "1",
  robot: "X500",
  verdict: "NO-GO",
  exit_code: 1,
  counts: { violation: 1, warning: 0, not_scored: 0 },
  scored: 4,
  findings: [{
    severity: "violation", quantity: "model/joints/elbow/velocity", observed: "3.1 rad/s", expected: "<= 2.0 rad/s",
    action: "slow the elbow", source: "datasheet", witness: null,
  }],
  artifact_type: "lockwire-preflight-report",
  source: "/recordings/x500-flight.mcap",
  limits_raised: [],
};
const JOINTS = [{ name: "elbow", parent: "upper", child: "forearm" }] as RobotJoint[];

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const click = (el: Element | null | undefined) => act(async () => (el as HTMLElement).click());
const byText = (text: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === text);

describe("TestFace", () => {
  it("lists findings as Check-style rows with the joint they name, evidence on open", () => {
    act(() => root.render(<TestFace payload={PAYLOAD} joints={JOINTS} />));
    const row = container.querySelector(".kit-finding")!;
    expect(row.textContent).toContain("model/joints/elbow/velocity");
    expect(row.querySelector(".kit-chip")?.textContent).toBe("elbow");
    act(() => (row as HTMLElement).click());
    expect(container.querySelector(".kit-kv")?.textContent).toContain("3.1 rad/s");
    expect(container.textContent).toContain("witness unknown"); // provenance sits behind "Show the exact finding"
    expect(byText("View citation")).toBeUndefined();
  });

  it("one header per draw mode, no engine parentheticals, second mode collapsed", () => {
    const f = PAYLOAD.findings[0]!;
    const tail = " (declared enforcement provenance: datasheet)";
    const modes = [
      { ...f, observed: "35.20 W (draw mode: nominal)", expected: `<= 15.60 W${tail}`, observed_quantity: { value: 35.2, unit: "W" }, expected_quantity: { value: 15.6, unit: "W" } },
      { ...f, observed: "17.20 W (draw mode: 7W)", expected: `<= 15.60 W${tail}`, observed_quantity: { value: 17.2, unit: "W" }, expected_quantity: { value: 15.6, unit: "W" } },
    ];
    const labels = modes.map(() => ({ label: "Total power budget", targets: [] }));
    act(() => root.render(<TestFace payload={{ ...PAYLOAD, findings: modes, finding_labels: labels }} joints={JOINTS} />));
    act(() => (container.querySelector(".kit-finding") as HTMLElement).click());
    expect([...container.querySelectorAll("summary")].map((x) => x.textContent)).toEqual(expect.arrayContaining(["Nominal · 35.2 W of 15.6 W", "7W · 17.2 W of 15.6 W"]));
    expect(container.textContent).not.toContain("draw mode");
    expect(container.textContent).not.toContain("<=");
  });

  it("a lone finding with a mode gets the same header; equal headers name their rail", () => {
    const f = PAYLOAD.findings[0]!;
    const mk = (obs: number) => ({ ...f, observed: `${obs} W (draw mode: stall)`, expected: "<= 54 W", observed_quantity: { value: obs, unit: "W" }, expected_quantity: { value: 54, unit: "W" } });
    const rail = (label: string) => ({ label: "Total power budget", targets: [{ id: label, label, kind: "rail" }] });
    act(() => root.render(<TestFace payload={{ ...PAYLOAD, findings: [mk(275.52), mk(176.64)], finding_labels: [rail("Motor 12 V A"), rail("Servo 6 V")] } as never} joints={JOINTS} />));
    act(() => (container.querySelector(".kit-finding") as HTMLElement).click());
    expect([...container.querySelectorAll("summary")].map((x) => x.textContent)).toEqual(expect.arrayContaining(["Stall · Motor 12 V A · 275.5 W of 54 W", "Stall · Servo 6 V · 176.6 W of 54 W"]));
    act(() => root.render(<TestFace payload={{ ...PAYLOAD, findings: [mk(194.4)], finding_labels: [rail("Psu 12 V")] } as never} joints={JOINTS} />));
    expect(container.textContent).toContain("Stall · 194.4 W of 54 W");
    expect(container.textContent).not.toContain("Observed");
  });

  it("a mass finding gets the same header, no Observed grid", () => {
    const f = { ...PAYLOAD.findings[0]!, observed: "57 kg", expected: "<= 50 kg", observed_quantity: { value: 57, unit: "kg" }, expected_quantity: { value: 50, unit: "kg" } };
    act(() => root.render(<TestFace payload={{ ...PAYLOAD, findings: [f], finding_labels: [{ label: "Total mass budget", targets: [] }] }} joints={JOINTS} />));
    act(() => (container.querySelector(".kit-finding") as HTMLElement).click());
    expect(container.textContent).toContain("Mass · 57 kg of 50 kg");
    expect(container.textContent).not.toContain("Observed");
  });

  it("text without typed quantities or a mode keeps the old rows and no mode header", () => {
    const f = PAYLOAD.findings[0]!;
    const rows = [{ ...f, observed: "35.20 W (draw mode: nominal)", expected: "<= 15.60 W" }, { ...f, observed: "17.20 W", expected: "<= 15.60 W (declared enforcement provenance: x)" }];
    const labels = rows.map(() => ({ label: "Total power budget", targets: [] }));
    act(() => root.render(<TestFace payload={{ ...PAYLOAD, findings: rows, finding_labels: labels }} joints={JOINTS} />));
    act(() => (container.querySelector(".kit-finding") as HTMLElement).click());
    expect([...container.querySelectorAll("summary")].some((x) => x.textContent?.includes(" of "))).toBe(false);
    expect(container.textContent).toContain("<= 15.6 W (declared enforcement provenance: x)");
  });

  it("names a row with the engine's label and keeps the raw path for the detail", () => {
    const labelled = { ...PAYLOAD, finding_labels: [{ label: "Elbow speed", targets: [{ kind: "joint" as const, id: "elbow", label: "elbow" }] }] };
    act(() => root.render(<TestFace payload={labelled} joints={JOINTS} />));
    expect(container.querySelector(".kit-finding-title")!.textContent).toBe("Elbow speed");
    act(() => container.querySelector<HTMLButtonElement>(".kit-finding")!.click());
    expect(container.querySelector(".finding-path")!.textContent).toBe("model/joints/elbow/velocity");
  });

  it("reports the selected row to its parent so the robot can light up", () => {
    const onSelect = vi.fn();
    act(() => root.render(<TestFace payload={PAYLOAD} joints={JOINTS} selected={null} onSelect={onSelect} />));
    act(() => container.querySelector<HTMLButtonElement>(".kit-finding")!.click());
    expect(onSelect).toHaveBeenCalledWith(0);
  });
});

describe("TestInput", () => {
  const props = (over: Partial<Parameters<typeof TestInput>[0]> = {}) => ({
    contract: "/contracts/x500.yaml", payload: null, onPayload: vi.fn(),
    onChooseRecording: vi.fn().mockResolvedValue("/recordings/x500-flight.mcap"), onTest: vi.fn().mockResolvedValue(PAYLOAD), ...over,
  });

  it("offers Score recording only after a pick, then sends the whole request", async () => {
    const p = props({ context: "nominal-op" });
    act(() => root.render(<TestInput {...p} />));
    expect(container.querySelector(".kit-panel-title")?.textContent).toBe("Test");
    expect(byText("Score recording")).toBeUndefined();
    await click(byText("Choose recording"));
    expect(container.querySelector(".kit-filepick .kit-cli-text")?.textContent).toBe("x500-flight.mcap");
    expect(byText("Score recording")?.className).toContain("kit-btn-primary");
    await click(byText("Score recording"));
    expect(p.onTest).toHaveBeenCalledWith({ contract: "/contracts/x500.yaml", recording: "/recordings/x500-flight.mcap", context: "nominal-op" });
    expect(p.onPayload).toHaveBeenCalledWith(PAYLOAD);
  });

  it("a not-scored row sits inside the one 'not checked' disclosure", () => {
    const payload = { ...PAYLOAD, findings: [PAYLOAD.findings[0]!, { ...PAYLOAD.findings[0]!, severity: "not_scored" as const }] };
    act(() => root.render(<TestFace payload={payload} />));
    expect(container.querySelector(".kit-disclosure > summary")!.textContent).toBe("1 not checked");
    expect(container.querySelectorAll(".check-list > .kit-disclosure .kit-finding")).toHaveLength(1);
  });

  it("a scored GO lists what agreed, between the problems and the not-checked group", () => {
    const quiet = { ...PAYLOAD.findings[0]!, severity: "not_scored" as const };
    const payload = { ...PAYLOAD, verdict: "GO" as const, findings: [PAYLOAD.findings[0]!, quiet], passed: [{ label: "Update rate", observed: "10 Hz", expected: ">= 5 Hz" }, { label: "Rail load", observed: "4 W", expected: "<= 9 W" }] };
    act(() => root.render(<TestFace payload={payload} />));
    const summaries = [...container.querySelectorAll(".check-list > .kit-disclosure > summary")].map((x) => x.textContent);
    expect(summaries).toEqual(["2 agree", "1 not checked"]);
    const agree = container.querySelector(".check-list > .kit-disclosure")!;
    expect([...agree.querySelectorAll(".kit-finding-title")].map((x) => x.textContent)).toEqual(["Update rate", "Rail load"]);
    expect(agree.textContent).toContain("10 Hz");
  });

  it("a NO-GO with only not-scored rows leads with the missing evidence", () => {
    const payload = { ...PAYLOAD, findings: [{ ...PAYLOAD.findings[0]!, severity: "not_scored" as const }], missing_required_evidence: ["watched-command-stream" as const] };
    act(() => root.render(<TestFace payload={payload} />));
    const first = container.querySelector(".kit-finding")!;
    expect(first.textContent).toContain("Missing evidence");
    expect(first.textContent).toContain("watched command stream");
  });

  it("shows the verdict pill, marks the check unpowered, and lists the findings", () => {
    act(() => root.render(<TestInput {...props({ payload: PAYLOAD })} />));
    expect(container.querySelector(".verdict-word .kit-pill")?.textContent).toBe("NO-GO");
    expect(container.querySelector(".test-mode .kit-pill")?.textContent).toBe("Robot off");
    expect(container.querySelector(".test-mode")?.getAttribute("title")).toContain("recording only");
    // The mode sits beside the file chip, not in the header next to the verdict.
    expect(container.querySelector(".kit-panel-head .test-mode")).toBeNull();
    expect(container.querySelector(".test-input .test-mode")).not.toBeNull();
    expect(container.querySelectorAll(".kit-finding")).toHaveLength(1);
  });

  it("a NOT_SCORED result says nothing could be compared to the robot and keeps Score primary", async () => {
    const p = props({ payload: { ...PAYLOAD, verdict: "NOT_SCORED" as const }, onChooseRecording: vi.fn().mockResolvedValue("/tmp/recording") });
    act(() => root.render(<TestInput {...p} />));
    await click(byText("Choose recording"));
    expect(container.querySelector(".test-ready")?.textContent).toBe(`Nothing in this recording could be compared to ${PAYLOAD.robot}`);
    expect(byText("Score recording")!.className).toContain("kit-btn-primary");
  });

  it("recovers picker and scoring rejections as error Notices, keeping the recording for retry", async () => {
    const p = props({
      onChooseRecording: vi.fn().mockRejectedValueOnce(new Error("picker refused")).mockResolvedValue("/tmp/recording"),
      onTest: vi.fn().mockRejectedValueOnce(new Error("score refused")).mockResolvedValue(PAYLOAD),
    });
    act(() => root.render(<TestInput {...p} />));
    await click(byText("Choose recording"));
    expect(container.querySelector(".kit-notice-error")?.textContent).toContain("picker refused");
    await click(byText("Choose recording"));
    await click(byText("Score recording"));
    expect(container.querySelector(".kit-notice-error")?.textContent).toContain("score refused");
    expect(container.querySelector(".kit-cli-text")?.textContent).toBe("recording");
    await click(byText("Score recording"));
    expect(p.onTest).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".kit-notice-error")).toBeNull();
  });

  it("does not offer scoring when the picker is cancelled; removing the chip clears the result", async () => {
    const p = props({ onChooseRecording: vi.fn().mockResolvedValueOnce(null).mockResolvedValue("/r/a.mcap"), payload: PAYLOAD });
    act(() => root.render(<TestInput {...p} />));
    await click(byText("Choose recording"));
    expect(byText("Score recording")).toBeUndefined();
    await click(byText("Choose recording"));
    await click(container.querySelector('[title="Remove file"]'));
    expect(p.onPayload).toHaveBeenLastCalledWith(null);
    expect(byText("Score recording")).toBeUndefined();
  });

  it("sorted rows report their original finding index", () => {
    const warn = { ...PAYLOAD.findings[0]!, severity: "warning" as const, quantity: "model/joints/elbow/effort" };
    const onSelect = vi.fn();
    act(() => root.render(<TestFace payload={{ ...PAYLOAD, findings: [warn, PAYLOAD.findings[0]!] }} joints={JOINTS} selected={null} onSelect={onSelect} />));
    act(() => container.querySelector<HTMLButtonElement>(".kit-finding")!.click()); // bad sorts first
    expect(onSelect).toHaveBeenCalledWith(1);
  });
});
