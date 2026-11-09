import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { DECLARE_FORM, DIFF, HISTORY, PAGE, PLAN, deferred, mockMatchMedia, picked } from "../App.testkit";
import type { PagePayload, PlanPayload } from "../../shared/api.gen";
import type { ActuatorCase } from "../../shared/pro/types";
import { X500_CHECK_PAYLOAD } from "../checkFace.fixture";
import { ActuatorCases } from "./ActuatorCases";
import { proApi } from "./api";
import { applyHighlight } from "../scene/robotScene";

vi.mock("../scene/Canvas", () => ({
  Canvas: ({ children }: { children?: React.ReactNode }) => <div data-testid="scene-canvas">{children}</div>,
  useScene: () => ({}),
}));
vi.mock("../scene/robotScene", async (orig) => ({ ...(await orig<typeof import("../scene/robotScene")>()), applyHighlight: vi.fn(), applyDim: vi.fn(), focusCamera: vi.fn() }));

// The paid tabs and slots of the App, mounted through the real pro/ registry.
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  mockMatchMedia();
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  Reflect.deleteProperty(window, "lockwire");
});

function clickOpenRobot() {
  const button = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Open robot")!;
  act(() => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

const ACTUATOR_CASES: ActuatorCase[] = [
    {
      case_id: "elbow.nominal",
      evidence: null,
      conformance: {
        label: "physical conformance" as const,
        verdict: "NOT_SCORED" as const,
        exit_code: 3,
        scored: null,
        comparisons: [{
          quantity: "actuator_cases/elbow.nominal/physical_position",
          observed: "0.11 rad", expected: "0.1 rad", margin: "within declared tracking error",
          observed_quantity: { value: .11, unit: "rad" }, expected_quantity: null,
          uncertainty: { value: .006, unit: "rad" },
          provenance: { source: "measurement-log/run-1", witness: "bench operator", locked_at: "2026-09-27" },
        }],
        gaps: [
          {
            severity: "not_scored" as const,
            quantity: "actuator_cases/elbow.nominal/conformance/evidence",
            observed: "no actuator evidence selected",
            expected: "a hash-bound actuator evidence artifact",
            owner: null,
            source: null,
            witness: null,
            action: "select evidence for physical conformance",
          },
        ],
        context: {
          case_id: "elbow.nominal", claim_id: "conformance", contract_revision: null,
          selected_evidence: { path: "physical/selected.json", sha256: "1".repeat(64) },
          evidence_class: "physical_measurement", source_witnesses: [], uncertainty: "unknown",
          dependencies: [], freshness: "unverified", validation_reference: null,
          reference_held_out: null, calibration_artifacts: [], draft_binding: null,
        },
      },
      simulation_validity: {
        label: "simulation validity" as const,
        verdict: "GO" as const,
        exit_code: 0,
        scored: 1,
        comparisons: [{
          quantity: "actuator_cases/elbow.nominal/simulation_validity/rmse",
          observed: "0.01 rad", expected: "<= 0.03 rad",
          margin: "within declared prediction error; position uncertainty unknown",
          provenance: null,
        }],
        gaps: [],
        context: {
          case_id: "elbow.nominal", claim_id: "simulation_validity", contract_revision: null,
          selected_evidence: { path: "simulation/selected.json", sha256: "2".repeat(64) },
          evidence_class: "prediction_with_held_out_reference", source_witnesses: [], uncertainty: "unknown",
          dependencies: [], freshness: "unverified",
          validation_reference: { path: "simulation/reference.json", sha256: "3".repeat(64) },
          reference_held_out: true,
          calibration_artifacts: [{ path: "simulation/calibration.json", sha256: "4".repeat(64) }],
          draft_binding: null,
        },
      },
    },
  ];

const ACTUATOR_PAYLOAD = {
  ...X500_CHECK_PAYLOAD,
  actuator_cases: ACTUATOR_CASES,
};
const PREFLIGHT = {
  format_version: "2", schema_version: "1", robot: "X500", verdict: "GO" as const, exit_code: 0,
  counts: { violation: 0, warning: 0, not_scored: 0 }, scored: 1, findings: [],
  artifact_type: "lockwire-preflight-report" as const, source: "/tmp/test.mcap", limits_raised: [],
};
const RUN_STATE = {
  status: "running" as const, exit_code: null, rows_written: 0, document: null, output: "", errors: "", error: null,
  health: {
    path: "/tmp/x500/health.jsonl", origin: { path: "/tmp/x500/lockwire.yaml", copied_at: null }, contract_sha: "abc",
    devices: [], foreign_rows: 0, series_excluded_foreign: 0, series_excluded_unplaceable: 0,
    not_watched: 0, scored: 0, chain: { rows: 0, skipped: 0, ok: true, line: null, why: null },
    summary: { last_run: null, watched: 0, worst: null, per_device: {}, skipped: 0 }, verdict: "NOT_SCORED" as const,
    exit_code: 3, interface_poll_s: 1, nothing_watched: true, next_step: "record a run",
  },
};

describe("the App with pro/ present", () => {
  it("shows the real Test and Run tabs and not the Pro line", async () => {
    Object.assign(window, { lockwire: {
      startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/robot.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
      getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
    } });
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot(); await act(async () => {});
    act(() => container.querySelector<HTMLButtonElement>(".shell-more")!.click());
    expect([...container.querySelectorAll("[role=tab]")].map((t) => t.textContent)).toEqual(["Design", "Declare", "Check", "Plan", "Test", "Run", "Docs"]);
    expect(container.querySelector(".shell-pro-line")).toBeNull();
  });

  it("completes Run inside one frame and keeps all seven root tabs functional", async () => {
    Object.assign(window, { lockwire: {
      startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/robot.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
      getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
      chooseRecording: vi.fn().mockResolvedValue("/tmp/run"), startRun: vi.fn().mockResolvedValue({ ...RUN_STATE, status: "completed" }),
    } });
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot(); await act(async () => {});
    act(() => container.querySelector<HTMLButtonElement>(".shell-more")!.click());
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>("[role=tab]")].find(b => b.textContent === "Run")!.click());
    await act(async () => container.querySelector<HTMLButtonElement>(".run-choose-recording button")!.click());
    await act(async () => container.querySelector<HTMLButtonElement>(".run-start")!.click());
    expect(container.querySelectorAll(".shell")).toHaveLength(1);
    expect(container.querySelectorAll("[role=tab]")).toHaveLength(7);
    expect(container.querySelector(".check-headline-left")?.textContent).toBe("Not scored");
    expect(container.querySelector(".kit-panel .day-one-notice")?.textContent).toBe("Nothing compared yet");
    for (const tab of container.querySelectorAll<HTMLButtonElement>("[role=tab]")) {
      act(() => tab.click());
      expect(tab.getAttribute("aria-selected")).toBe("true");
    }
  });

  it("Run passes the lit devices as the scene selection so the rest of the robot ghosts", async () => {
    const dev = { id: "dev.imu", name: "IMU", kind: "device", state: "faulty", word: "faulty", reason: "r", drives_joint: null, mount_frame: null };
    Object.assign(window, { lockwire: {
      startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/robot.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
      getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
      chooseRecording: vi.fn().mockResolvedValue("/tmp/run"), startRun: vi.fn().mockResolvedValue({ ...RUN_STATE, status: "completed", health: { ...RUN_STATE.health, nothing_watched: false, devices: [dev] } }),
    } });
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot(); await act(async () => {});
    act(() => container.querySelector<HTMLButtonElement>(".shell-more")!.click());
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>("[role=tab]")].find(b => b.textContent === "Run")!.click());
    await act(async () => container.querySelector<HTMLButtonElement>(".run-choose-recording button")!.click());
    await act(async () => container.querySelector<HTMLButtonElement>(".run-start")!.click());
    const last = vi.mocked(applyHighlight).mock.calls.at(-1)!;
    expect(last[1]).toContainEqual({ id: "dev.imu", level: "bad" });
    expect(last[2]).toContain("dev.imu");
  });

  it("shows the first finding above the collapsed actuator draft", async () => {
    (window as unknown as { lockwire: object }).lockwire = {
      startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/x500/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
      getContractPage: vi.fn().mockResolvedValue(PAGE),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM),
      getHistory: vi.fn().mockResolvedValue([]),
    };
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});

    const card = container.querySelector(".finding-card")!;
    const draft = container.querySelector(".actuator-review-draft")!;
    expect(draft.tagName).toBe("DETAILS");
    expect((draft as HTMLDetailsElement).open).toBe(false);
    expect(card.compareDocumentPosition(draft) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("test_the_existing_preload_reply_presents_both_actuator_verdicts_without_re_evaluating_them", async () => {
    (window as unknown as { lockwire: object }).lockwire = {
      startFromUrdf: vi.fn().mockResolvedValue(picked({
        opened: true,
        path: "/tmp/x500/lockwire.yaml",
        payload: ACTUATOR_PAYLOAD,
        checkPayload: ACTUATOR_PAYLOAD,
      })),
      getContractPage: vi.fn().mockResolvedValue(PAGE),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM),
      getHistory: vi.fn().mockResolvedValue([]),
    };
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});

    const claims = Array.from(container.querySelectorAll<HTMLElement>(".actuator-claim"));
    expect(claims.map((claim) => claim.querySelector(".actuator-claim-label")?.textContent)).toEqual([
      "physical conformance",
      "simulation validity",
    ]);
    expect(claims.map((claim) => claim.querySelector(".actuator-claim-verdict")?.textContent)).toEqual([
      "NOT_SCORED",
      "GO",
    ]);
    const [physical, simulation] = claims;
    expect(physical).toBeDefined();
    expect(simulation).toBeDefined();
    if (physical === undefined || simulation === undefined) {
      throw new Error("expected physical and simulation actuator claims");
    }
    const positive = physical.querySelector(".actuator-claim-comparison");
    expect(positive?.textContent).toContain("0.11 rad");
    expect(positive?.textContent).toContain("measurement-log/run-1");
    expect(positive?.textContent).toContain("bench operator");
    expect(positive?.textContent).toContain("uncertainty 0.006 rad");
    const simulationPass = simulation.querySelector(".actuator-claim-comparison");
    expect(simulationPass?.textContent).toContain("<= 0.03 rad");
    expect(simulationPass?.textContent).toContain("source unknown");
    expect(simulationPass?.textContent).toContain("witness unknown");
    expect(simulationPass?.textContent).toContain("uncertainty unknown");
    expect(simulationPass?.textContent).not.toContain("bench operator");
    expect(container.querySelector(".actuator-claim-gaps code")?.textContent).toBe(
      "actuator_cases/elbow.nominal/conformance/evidence",
    );
  });
});

