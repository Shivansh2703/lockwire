import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { DECLARE_FORM, DIFF, HISTORY, PAGE, PAGE_WITH_SCENE, PLAN, deferred, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload, PlanPayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { readRecents } from "./recents";
import { applyHighlight } from "./scene/robotScene";
import { getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";

const canvasProps = vi.hoisted(() => ({ onDropPart: undefined as undefined | ((d: string, hit: null) => void) }));
vi.mock("./scene/Canvas", () => ({
  Canvas: ({ children, onDropPart }: { children?: React.ReactNode; onDropPart?: (d: string, hit: null) => void }) => { canvasProps.onDropPart = onDropPart; return <div data-testid="scene-canvas">{children}</div>; },
  useScene: () => ({}),
}));
vi.mock("./scene/robotScene", async (orig) => ({ ...(await orig<typeof import("./scene/robotScene")>()), applyHighlight: vi.fn(), applyDim: vi.fn(), focusCamera: vi.fn() }));


let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  mockMatchMedia();
  setSelectedPartId(null);
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



describe("App mounts OpenFace first, then CheckFace from the real preload reply", () => {
  it("one Open robot click checks the model and, with a lockfile already beside it, shows it with no prompt", async () => {
    const startFromUrdf = vi.fn().mockResolvedValueOnce({ started: false }).mockResolvedValue({
      started: true, urdf: "/tmp/robot.urdf", start: { path: "/tmp/lockwire.yaml", revision: "a".repeat(64), written: false },
      opened: { opened: true, path: "/tmp/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD },
    });
    Object.assign(window, { lockwire: { startFromUrdf, getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    act(() => root.render(<App />));
    const open = () => [...container.querySelectorAll("button")].find(b => b.textContent === "Open robot")!.click();
    await act(async () => open());
    expect(container.querySelector(".open-face")).not.toBeNull();
    await act(async () => open());
    expect(container.querySelector(".check-headline")).not.toBeNull();
    expect(container.querySelector(".finding-card")).not.toBeNull();
    expect(container.querySelector("[role=alertdialog]")).toBeNull();
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Overwrite from model")!.click());
    expect(startFromUrdf).toHaveBeenLastCalledWith({ urdf: "/tmp/robot.urdf", expected_revision: "a".repeat(64) });
  });

  it("shows the Start reply's note, so missing meshes are not silent", async () => {
    const note = "5 of 5 mesh files not found inside the robot's package folder; the 3D model is not recorded.";
    const startFromUrdf = vi.fn().mockResolvedValue({
      started: true, urdf: "/tmp/robot.urdf", start: { path: "/tmp/lockwire.yaml", revision: "a".repeat(64), written: true, note },
      opened: { opened: true, path: "/tmp/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD },
    });
    Object.assign(window, { lockwire: { startFromUrdf, getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    act(() => root.render(<App />));
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Open robot")!.click());
    expect(container.querySelector(".start-note")?.textContent).toBe(note);
  });

  it("a slow first load does not wipe the refusal from an overwrite clicked meanwhile", async () => {
    const page = deferred<PagePayload>();
    const startFromUrdf = vi.fn().mockResolvedValueOnce({
      started: true, urdf: "/tmp/robot.urdf", start: { path: "/tmp/lockwire.yaml", revision: "a".repeat(64), written: false },
      opened: { opened: true, path: "/tmp/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD },
    }).mockRejectedValue(new Error("lockfile changed since it was opened; reload before overwriting"));
    Object.assign(window, { lockwire: { startFromUrdf, getContractPage: vi.fn().mockReturnValue(page.promise),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    act(() => root.render(<App />));
    const click = (label: string) => [...container.querySelectorAll("button")].find(b => b.textContent === label)!.click();
    await act(async () => click("Open robot"));
    await act(async () => click("Overwrite from model"));
    await act(async () => page.resolve({ ...PAGE, revision: "a".repeat(64) }));
    expect(container.querySelector(".open-face-error")?.textContent).toContain("changed since");
  });

  it("catches initial open rejection and retries the opening operation", async () => {
    const startFromUrdf = vi.fn().mockRejectedValueOnce(new Error("sidecar unavailable")).mockResolvedValue({ started: false });
    Object.assign(window, { lockwire: { startFromUrdf } });
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
    expect(container.querySelector("[role=alert]")?.textContent).toContain("sidecar unavailable");
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Retry opening")!.click());
    expect(startFromUrdf).toHaveBeenCalledTimes(2);
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("test_first_launch_shows_open_face_not_a_check_face", () => {
    root = createRoot(container);
    act(() => root.render(<App />));
    expect(container.querySelector(".open-face")).not.toBeNull();
    expect(container.querySelector(".check-headline")).toBeNull();
  });

  it("test_a_cancelled_dialog_leaves_the_app_on_open_face", async () => {
    (window as unknown as { lockwire: object }).lockwire = {
      startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: false })),
    };
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
    expect(container.querySelector(".open-face")).not.toBeNull();
  });

  it("test_a_real_preload_reply_mounts_check_face_with_the_headline_on_the_page", async () => {
    // The mocked preload returns the checked-in payload fixture -- the accept line's own
    // wording: this proves App wires a real preload reply through to CheckFace, not that
    // the app's own IPC/sidecar plumbing is exercised here (that is ipc.test.ts's job).
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

    expect(container.querySelector(".open-face")).toBeNull();
    const headline = container.querySelector(".check-headline");
    expect(headline).not.toBeNull();
    // The sentence moved to the pill's tooltip; the page shows the verdict and the count.
    expect(headline!.getAttribute("title")).toBe(X500_CHECK_PAYLOAD.headline);
    expect(headline!.textContent).toMatch(/^NO-GO\d+ problems?( · \d+ modes)?( · \d+ not checked)?$/);
  });
});

describe("App owns the mode tab router", () => {
  async function openToCheckFace(page: PagePayload = PAGE, form: unknown = DECLARE_FORM) {
    const writeContract = vi.fn().mockResolvedValue({ payload: { written: true }, refusal: null });
    const compareContractWhatIf = vi.fn().mockResolvedValue({
      current: { verdict: "NO-GO", scored: 1, findings: [{}] },
      proposed: { verdict: "GO", scored: 2, findings: [] },
      diff: "proposal changes capacity",
    });
    const getContractPage = vi.fn().mockResolvedValue(page);
    const undoContract = vi.fn();
    (window as unknown as { lockwire: object }).lockwire = {
      startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/x500/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
      getContractPage,
      getDeclareForm: vi.fn().mockResolvedValue(form),
      getHistory: vi.fn().mockResolvedValue(HISTORY),
      diffContract: vi.fn().mockResolvedValue(DIFF),
      writeContract,
      compareContractWhatIf,
      undoContract,
      choosePlan: vi.fn().mockResolvedValue("/tmp/plan.yaml"),
      checkPlan: vi.fn().mockImplementation(async (r: { plan: string }) => (r.plan === "" ? null : PLAN)),
    };
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
    return { getContractPage, undoContract, writeContract, compareContractWhatIf };
  }

  function clickTab(name: string) {
    const tab = Array.from(container.querySelectorAll('[role="tab"]')).find((el) => el.textContent === name)!;
    act(() => tab.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  }

  it("test_check_mode_renders_frame_with_check_face", async () => {
    await openToCheckFace();
    expect(container.querySelector(".shell")).not.toBeNull();
    expect(container.querySelector(".check-headline")).not.toBeNull();
    const cards = Array.from(container.querySelectorAll<HTMLElement>(".finding-card"));
    const firstCard = cards[0];
    act(() => firstCard!.querySelector("button")!.click()); // the detail opens inline on select
    expect(firstCard?.dataset.shape).toBe(PAGE.cards[0]?.shape);
    expect(firstCard?.querySelector(".finding-info")?.getAttribute("title")).toBe(PAGE.cards[0]?.figure_note); // number-note: a tooltip, not body text
    expect(firstCard?.querySelector(".finding-fix")?.textContent).toBe(PAGE.cards[0]?.fix);
    expect(cards.map((card) => card.dataset.shape)).toEqual(PAGE.cards.map((card) => card.shape).filter((_, i) => i !== 1)); // the second budget card is the first rail's other mode: one row
  });

  it("test_page_faces_route from the generated page document", async () => {
    await openToCheckFace();
    clickTab("Design");
    expect(container.querySelector(".design-face-empty")?.textContent).toBe("no Design face yet");
    clickTab("Plan");
    expect(container.querySelector(".plan-input")).not.toBeNull();
    clickTab("Docs");
    expect(container.querySelector(".reference-face")).not.toBeNull();
  });

  it("a drop that lands on nothing, or with no witness name, says so instead of doing nothing", async () => {
    await openToCheckFace(PAGE_WITH_SCENE);
    act(() => canvasProps.onDropPart!("dev.x", null));
    expect(container.querySelector(".open-face-error")?.textContent).toContain("Drop on the robot's body");
    await openToCheckFace(PAGE_WITH_SCENE, { ...DECLARE_FORM, witness: { prefill: "" } });
    act(() => canvasProps.onDropPart!("dev.x", null));
    expect(container.querySelector(".open-face-error")?.textContent).toContain("Set your witness name");
  });

  it("test_switching_back_to_check_remounts_check_face", async () => {
    await openToCheckFace();
    clickTab("Plan");
    expect(container.querySelector(".plan-input")).not.toBeNull();
    clickTab("Check");
    expect(container.querySelector(".check-headline")).not.toBeNull();
  });


  it("a GO plan lights the parts its steps touch", async () => {
    await openToCheckFace(PAGE_WITH_SCENE);
    clickTab("Plan");
    await act(async () => {});
    vi.mocked(window.lockwire.checkPlan).mockResolvedValueOnce({ ...PLAN, steps: [{ path: "plan/steps/0/rail_load/draw", status: "PASS", not_scored_reason: null, targets: [{ kind: "rail", id: "rail.x", label: "x", parts: ["frame"] }] }] });
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    expect(vi.mocked(applyHighlight).mock.lastCall?.[1]).toEqual([{ id: "frame", level: "pass" }]);
  });

  it("a plan beside the lockfile loads itself, and with none the page stays empty", async () => {
    await openToCheckFace();
    clickTab("Plan");
    await act(async () => {});
    expect(window.lockwire.checkPlan).toHaveBeenCalledWith({ contract: "/tmp/x500/lockwire.yaml", plan: "" });
    expect(container.querySelector(".verdict-word")).toBeNull();
    act(() => root.unmount());
    root = createRoot(container);
    await openToCheckFace();
    vi.mocked(window.lockwire.checkPlan).mockResolvedValue(PLAN);
    clickTab("Plan");
    await act(async () => {});
    expect(container.querySelector(".verdict-word")?.textContent).toBe("GO");
    expect(container.querySelector(".kit-filepick .kit-cli-text")?.textContent).toBe("lockwire plan");
  });

  it("a sibling plan that cannot be read says so in one line", async () => {
    await openToCheckFace();
    vi.mocked(window.lockwire.checkPlan).mockRejectedValue(new Error("bad plan file"));
    clickTab("Plan");
    await act(async () => {});
    expect(container.querySelector(".plan-total")?.textContent).toBe("The example plan couldn't be read: bad plan file");
  });

  it("test_plan_file_runs_through_the_served_plan_channel", async () => {
    await openToCheckFace();
    clickTab("Plan");
    await act(async () => {});
    vi.mocked(window.lockwire.checkPlan)
      .mockResolvedValueOnce(PLAN)
      .mockResolvedValueOnce({ ...PLAN, verdict: "NOT_SCORED", exit_code: 3 });
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    expect(window.lockwire.checkPlan).toHaveBeenCalledWith({
      contract: "/tmp/x500/lockwire.yaml",
      plan: "/tmp/plan.yaml",
    });
    expect(container.querySelector(".verdict-word")?.textContent).toBe("GO");
    clickTab("Declare");
    clickTab("Plan");
    expect(container.querySelector(".verdict-word")?.textContent).toBe("GO");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    expect(container.querySelector(".verdict-word")?.textContent).toBe("Not scored");
  });

  it("binds the selected plan finding citation into Reference", async () => {
    await openToCheckFace();
    vi.mocked(window.lockwire.checkPlan).mockResolvedValue({
      ...PLAN,
      findings: [{
        severity: "not_scored",
        quantity: "plan/steps/0/joint_target/joint",
        observed: "joint missing",
        expected: "declared joint",
        action: "declare joint",
        source: "model/arm.urdf",
        witness: "model owner",
        alternatives: [{ text: "use elbow joint", value: 1, unit: "count", kind: "model" }],
      }],
    });
    clickTab("Plan");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    act(() => container.querySelector<HTMLButtonElement>(".plan-finding .kit-finding")!.click());
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "View citation")!.click());
    const selected = container.querySelector(".reference-plan-finding");
    expect(container.querySelector(".reference-face")).not.toBeNull();
    expect(selected?.textContent).toContain("model/arm.urdf");
    expect(selected?.textContent).toContain("model owner");
    expect(selected?.textContent).toContain("use elbow joint (model)");
    expect(selected?.querySelector("li")?.dataset.kind).toBe("model");
  });

  it("hides a selected plan citation after a successful revision change", async () => {
    const { writeContract } = await openToCheckFace();
    vi.mocked(window.lockwire.checkPlan).mockResolvedValue({
      ...PLAN,
      findings: [{
        severity: "not_scored", quantity: "plan/steps/0/joint_target/joint",
        observed: "joint missing", expected: "declared joint", action: "declare joint",
        source: "model/arm.urdf", witness: "model owner",
      }],
    });
    writeContract.mockResolvedValue({ payload: { written: true, revision: "next-revision" }, refusal: null });
    clickTab("Plan");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    act(() => container.querySelector<HTMLButtonElement>(".plan-finding .kit-finding")!.click());
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "View citation")!.click());
    expect(container.querySelector(".reference-plan-finding")).not.toBeNull();
    clickTab("Declare");
    await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click());
    clickTab("Docs");
    expect(container.querySelector(".reference-plan-finding")).toBeNull();
  });

  it("opening another robot clears the previous robot's history diff", async () => {
    await openToCheckFace();
    const showDiff = async () => {
      clickTab("Docs");
      act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg-opt input")].find((i) => i.parentElement?.textContent === "History")!.click());
      return container.querySelector(".history-diff-result");
    };
    await showDiff();
    await act(async () => container.querySelector<HTMLButtonElement>(".history-diff-disk")!.click());
    expect(container.querySelector(".history-diff-result")).not.toBeNull();
    clickTab("Check");
    Object.assign(window.lockwire, { openLockfile: vi.fn().mockResolvedValue({ opened: true, path: "/tmp/b.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }) });
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    expect(await showDiff()).toBeNull();
  });

  it("a slow earlier open landing after a faster later one does not win", async () => {
    const first = deferred<any>(), second = deferred<any>(); let opens = 0;
    Object.assign(window, { lockwire: { startFromUrdf: () => (++opens === 1 ? first.promise : second.promise).then(picked),
      getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container); act(() => root.render(<App />)); clickOpenRobot(); clickOpenRobot();
    const doc = (path: string) => ({ opened: true, path, payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD });
    await act(async () => second.resolve(doc("/tmp/b.yaml")));
    await act(async () => first.resolve(doc("/tmp/a.yaml")));
    expect(readRecents()[0]?.path).toBe("/tmp/b.yaml");
  });

  it("drops a late Plan result when a second document opens", async () => {
    const first = deferred<any>(), second = deferred<any>(), plan = deferred<PlanPayload>();
    const secondPage = deferred<PagePayload>(); let pages = 0;
    Object.assign(window, { lockwire: { startFromUrdf: () => first.promise.then(picked), openLockfile: () => second.promise,
      getContractPage: () => ++pages === 1 ? Promise.resolve(PAGE) : secondPage.promise, getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
      choosePlan: vi.fn().mockResolvedValue("/tmp/plan"), checkPlan: (r: { plan: string }) => (r.plan === "" ? Promise.resolve(null) : plan.promise) } });
    root = createRoot(container); act(() => root.render(<App />)); clickOpenRobot();
    await act(async () => first.resolve({ opened: true, path: "/tmp/a.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }));
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click()); // opens are ordered by start; the second begins once the first has landed
    await act(async () => second.resolve({ opened: true, path: "/tmp/b.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }));
    clickTab("Plan"); await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    await act(async () => secondPage.resolve(PAGE));
    await act(async () => { plan.resolve(PLAN); });
    clickTab("Plan");
    expect(container.querySelector(".verdict-word")).toBeNull();
  });


  it("test_declare_uses_the_opened_contract_and_real_write_channel", async () => {
    await openToCheckFace();
    clickTab("Declare");
    expect(container.querySelector(".declare-face")).not.toBeNull();
    expect(container.querySelector(".declare-values .kit-tree-row")?.textContent).toContain("Total · mass"); expect(container.textContent).toContain("mass");
    expect(container.textContent).not.toContain("Declare unavailable");
    const source = container.querySelector('input[aria-label="source"]')!;
    act(() => {
      source.dispatchEvent(new InputEvent("input", { bubbles: true, data: "manual" }));
    });
    const button = container.querySelector(".declare-face button")!;
    await act(async () => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(window.lockwire.writeContract).toHaveBeenCalledWith({
      command: "declare",
      args: {
        contract: "/tmp/x500/lockwire.yaml",
        path: "mass.total",
        value: "2 kg",
        source: "",
        witness: "Maya",
        expected_revision: "revision",
      },
    });
  });

  it("adding a catalog part writes it and reloads the page and check with no recheck press", async () => {
    const catalog = { rails: ["rail.5v"], parts: [{ id: "cat.servo", name: "Servo", fits: ["rail.5v"],
      numbers: [{ key: "draw_max", value: 7, unit: "W", kind: "datasheet" as const, source: "datasheet p1" }] }] };
    const { getContractPage, writeContract } = await openToCheckFace(PAGE, { ...DECLARE_FORM, add_part: catalog });
    clickTab("Declare");
    const openLockfile = vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/x500/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }));
    Object.assign(window.lockwire, { openLockfile });
    getContractPage.mockClear();
    const set = (label: string, value: string) => {
      const el = container.querySelector<HTMLSelectElement | HTMLInputElement>(`[aria-label="${label}"]`)!;
      act(() => { el.value = value; el.dispatchEvent(new Event(el.tagName === "SELECT" ? "change" : "input", { bubbles: true })); });
    };
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "+ Add part")!.click()); // its own surface, from the Values header
    set("catalog part", "cat.servo");
    set("part witness", "Maya");
    const add = [...container.querySelectorAll("button")].find((b) => b.textContent === "Add part")!;
    await act(async () => add.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "add", args: expect.objectContaining({ id: "dev.servo", fields: expect.objectContaining({ power_rail: "rail.5v", power_draw: "7 W" }) }),
    }));
    expect(openLockfile).toHaveBeenCalledWith({ contract: "/tmp/x500/lockwire.yaml" });
    expect(getContractPage).toHaveBeenCalled(); // the refreshed page/check arrived without pressing recheck
  });

  it("test_review_routes_selected_value_and_surfaces_stale_revision_without_advancing_it", async () => {
    const { writeContract } = await openToCheckFace();
    clickTab("Declare");
    act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg input")].find((i) => i.parentElement!.textContent === "Question")!.click());
    const note = container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Review note"]')!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(note, "check scale calibration");
      note.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const record = [...container.querySelectorAll("button")].find((b) => b.textContent === "Record review")!;
    writeContract.mockResolvedValueOnce({ payload: { written: false, revision: "untrusted-revision" }, refusal: "stale raw revision" });
    await act(async () => record.click());
    expect(writeContract).toHaveBeenLastCalledWith({
      command: "review",
      args: {
        contract: "/tmp/x500/lockwire.yaml",
        path: "mass.total",
        state: "questioned",
        witness: "Maya",
        note: "check scale calibration",
        expected_revision: "revision",
      },
    });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("stale raw revision");
    await act(async () => record.click());
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "review", args: expect.objectContaining({ expected_revision: "revision" }),
    }));
    writeContract.mockResolvedValueOnce({ payload: { written: true, revision: "reviewed-revision" }, refusal: null });
    await act(async () => record.click());
    await act(async () => record.click());
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "review", args: expect.objectContaining({ expected_revision: "reviewed-revision" }),
    }));
  });

  it("test_declare_mounts_a_read_only_current_proposed_comparison_and_preserves_declaration", async () => {
    const { compareContractWhatIf, writeContract } = await openToCheckFace();
    clickTab("Declare");
    for (const [label, next] of [["source", "caliper.csv"], ["witness", "Maya"]]) {
      const input = container.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, next);
      act(() => input.dispatchEvent(new Event("input", { bubbles: true })));
    }

    const compare = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Compare",
    ) as HTMLButtonElement;
    expect(compare.disabled).toBe(false);
    await act(async () => compare.click());
    expect(compareContractWhatIf).toHaveBeenCalledWith({
      contract: "/tmp/x500/lockwire.yaml",
      expected_revision: "revision",
      change: { path: "mass.total", value: "2 kg", source: "caliper.csv", witness: "Maya" },
    });
    expect(writeContract).not.toHaveBeenCalled();
    expect([...container.querySelectorAll(".measure-compare-result .kit-pill")].map((p) => p.textContent)).toEqual(["NO-GO", "GO"]);

    const declare = container.querySelector<HTMLButtonElement>(".declare-face button")!;
    await act(async () => declare.click());
    expect(writeContract).toHaveBeenCalledWith({
      command: "declare",
      args: {
        contract: "/tmp/x500/lockwire.yaml",
        path: "mass.total",
        value: "2 kg",
        source: "caliper.csv",
        witness: "Maya",
        expected_revision: "revision",
      },
    });
  });


  it("test_reference_mounts_validated_history_and_real_diff", async () => {
    await openToCheckFace();
    clickTab("Docs");
    await act(async () => {});
    act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg-opt input")].find((i) => i.parentElement?.textContent === "History")!.click());
    expect(container.querySelector(".history-entry")?.textContent).toContain("lock mount");
    await act(async () => container.querySelector<HTMLButtonElement>(".history-diff-disk")!.click());
    expect(window.lockwire.diffContract).toHaveBeenCalledWith({ contract: "/tmp/x500/lockwire.yaml", a: "abc123" });
    expect(container.querySelector(".history-diff-result")?.textContent).toBe("No changes");
  });

  it("reloads the raw revision after Undo before Declare and CAD writes without leaking selection across pages", async () => {
    const beforeUndo = "a".repeat(64);
    const afterUndo = "b".repeat(64);
    const afterDeclare = "c".repeat(64);
    const afterCad = "d".repeat(64);
    const initialPage = { ...PAGE_WITH_SCENE, revision: beforeUndo };
    const { getContractPage, undoContract, writeContract } = await openToCheckFace(initialPage);
    let diskRevision = beforeUndo;
    getContractPage.mockImplementation(async () => ({ ...PAGE_WITH_SCENE, revision: diskRevision }));
    undoContract.mockImplementation(async (request: { apply?: boolean }) => {
      if (request.apply) diskRevision = afterUndo;
      return {
        refusal: null,
        payload: { kind: "restore", scope: ["lockwire.yaml"], sentence: "restores the contract from HEAD" },
      };
    });
    writeContract.mockImplementation(async (request: { command: string; args: { expected_revision?: string } }) => {
      const current = request.args.expected_revision === diskRevision;
      if (current) diskRevision = request.command === "declare" ? afterDeclare : afterCad;
      return {
        payload: { written: current, revision: diskRevision },
        refusal: current ? null : "contract changed since it was opened; reload before applying this edit",
      };
    });

    clickTab("Design");
    act(() => container.querySelectorAll<HTMLButtonElement>('.kit-tree-chev[aria-expanded="false"]').forEach((c) => c.click()));
    act(() => container.querySelector<HTMLButtonElement>('[data-part-id="frame"]')!.click());
    clickTab("Docs");
    act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg-opt input")].find((i) => i.parentElement?.textContent === "History")!.click());
    const message = container.querySelector<HTMLInputElement>('[aria-label="commit message"]')!;
    const source = container.querySelector<HTMLInputElement>('[aria-label="undo source"]')!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      setter.call(message, "pending witness note");
      message.dispatchEvent(new Event("input", { bubbles: true }));
      setter.call(source, "HEAD~1");
      source.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector<HTMLButtonElement>(".history-undo-plan")!.click());
    expect(getContractPage).toHaveBeenCalledOnce();
    expect(container.querySelector(".history-write-status")).toBeNull();

    await act(async () => container.querySelector<HTMLButtonElement>(".history-undo-apply")!.click());
    expect(getContractPage).toHaveBeenLastCalledWith({ contract: "/tmp/x500/lockwire.yaml" });
    expect(getContractPage).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".history-write-status")?.textContent).toBe("undo applied");
    expect(container.querySelector<HTMLInputElement>('[aria-label="commit message"]')!.value).toBe("pending witness note");
    expect(container.querySelector<HTMLInputElement>('[aria-label="undo source"]')!.value).toBe("HEAD~1");
    expect(container.querySelector(".history-undo-plan-result")).not.toBeNull();

    clickTab("Declare");
    await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click());
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "declare",
      args: expect.objectContaining({ expected_revision: afterUndo }),
    }));
    expect(container.querySelector(".kit-notice-info")?.textContent).toBe("Written");

    clickTab("Design");
    expect(getSelectedPartId()).toBe("frame"); // the chosen part outlives the tab
    act(() => container.querySelectorAll<HTMLButtonElement>('.kit-tree-chev[aria-expanded="false"]').forEach((c) => c.click()));
    const frame = container.querySelector<HTMLButtonElement>('[data-part-id="frame"]')!;
    expect(frame.getAttribute("aria-pressed")).toBe("true");
    act(() => frame.click());
    for (const [label, value] of [["Source", "caliper"], ["Witness", "Maya"]]) {
      const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
      act(() => {
        setter.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }
    await act(async () => container.querySelector<HTMLFormElement>(".design-controls")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "move",
      args: expect.objectContaining({ expected_revision: afterDeclare, target: "frame" }),
    }));
    expect(container.querySelector(".design-controls-refusal")).toBeNull();
  });

  it.each([
    ["is refused", "contract changed since it was opened; reload before applying this edit", false],
    ["request is rejected", "undo request cancelled", true],
  ])("does not reload or replace the raw revision when Undo %s", async (_case, undoFailure, rejects) => {
    const beforeUndo = "a".repeat(64);
    const { getContractPage, undoContract, writeContract } = await openToCheckFace({ ...PAGE, revision: beforeUndo });
    undoContract.mockResolvedValueOnce({
      refusal: null,
      payload: { kind: "restore", scope: ["lockwire.yaml"], sentence: "restores the contract from HEAD" },
    });
    if (rejects) undoContract.mockRejectedValueOnce(new Error(undoFailure));
    else undoContract.mockResolvedValueOnce({ refusal: undoFailure, payload: null });

    clickTab("Docs");
    act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg-opt input")].find((i) => i.parentElement?.textContent === "History")!.click());
    const message = container.querySelector<HTMLInputElement>('[aria-label="commit message"]')!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      setter.call(message, "keep this proposal");
      message.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector<HTMLButtonElement>(".history-undo-plan")!.click());
    expect(getContractPage).toHaveBeenCalledOnce();
    await act(async () => container.querySelector<HTMLButtonElement>(".history-undo-apply")!.click());
    expect(getContractPage).toHaveBeenCalledOnce();
    expect(container.querySelector(".history-write-status")?.textContent).toBe(undoFailure);
    expect(container.querySelector<HTMLInputElement>('[aria-label="commit message"]')!.value).toBe("keep this proposal");
    expect(container.querySelector(".history-undo-plan-result")).not.toBeNull();

    clickTab("Declare");
    await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click());
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "declare",
      args: expect.objectContaining({ expected_revision: beforeUndo }),
    }));
  });

  it.each([
    ["fails", () => Promise.reject(new Error("page read failed")), "page read failed"],
    ["returns no revision", () => Promise.resolve({ ...PAGE_WITH_SCENE, revision: null }), "lockfile revision missing"],
  ])("invalidates the stale revision when Undo applies but page reload %s", async (_case, reload, reason) => {
    const beforeUndo = "a".repeat(64);
    const { getContractPage, undoContract, writeContract } = await openToCheckFace({ ...PAGE_WITH_SCENE, revision: beforeUndo });
    undoContract.mockResolvedValue({
      refusal: null,
      payload: { kind: "restore", scope: ["lockwire.yaml"], sentence: "restores the contract from HEAD" },
    });
    getContractPage.mockImplementation(reload);

    clickTab("Docs");
    act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg-opt input")].find((i) => i.parentElement?.textContent === "History")!.click());
    const message = container.querySelector<HTMLInputElement>('[aria-label="commit message"]')!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      setter.call(message, "keep this proposal");
      message.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector<HTMLButtonElement>(".history-undo-plan")!.click());
    await act(async () => container.querySelector<HTMLButtonElement>(".history-undo-apply")!.click());

    expect(getContractPage).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".history-write-status")?.textContent).toBe(
      `undo applied, but reload failed: ${reason}; reload before applying another edit`,
    );
    expect(container.querySelector<HTMLInputElement>('[aria-label="commit message"]')!.value).toBe("keep this proposal");
    expect(container.querySelector(".history-undo-plan-result")).not.toBeNull();

    clickTab("Declare");
    await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click());
    expect(writeContract).not.toHaveBeenCalled();
    expect(container.querySelector(".kit-notice-error")?.textContent).toContain("revision missing");

    clickTab("Design");
    expect(container.querySelector(".design-controls")).toBeNull();
  });
});