it("shows simulation gap operands and unknown provenance without borrowing the physical witness", () => {
  const base = ACTUATOR_PAYLOAD.actuator_cases[0];
  if (base === undefined) throw new Error("expected actuator case fixture");
  const gapTemplate = base.conformance.gaps[0];
  if (gapTemplate === undefined) throw new Error("expected actuator gap fixture");
  const cases = [{ ...base, simulation_validity: {
    ...base.simulation_validity, verdict: "NO-GO" as const, exit_code: 1, comparisons: [],
    gaps: [{ ...gapTemplate, severity: "violation" as const,
      quantity: "actuator_cases/elbow.nominal/simulation_validity/rmse",
      observed: "0.19 rad", expected: "<= 0.03 rad" }],
  } }];
  root = createRoot(container);
  act(() => root.render(<ActuatorCases cases={cases} />));
  const gap = container.querySelectorAll(".actuator-claim")[1]?.querySelector(".actuator-claim-gaps");
  expect(gap?.textContent).toContain("0.19 rad");
  expect(gap?.textContent).toContain("<= 0.03 rad");
  expect(gap?.textContent).toContain("source unknown");
  expect(gap?.textContent).toContain("witness unknown");
  expect(gap?.textContent).toContain("position uncertainty unknown");
  expect(gap?.textContent).not.toContain("bench operator");
});

it("keeps mounted physical GO independent of a simulation reference gap", () => {
  const base = ACTUATOR_PAYLOAD.actuator_cases[0];
  if (base === undefined) throw new Error("expected actuator case fixture");
  const gapTemplate = base.conformance.gaps[0];
  if (gapTemplate === undefined) throw new Error("expected actuator gap fixture");
  const cases = [{ ...base,
    conformance: { ...base.conformance, verdict: "GO" as const, exit_code: 0, scored: 1, gaps: [] },
    simulation_validity: { ...base.simulation_validity,
      verdict: "NOT_SCORED" as const, exit_code: 3, scored: 0, comparisons: [],
      gaps: [{ ...gapTemplate,
        quantity: "actuator_cases/elbow.nominal/simulation_validity/reference",
        observed: "reference is synthetic, fitted, or unaligned",
        expected: "distinct held-out measured reference with valid clock mapping" }],
    },
  }];
  root = createRoot(container);
  act(() => root.render(<ActuatorCases cases={cases} />));
  const claims = Array.from(container.querySelectorAll(".actuator-claim"));
  expect(claims.map((claim) => claim.querySelector(".actuator-claim-verdict")?.textContent))
    .toEqual(["GO", "NOT_SCORED"]);
  const [physical, simulation] = claims;
  expect(physical?.querySelector(".actuator-claim-gaps")).toBeNull();
  expect(physical?.querySelector(".actuator-claim-comparison")?.textContent).toContain("bench operator");
  expect(simulation?.querySelector(".actuator-claim-gaps code")?.textContent)
    .toBe("actuator_cases/elbow.nominal/simulation_validity/reference");
  expect(simulation?.querySelector(".actuator-claim-comparison")).toBeNull();
  expect(simulation?.textContent).not.toContain("bench operator");
  expect(physical?.textContent).toContain("physical/selected.json");
  expect(physical?.textContent).not.toContain("simulation/selected.json");
  expect(physical?.textContent).not.toContain("simulation/reference.json");
  expect(physical?.textContent).not.toContain("simulation/calibration.json");
  expect(simulation?.textContent).toContain("simulation/selected.json");
  expect(simulation?.textContent).toContain("simulation/reference.json");
  expect(simulation?.textContent).toContain("held out");
  expect(simulation?.textContent).toContain("simulation/calibration.json");
  expect(simulation?.textContent).not.toContain("physical/selected.json");
});

describe("App owns the paid mode tabs", () => {
  async function openToCheckFace(page: PagePayload = PAGE) {
    const getContractPage = vi.fn().mockResolvedValue(page);
    (window as unknown as { lockwire: object }).lockwire = {
      startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/x500/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
      getContractPage,
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM),
      getHistory: vi.fn().mockResolvedValue(HISTORY),
      diffContract: vi.fn().mockResolvedValue(DIFF),
      choosePlan: vi.fn().mockResolvedValue("/tmp/plan.yaml"),
      checkPlan: vi.fn().mockResolvedValue(PLAN),
      chooseRecording: vi.fn().mockResolvedValue("/tmp/test.mcap"),
      checkRecording: vi.fn().mockResolvedValue(PREFLIGHT),
      startRun: vi.fn().mockResolvedValue(RUN_STATE),
      pollRun: vi.fn().mockResolvedValue({ ...RUN_STATE, status: "completed" }),
      cancelRun: vi.fn().mockResolvedValue({ ...RUN_STATE, status: "cancelled" }),
    };
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
  }

  function clickTab(name: string) {
    act(() => container.querySelector<HTMLButtonElement>(".shell-more")?.click()); 
    const tab = Array.from(container.querySelectorAll('[role="tab"]')).find((el) => el.textContent === name)!;
    act(() => tab.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  }

  it("test_switching_back_to_check_remounts_check_face", async () => {
    await openToCheckFace();
    clickTab("Run");
    expect(container.textContent).toContain("Pick a source, then Start.");
    clickTab("Check");
    expect(container.querySelector(".check-headline")).not.toBeNull();
  });

  it("test_recording_runs_through_the_served_preflight_channel", async () => {
    await openToCheckFace();
    clickTab("Test");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Score recording")!.click());
    expect(proApi().checkRecording).toHaveBeenCalledWith({
      contract: "/tmp/x500/lockwire.yaml",
      recording: "/tmp/test.mcap",
    });
    expect(container.querySelector(".kit-filepick .kit-cli-text")?.textContent).toBe("test.mcap");
  });

  it("test_open_robot_shows_no_empty_state_over_the_test_panel", async () => {
    await openToCheckFace();
    clickTab("Test");
    expect(container.textContent).not.toContain("No robot open");
  });

  it("test_result_survives_tab_navigation", async () => {
    await openToCheckFace();
    clickTab("Test");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Score recording")!.click());
    expect(container.querySelector(".verdict-word")?.textContent).toBe("GO");

    clickTab("Declare");
    clickTab("Test");
    expect(container.querySelector(".verdict-word")?.textContent).toBe("GO");
  });

  it("drops late Plan and Test results when a second document opens", async () => {
    const first = deferred<any>(), second = deferred<any>(), plan = deferred<PlanPayload>(), test = deferred<typeof PREFLIGHT>();
    const secondPage = deferred<PagePayload>(); let pages = 0;
    Object.assign(window, { lockwire: { startFromUrdf: () => first.promise.then(picked), openLockfile: () => second.promise,
      getContractPage: () => ++pages === 1 ? Promise.resolve(PAGE) : secondPage.promise, getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
      choosePlan: vi.fn().mockResolvedValue("/tmp/plan"), checkPlan: (r: { plan: string }) => r.plan === "" ? Promise.resolve(null) : plan.promise,
      chooseRecording: vi.fn().mockResolvedValue("/tmp/test.mcap"), checkRecording: () => test.promise } });
    root = createRoot(container); act(() => root.render(<App />)); clickOpenRobot();
    await act(async () => first.resolve({ opened: true, path: "/tmp/a.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }));
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click()); // opens are ordered by start; the second begins once the first has landed
    await act(async () => second.resolve({ opened: true, path: "/tmp/b.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }));
    clickTab("Plan"); await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    clickTab("Test"); await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Score recording")!.click());
    await act(async () => secondPage.resolve(PAGE));
    await act(async () => { plan.resolve(PLAN); test.resolve(PREFLIGHT); });
    clickTab("Plan");
    expect(container.querySelector(".verdict-word")).toBeNull();
    clickTab("Test");
    expect(container.querySelector(".verdict-word")).toBeNull();
  });

  const setValue = (input: HTMLInputElement, value: string) => act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new InputEvent("input", { bubbles: true, data: value }));
  });
  const pickSource = (label: string) => act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg-opt input")]
    .find((i) => i.parentElement?.textContent === label)!.click());
  const jobButton = () => [...container.querySelectorAll<HTMLButtonElement>(".kit-jobbar button")].find((b) => b.textContent === "Cancel");

  it("test_run_start_poll_cancel_use_the_real_controller_channels", async () => {
    await openToCheckFace();
    clickTab("Run");
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>(".run-choose-recording button")][0]!.click());
    await act(async () => container.querySelector<HTMLButtonElement>(".run-start")!.click());
    expect(proApi().startRun).toHaveBeenCalledWith({ contract: "/tmp/x500/lockwire.yaml", recording: "/tmp/test.mcap" });
    expect(container.querySelector(".kit-jobbar")?.textContent).toContain("Running");
    expect(container.querySelectorAll(".kit-jobbar button")).toHaveLength(1); // one Cancel, no duplicate
    await act(async () => jobButton()!.click());
    expect(proApi().cancelRun).toHaveBeenCalledOnce();
    await act(async () => container.querySelector<HTMLButtonElement>(".run-poll")!.click());
    expect(proApi().pollRun).toHaveBeenCalledOnce();
  });

  it("test_run_page_polls_itself_until_the_run_is_done", async () => {
    await openToCheckFace();
    clickTab("Run");
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>(".run-choose-recording button")][0]!.click());
    vi.useFakeTimers();
    try {
      await act(async () => container.querySelector<HTMLButtonElement>(".run-start")!.click());
      expect(container.querySelector(".kit-jobbar")?.textContent).toContain("Running");
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(proApi().pollRun).toHaveBeenCalledOnce();
      expect(container.querySelector(".kit-jobbar")).toBeNull(); // no bar once finished
      expect(container.querySelector(".check-headline")?.textContent).toBe("Not scored"); expect(container.textContent).not.toContain("Done"); // one status
    } finally { vi.useRealTimers(); }
  });

  it("test_live_run_requires_a_container_target_and_sends_its_trimmed_name", async () => {
    await openToCheckFace();
    clickTab("Run");
    pickSource("Docker");
    expect(container.querySelector('[aria-label="SSH host"]')).toBeNull(); // only the chosen source's fields

    const start = container.querySelector<HTMLButtonElement>(".run-start")!;
    expect(start.disabled).toBe(true);
    await act(async () => start.click());
    expect(proApi().startRun).not.toHaveBeenCalled();

    setValue(container.querySelector<HTMLInputElement>('[aria-label="Docker container name or ID"]')!, "  robot-live  ");
    expect(start.disabled).toBe(false);

    await act(async () => start.click());
    expect(proApi().startRun).toHaveBeenCalledWith({
      contract: "/tmp/x500/lockwire.yaml",
      live: true,
      container: "robot-live",
    });
  });

  it("sends a pinned SSH live target through the existing Run control", async () => {
    await openToCheckFace();
    clickTab("Run");
    pickSource("SSH");
    const start = container.querySelector<HTMLButtonElement>(".run-start")!;
    expect(start.disabled).toBe(true);
    for (const [label, value] of [
      ["SSH host", " 127.0.0.1 "], ["SSH port", "2222"], ["SSH user", "root"],
      ["SSH private key file", "/keys/id"], ["SSH known hosts file", "/keys/known_hosts"],
      ["SSH declared target", "declared-robot"], ["SSH declared image", "declared-image"],
      ["SSH keep bag", "/bags/partial"],
    ] as const) setValue(container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!, value);
    expect(start.disabled).toBe(false);
    await act(async () => start.click());
    expect(proApi().startRun).toHaveBeenCalledWith({
      contract: "/tmp/x500/lockwire.yaml", live: true, ssh_host: "127.0.0.1",
      ssh_port: 2222, ssh_user: "root", ssh_identity_file: "/keys/id",
      ssh_known_hosts: "/keys/known_hosts", ssh_target: "declared-robot",
      ssh_image: "declared-image", keep_bag: "/bags/partial",
    });
  });
});
