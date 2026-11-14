import { explainOpenError } from "./openError";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { PLACED_SOURCE } from "./DesignControls";
import { DECLARE_FORM, DIFF, HISTORY, PAGE, PAGE_WITH_SCENE, PLAN, deferred, goMode, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload, PlanPayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { pro } from "./proRegistry";
import { readRecents } from "./recents";
import { applyHighlight } from "./scene/robotScene";
import { getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";

const canvasProps = vi.hoisted(() => ({ onDropPart: undefined as undefined | ((d: string, hit: null) => void), skeleton: undefined as unknown, parts: [] as unknown[] }));
vi.mock("./scene/Canvas", () => ({
  Canvas: ({ children, onDropPart, skeleton, parts }: { children?: React.ReactNode; onDropPart?: (d: string, hit: null) => void; skeleton?: unknown; parts?: unknown[] }) => { canvasProps.onDropPart = onDropPart; canvasProps.skeleton = skeleton; canvasProps.parts = parts ?? []; return <div data-testid="scene-canvas">{children}</div>; },
  useScene: () => ({ meshes: [] }),
}));
const { replies } = vi.hoisted(() => ({ replies: [] as Promise<{ written: boolean; refusal: string | null }>[] }));
vi.mock("./DesignControls", async (orig) => {
  const real = await orig<typeof import("./DesignControls")>();
  return { ...real, DesignControls: (p: Parameters<typeof real.DesignControls>[0]) => <real.DesignControls {...p} onEdit={(r) => { replies.push(p.onEdit(r)); return replies.at(-1)!; }} /> };
});
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
    const getContractPage = vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) });
    const openLockfile = vi.fn().mockResolvedValue({ opened: true, path: "/tmp/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD });
    Object.assign(window, { lockwire: { startFromUrdf, getContractPage, openLockfile,
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
    expect(container.querySelector(".overwrite-from-model")).toBeNull(); // the model has not changed since its lock
    getContractPage.mockResolvedValue({ ...PAGE, revision: "a".repeat(64), model_changed: true });
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Re-check")!.click());
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Update from model file")!.click());
    expect(startFromUrdf).toHaveBeenLastCalledWith({ urdf: "/tmp/robot.urdf", expected_revision: "a".repeat(64) });
  });

  it.each([["/tmp/robot.wbt", true], ["/tmp/robot.xml", false]])("a Webots file says there is no 3D view, other types do not (%s)", async (urdf, says) => {
    const startFromUrdf = vi.fn().mockResolvedValue({
      started: true, urdf, start: { path: "/tmp/lockwire.yaml", revision: "a".repeat(64), written: true },
      opened: { opened: true, path: "/tmp/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD },
    });
    Object.assign(window, { lockwire: { startFromUrdf, getContractPage: vi.fn().mockResolvedValue({ ...PAGE_WITH_SCENE, joints: [{ name: "q", type: "revolute", parent: "a", child: "b", position: [0, 0, 0], axis: [0, 0, 1] }], revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    act(() => root.render(<App />));
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Open robot")!.click());
    expect(container.querySelector(".start-note")?.textContent ?? "").toContain(says ? "No 3D view for this file type" : "");
    expect(container.textContent?.includes("No 3D view for this file type")).toBe(says);
    expect(!!container.querySelector(".shell-hint")).toBe(!says); // nothing to turn: no orbit hint on a Webots file
    expect(canvasProps.skeleton === undefined && canvasProps.parts.length === (says ? 0 : 1)).toBe(true); // and no body, not even the joint skeleton
  });

  it("after a Webots file, opening another robot shows that robot's view", async () => {
    const reply = (urdf: string, dir: string) => ({ started: true, urdf, start: { path: `${dir}/lockwire.yaml`, revision: "a".repeat(64), written: false },
      opened: { opened: true, path: `${dir}/lockwire.yaml`, payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD } });
    let menu: (r: unknown) => void = () => undefined;
    Object.assign(window, { lockwire: { startFromUrdf: vi.fn().mockResolvedValue(reply("/w/robot.wbt", "/w")), onOpenedFromMenu: (cb: typeof menu) => { menu = cb; return () => undefined; },
      getContractPage: vi.fn().mockResolvedValue({ ...PAGE_WITH_SCENE, revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    act(() => root.render(<App />));
    const view = () => [!!container.querySelector(".shell-hint"), canvasProps.parts.length];
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Open robot")!.click());
    expect(view()).toEqual([false, 0]);
    await act(async () => menu({ result: { started: false, opened: reply("/u/robot.urdf", "/u").opened } }));
    expect(view()).toEqual([true, 1]); // File > Open Robot on an existing URDF lockfile: its parts and orbit hint are back
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
    expect(container.querySelector(".check-count")?.textContent).toMatch(/ · saved beside your model$/); // a line in the verdict card, not a box over the robot
  });

  it("missing meshes mount the folder fixer; Choose folder re-starts there and re-checks, and the panel goes", async () => {
    const opened = (path: string) => ({ opened: true, path, payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD });
    const startFromUrdf = vi.fn().mockResolvedValue({ started: true, urdf: "/r/urdf/a.urdf", opened: opened("/r/urdf/lockwire.yaml"),
      start: { path: "/r/urdf/lockwire.yaml", revision: "a".repeat(64), written: true, note: "4 of 4 mesh files not found (e.g. package://p/meshes/b.stl).", mesh_gap: { missing: 4, total: 4, example: "package://p/meshes/b.stl" } } });
    const choosePackageFolder = vi.fn().mockResolvedValue({ started: true, urdf: "/r/urdf/a.urdf", opened: opened("/r/lockwire.yaml"), removed: "/r/urdf/lockwire.yaml", start: { path: "/r/lockwire.yaml", revision: "b".repeat(64), written: true } });
    Object.assign(window, { lockwire: { startFromUrdf, choosePackageFolder, getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    act(() => root.render(<App />));
    await act(async () => clickOpenRobot());
    expect(container.querySelector(".check-verdict + .empty-geometry-panel strong")?.textContent).toBe("Meshes not found (4)"); // under the verdict, not over it
    expect(container.querySelector(".start-note")).toBeNull(); // saved beside: the verdict card says it; the fixer says the meshes
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Choose folder…")!.click());
    expect(choosePackageFolder).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".empty-geometry-panel")).toBeNull();
    expect(container.querySelector(".start-note")?.textContent).toBe("Saved lockwire.yaml in the robot's package folder, /r.");
    expect(readRecents().map((r) => r.path).filter((p) => p.startsWith("/r/"))).toEqual(["/r/lockwire.yaml"]); // moved: Recent lists the robot once
  });

  it.each([
    ["Start from blank", null, "Not saved yet. Save it to keep your work.", "Save…"],
    ["Rover ·", "rover", "This is an example. Save a copy to keep your changes.", "Save a copy…"],
  ])("%s opens from the app's folder with its own notice, and its button asks where to put an editable copy", async (button, starter, notice, label) => {
    const newRobot = vi.fn().mockResolvedValue({ started: false, example: `/ud/examples/${starter ?? "blank"}`, opened: { opened: true, path: "/ud/examples/x/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD } });
    Object.assign(window, { lockwire: { newRobot, getSidecarState: vi.fn().mockResolvedValue({ phase: "ready" }), onSidecarState: vi.fn().mockReturnValue(() => undefined), getStarters: vi.fn().mockResolvedValue([{ name: "rover", title: "Rover", based_on: "Rover" }]), getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>(".open-face-new button")].find((b) => b.textContent!.startsWith(button))!.click());
    expect(container.querySelector(".start-note")?.textContent).toBe(notice + label);
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === label)!.click());
    expect(newRobot.mock.calls).toEqual([[starter, undefined], [starter, true]]);
  });

  it("with no robot open there is one Agent entry; it opens a blank robot and the Agent tab", async () => {
    const newRobot = vi.fn().mockResolvedValue({ started: false, example: "/ud/examples/blank", opened: { opened: true, path: "/ud/examples/x/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD } });
    Object.assign(window, { lockwire: { newRobot, getSidecarState: vi.fn().mockResolvedValue({ phase: "ready" }), onSidecarState: vi.fn().mockReturnValue(() => undefined), getStarters: vi.fn().mockResolvedValue([{ name: "rover", title: "Rover", based_on: "Rover" }]), getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]), onAssistantEvent: () => () => {}, onAgentEvent: () => () => {}, getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: false, model: "", enabledRobots: [] }) } });
    root = createRoot(container);
    await act(async () => root.render(<App />));
    const entries = [...container.querySelectorAll("button")].filter((b) => /Agent/.test(b.textContent!));
    expect(entries.map((b) => b.textContent)).toEqual(["Ask the Agent to design one"]);
    await act(async () => entries[0]!.click());
    expect(newRobot.mock.calls).toEqual([[null, undefined]]);
    expect(container.querySelector("#side-drawer [role=tab][aria-selected=true]")!.textContent).toBe("Agent");
  });

  it("when the blank robot fails to open, the Agent entry does not leave the drawer set to pop open on the next robot", async () => {
    const newRobot = vi.fn().mockRejectedValueOnce(new Error("could not open")).mockResolvedValue({ started: false, example: "/ud/examples/rover", opened: { opened: true, path: "/ud/examples/x/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD } });
    Object.assign(window, { lockwire: { newRobot, getSidecarState: vi.fn().mockResolvedValue({ phase: "ready" }), onSidecarState: vi.fn().mockReturnValue(() => undefined), getStarters: vi.fn().mockResolvedValue([{ name: "rover", title: "Rover", based_on: "Rover" }]), getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) }), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]), onAssistantEvent: () => () => {}, onAgentEvent: () => () => {}, getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: false, model: "", enabledRobots: [] }) } });
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Ask the Agent to design one")!.click());
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>(".open-face-new button")].find((b) => b.textContent!.startsWith("Rover ·"))!.click());
    expect(newRobot).toHaveBeenCalledTimes(2);
    expect(container.querySelector("#side-drawer")!.hasAttribute("hidden")).toBe(true);
  });

  it("an example on the start screen opens in one tap and does not open the Agent", async () => {
    const newRobot = vi.fn().mockResolvedValue({ started: false, example: "/ud/examples/rover", opened: { opened: true, path: "/ud/examples/x/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD } });
    Object.assign(window, { lockwire: { newRobot, getSidecarState: vi.fn().mockResolvedValue({ phase: "ready" }), onSidecarState: vi.fn().mockReturnValue(() => undefined), getStarters: vi.fn().mockResolvedValue([{ name: "rover", title: "Rover", based_on: "Rover" }]), getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64) }),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>(".open-face-new button")].find((b) => b.textContent!.startsWith("Rover ·"))!.click());
    expect(newRobot.mock.calls).toEqual([["rover", undefined]]);
    expect(container.querySelector("#side-drawer")!.hasAttribute("hidden")).toBe(true);
  });

  it("a drifted robot opened from Recent offers Update from model file, re-locking the lockfile's own model", async () => {
    localStorage.setItem("lockwire.recent", JSON.stringify([{ path: "/r/lockwire.yaml", name: "Arm" }]));
    const startFromUrdf = vi.fn().mockResolvedValue({ started: false });
    Object.assign(window, { lockwire: { startFromUrdf, getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
      openLockfile: vi.fn().mockResolvedValue({ opened: true, path: "/r/lockwire.yaml", model: "/r/arm.urdf", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }),
      getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64), model_changed: true }) } });
    act(() => (root = createRoot(container)).render(<App />));
    await act(async () => container.querySelector<HTMLButtonElement>(".open-face-recents button")!.click());
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Update from model file")!.click());
    expect(startFromUrdf).toHaveBeenLastCalledWith({ urdf: "/r/arm.urdf", expected_revision: "a".repeat(64) });
  });

  it("opening a robot whose lockfile names no re-lockable model drops the last robot's Update from model file", async () => {
    localStorage.setItem("lockwire.recent", JSON.stringify([{ path: "/r/lockwire.yaml", name: "Arm" }]));
    const startFromUrdf = vi.fn().mockResolvedValue({ started: false });
    Object.assign(window, { lockwire: { startFromUrdf, getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
      openLockfile: vi.fn().mockResolvedValue({ opened: true, path: "/r/lockwire.yaml", model: "/r/arm.urdf", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }),
      getContractPage: vi.fn().mockResolvedValue({ ...PAGE, revision: "a".repeat(64), model_changed: true }) } });
    act(() => (root = createRoot(container)).render(<App />));
    await act(async () => container.querySelector<HTMLButtonElement>(".open-face-recents button")!.click());
    const update = () => [...container.querySelectorAll("button")].find(b => b.textContent === "Update from model file");
    expect(update()).toBeDefined();
    Object.assign(window.lockwire, { openLockfile: vi.fn().mockResolvedValue({ opened: true, path: "/b/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }) });
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    expect(update()).toBeUndefined();
    expect(startFromUrdf).not.toHaveBeenCalled();
  });

  it("a recent whose lockfile is gone leaves the list with one line, not an error and a Retry", async () => {
    localStorage.setItem("lockwire.recent", JSON.stringify([{ path: "/gone/lockwire.yaml", name: "Gone arm" }]));
    const openLockfile = vi.fn().mockResolvedValue({ opened: true, path: "/gone/lockwire.yaml", error: "no contract at /gone/lockwire.yaml — run `lockwire start <urdf>` first" });
    Object.assign(window, { lockwire: { openLockfile } });
    root = createRoot(container);
    act(() => root.render(<App />));
    await act(async () => container.querySelector<HTMLButtonElement>(".open-face-recents button")!.click());
    expect(container.querySelector(".start-note")?.textContent).toBe("Gone arm is no longer there, so it was removed from Recent.");
    expect(container.querySelector(".open-face-error")).toBeNull();
    expect(readRecents()).toEqual([]);
    expect(container.textContent).toContain("No recent robots");
  });

  it("a slow re-check does not wipe the refusal from an overwrite clicked meanwhile", async () => {
    const page = deferred<PagePayload>();
    const opened = { opened: true, path: "/tmp/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD };
    const startFromUrdf = vi.fn().mockResolvedValueOnce({
      started: true, urdf: "/tmp/robot.urdf", start: { path: "/tmp/lockwire.yaml", revision: "a".repeat(64), written: false },
      opened: { opened: true, path: "/tmp/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD },
    }).mockRejectedValue(new Error("lockfile changed since it was opened; reload before overwriting"));
    Object.assign(window, { lockwire: { startFromUrdf, openLockfile: vi.fn().mockResolvedValue(opened), getContractPage: vi.fn().mockResolvedValueOnce({ ...PAGE, revision: "a".repeat(64), model_changed: true }).mockReturnValue(page.promise),
      getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
    root = createRoot(container);
    act(() => root.render(<App />));
    const click = (label: string) => [...container.querySelectorAll("button")].find(b => b.textContent === label)!.click();
    await act(async () => click("Open robot"));
    await act(async () => click("Re-check"));
    await act(async () => click("Update from model file"));
    await act(async () => page.resolve({ ...PAGE, revision: "a".repeat(64), model_changed: true }));
    expect(container.querySelector(".open-face-error")?.textContent).toContain("changed since");
  });

  it("catches initial open rejection and retries the opening operation", async () => {
    const startFromUrdf = vi.fn().mockRejectedValueOnce(new Error("the sidecar stopped")).mockResolvedValue({ started: false });
    Object.assign(window, { lockwire: { startFromUrdf } });
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
    expect(container.querySelector("[role=alert]")?.textContent).toContain("the sidecar stopped");
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Retry opening")!.click());
    expect(startFromUrdf).toHaveBeenCalledTimes(2);
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("a file the engine refuses offers opening a different robot, not a retry", async () => {
    const startFromUrdf = vi.fn().mockResolvedValue({ started: false, opened: { opened: true, path: "/x/bad.yaml", error: "not a lockfile" } });
    Object.assign(window, { lockwire: { startFromUrdf } });
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
    const labels = [...container.querySelectorAll("[role=alert] button")].map((b) => b.textContent);
    expect(labels).toEqual(["Open a different robot"]);
  });

  it("a bad-YAML open shows one plain line, the raw text folded, and no Retry", async () => {
    const startFromUrdf = vi.fn().mockRejectedValue(new Error("mismatched tag: line 1, column 60"));
    Object.assign(window, { lockwire: { startFromUrdf } });
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
    const alert = container.querySelector("[role=alert]")!;
    expect(alert.textContent).toContain("This file could not be opened as a robot model. Choose a different file.");
    expect(alert.querySelector("details")?.textContent).toContain("mismatched tag");
    expect([...alert.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Open a different robot"]);
  });

  it("a failed document refresh says so in one line and keeps Retry opening", () => {
    const f = explainOpenError("Opened the check, but document refresh failed: lockfile revision missing");
    expect(f.line).toContain("Open the file again to try once more.");
    expect(f.retry).toBe(true);
    expect(f.raw).toContain("lockfile revision missing");
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
    expect(headline!.textContent).toMatch(/^VerdictNO-GO\d+ (?:faults?|warnings?)( · \d+ not scored)?$/);
  });
});

describe("App owns the mode tab router", () => {
  async function openToCheckFace(page: PagePayload = PAGE, form: unknown = DECLARE_FORM, sibling: PlanPayload | Error | null = null) {
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
      openLockfile: vi.fn().mockResolvedValue({ opened: true, path: "/tmp/x500/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }),
      getContractPage,
      getDeclareForm: vi.fn().mockResolvedValue(form),
      getHistory: vi.fn().mockResolvedValue(HISTORY),
      diffContract: vi.fn().mockResolvedValue(DIFF),
      writeContract,
      compareContractWhatIf,
      undoContract,
      choosePlan: vi.fn().mockResolvedValue("/tmp/plan.yaml"),
      checkPlan: vi.fn().mockImplementation(async (r: { plan: string }) => { if (r.plan === "" && sibling instanceof Error) throw sibling; return r.plan === "" ? sibling : PLAN; }),
    };
    root = createRoot(container);
    act(() => root.render(<App />));
    clickOpenRobot();
    await act(async () => {});
    return { getContractPage, undoContract, writeContract, compareContractWhatIf };
  }

  function clickTab(name: string) {
    goMode(container, name);
  }

  it("the tab bar is Design, Check, Plan; Declare and Docs live in More", async () => {
    const tabs = () => [...container.querySelectorAll('.shell-tabs [role="tab"]')].map((t) => t.textContent);
    await openToCheckFace();
    expect(tabs()).toEqual(["Design", "Check", "Plan", ...(pro?.modes.map((m) => m.label) ?? [])]); // pro/ tabs only when that folder is present
    expect(container.querySelector(".shell-more")?.textContent).toContain("More");
    goMode(container, "Docs");
    expect(container.querySelector(".shell-more")?.textContent).toContain("Docs");
  });

  it("test_check_mode_renders_frame_with_check_face", async () => {
    await openToCheckFace();
    expect(container.querySelector(".shell")).not.toBeNull();
    expect(container.querySelector(".check-headline")).not.toBeNull();
    const cards = Array.from(container.querySelectorAll<HTMLElement>(".finding-card"));
    const firstCard = cards[0];
    // The first problem lands already open (no tap).
    expect(firstCard?.dataset.shape).toBe(PAGE.cards[0]?.shape);
    expect(firstCard?.querySelector(".finding-info")?.getAttribute("title")).toBe(PAGE.cards[0]?.figure_note); // number-note: a tooltip, not body text
    expect(firstCard?.querySelector(".finding-fix")?.textContent).toBe(PAGE.cards[0]?.fix);
    expect(cards.map((card) => card.dataset.shape)).toEqual(PAGE.cards.map((card) => card.shape).filter((_, i) => i !== 1)); // the second budget card is the first rail's other mode: one row
  });

  it("test_page_faces_route from the generated page document", async () => {
    await openToCheckFace();
    clickTab("Design");
    expect(container.querySelector(".design-face-empty")?.textContent).toBe("Add a part to start.");
    clickTab("Plan");
    expect(container.querySelector(".plan-input")).not.toBeNull();
    clickTab("Docs");
    expect(container.querySelector(".reference-face")).not.toBeNull();
  });

  it("a drop that lands on nothing, or with no witness name, says so instead of doing nothing", async () => {
    await openToCheckFace(PAGE_WITH_SCENE);
    act(() => canvasProps.onDropPart!("dev.x", null));
    expect(container.querySelector(".open-face-error")?.textContent).toContain("Drop on the robot's body");
  });

  it("a drop with no saved name sends the place request at once, with no name prompt", async () => {
    const { writeContract } = await openToCheckFace({ ...PAGE_WITH_SCENE, links: [{ name: "base", position: [0, 0, 0] }] }, { ...DECLARE_FORM, witness: { prefill: "" } });
    const hit = { id: "base", point: [0, 0, 0] as [number, number, number] };
    await act(async () => (canvasProps.onDropPart as (d: string, h: unknown) => void)("dev.x", hit));
    expect(container.querySelector(".name-prompt")).toBeNull();
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({ command: "place", args: expect.objectContaining({ target: "dev.x", witness: "" }) }));
  });

  it("a written drop re-checks the robot it was placed on", async () => {
    await openToCheckFace({ ...PAGE_WITH_SCENE, links: [{ name: "base", position: [0, 0, 0] }] });
    const { openLockfile } = window.lockwire as unknown as { openLockfile: ReturnType<typeof vi.fn> };
    await act(async () => (canvasProps.onDropPart as (d: string, h: unknown) => void)("dev.x", { id: "base", point: [0, 0, 0] }));
    expect(openLockfile).toHaveBeenCalledWith({ contract: "/tmp/x500/lockwire.yaml" }); // writePlace's reload
  });

  it("a drawn shape re-checks once every step is written", async () => {
    const { writeContract } = await openToCheckFace(PAGE_WITH_SCENE);
    const { openLockfile } = window.lockwire as unknown as { openLockfile: ReturnType<typeof vi.fn> };
    clickTab("Design");
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Add shape")!.click());
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    for (const [label, v] of [["shape name", "rail"], ["length", "100"], ["width", "20"], ["height", "10"]]) {
      const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
      act(() => { set.call(input, v); input.dispatchEvent(new Event("input", { bubbles: true })); });
    }
    await act(async () => [...container.querySelectorAll(".add-part button")].find((b) => b.textContent === "Add shape")!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({ command: "add", args: expect.objectContaining({ kind: "link", id: "rail" }) }));
    expect(openLockfile).toHaveBeenCalledWith({ contract: "/tmp/x500/lockwire.yaml" }); // addShape's final reload
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
    await openToCheckFace(PAGE, DECLARE_FORM, PLAN);
    clickTab("Plan");
    await act(async () => {});
    expect(container.querySelector(".verdict-word")?.textContent).toBe("Plan passes");
    expect(container.querySelector(".kit-filepick .kit-cli-text")?.textContent).toBe("lockwire plan");
  });

  it("a sibling plan that cannot be read says so in one line", async () => {
    await openToCheckFace(PAGE, DECLARE_FORM, new Error("bad plan file"));
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
    expect(container.querySelector(".verdict-word")?.textContent).toBe("Plan passes");
    clickTab("Declare");
    clickTab("Plan");
    expect(container.querySelector(".verdict-word")?.textContent).toBe("Plan passes");
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
    clickTab("Plan"); await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click()); // A's plan is still being checked
    clickTab("Check"); // robot B is not interactive until its page lands, so the plan was picked on A first
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click()); // opens are ordered by start; the second begins once the first has landed
    await act(async () => second.resolve({ opened: true, path: "/tmp/b.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }));
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
        formal: true,
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
    set("part checked by", "Maya");
    const add = [...container.querySelectorAll("button")].find((b) => b.textContent === "Add part")!;
    await act(async () => add.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "add", args: expect.objectContaining({ id: "dev.servo", fields: expect.objectContaining({ power_rail: "rail.5v", power_draw: "7 W" }) }),
    }));
    expect(openLockfile).toHaveBeenCalledWith({ contract: "/tmp/x500/lockwire.yaml" });
    expect(getContractPage).toHaveBeenCalled(); // the refreshed page/check arrived without pressing recheck
  });

  describe("Add part from Design", () => {
    const catalog = { rails: ["rail.5v"], parts: [{ id: "cat.servo", name: "Servo", fits: ["rail.5v"],
      numbers: [{ key: "draw_max", value: 7, unit: "W", kind: "datasheet" as const, source: "datasheet p1" }] }] };
    const button = (text: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === text)!;
    const click = (text: string) => act(() => button(text).click());
    const hasServo = (p: any) => ({ ...p, faces: { ...p.faces, reference: { ...p.faces.reference, devices: [{ id: "dev.servo", name: "Servo", text: "", mount_mm: [1, 2, 3] }] } } });
    const bare = hasServo(PAGE); // the servo is declared; no 3D scene at all
    const withServo = (hash: string) => ({ ...hasServo(PAGE_WITH_SCENE), scene: { ...(PAGE_WITH_SCENE.scene as object), hash } }); // a scene with no body for it
    const field = (label: string) => container.querySelector<HTMLInputElement & HTMLSelectElement>(`:is(input, select)[aria-label="${label}"]`);
    async function openDesign(page: PagePayload = PAGE, witness = "Maya", cat = catalog) {
      window.localStorage.clear();
      const opened = await openToCheckFace(page, { ...DECLARE_FORM, witness: { prefill: witness }, add_part: cat });
      Object.assign(window.lockwire, { openLockfile: vi.fn().mockResolvedValue({ opened: true, path: "/tmp/x500/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }) });
      clickTab("Design");
      return opened;
    }
    const chooseServo = () => act(() => { field("catalog part")!.value = "cat.servo"; field("catalog part")!.dispatchEvent(new Event("change", { bubbles: true })); });
    const addServo = async () => { click("Add part"); chooseServo(); await act(async () => button("Add part").click()); };
    const type = (label: string, v: string) => act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field(label), v);
      field(label)!.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const submitForm = () => act(async () => container.querySelector("form.design-controls")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    describe("attaching a component (CAD-05)", () => {
      const withLink = { ...PAGE_WITH_SCENE, links: [{ name: "base", position: [0, 0, 0] }] };
      const attachServo = async (opened: Awaited<ReturnType<typeof openDesign>>) => {
        click("Add part"); chooseServo();
        act(() => { field("structure attach to")!.value = "base"; field("structure attach to")!.dispatchEvent(new Event("change", { bubbles: true })); });
        type("position x", "40"); type("position z", "25");
        await act(async () => button("Add part").click());
        return opened.writeContract.mock.calls.map((c) => c[0] as { command: string; args: Record<string, any> });
      };
      it("writes the device with its rail and draw, then the attach on the link in metres", async () => {
        const opened = await openDesign(withLink as PagePayload);
        const calls = await attachServo(opened);
        const adds = calls.filter((c) => c.command === "add");
        expect(adds.map((c) => c.args.kind)).toEqual(["device", "attach"]);
        expect(adds[0]!.args.fields).toMatchObject({ power_rail: "rail.5v", power_draw: "7 W" });
        expect(adds[1]!.args.fields).toMatchObject({ part: "cat.servo", parent: "base", frame: { xyz: [0.04, 0, 0.025] } });
        expect(calls.some((c) => c.command === "undo-edit")).toBe(false);
      });
      it("a refused attach undoes the device add", async () => {
        const opened = await openDesign(withLink as PagePayload);
        opened.writeContract.mockImplementation(async (r: { command: string; args: { kind?: string } }) =>
          r.command === "add" && r.args.kind === "attach" ? { payload: { written: false }, refusal: "No link base" } : r.command === "undo-edit" ? { payload: { undone: true }, refusal: null } : { payload: { written: true, revision: "r2" }, refusal: null });
        const calls = await attachServo(opened);
        expect(calls.filter((c) => c.command === "undo-edit")).toHaveLength(1);
        expect(container.textContent).toContain("No link base Nothing was added.");
      });
    });

    const moveServo = async () => {
      act(() => [...container.querySelectorAll<HTMLElement>(".kit-tree-row")].find((r) => r.textContent?.includes("Servo"))!.click());
      type("Move by X", "10");
      await submitForm();
    };
    const moves = (w: { mock: { calls: unknown[][] } }) => w.mock.calls.map(([r]) => r as { command: string; args: object }).filter((r) => r.command === "move");
    const refuseMoves = (w: { mockImplementation: (f: (r: { command: string }) => unknown) => void }) => w.mockImplementation(async ({ command }) => ({ payload: command === "move" ? null : { written: true }, refusal: command === "move" ? "Too far." : null }));
    it.each([["saved name signs it", "Maya"], ["no saved name: sent at once, no name, no prompt", ""]])("move, %s", async (_n, name) => {
      replies.length = 0;
      const { writeContract } = await openDesign(withServo("h1"), name);
      refuseMoves(writeContract);
      await moveServo();
      expect([field("Checked by"), field("Source"), field("Your name")]).toEqual([null, null, null]);
      expect(moves(writeContract).map((m) => m.args)).toEqual([expect.objectContaining({ offset: [11, 2, 3], witness: name, source: PLACED_SOURCE })]);
      expect(await Promise.race([replies[0], "unresolved"])).toEqual(expect.objectContaining({ refusal: "Too far." }));
      expect(container.textContent).toContain("Too far.");
    });
    it("the drawer lists a panel only when the robot has data for it, and no panel has a top-bar button of its own", async () => {
      const lens = (nodes: object[]) => ({ nodes, edges: [], label: "", caption: "" });
      const withParts = { ...PAGE, graph: { off: lens([]), mechanical: lens([{ id: "dev.servo", label: "dev.servo", sub: "", kind: "device" }]), power: lens([]), data: lens([]), health: lens([]) } } as unknown as PagePayload;
      const withJoint = { ...PAGE, controls_joints: [{ joint: "q", label: "Q", facts: [], t_s: [0, 1], position: [0, 1], advice: [] }] } as unknown as PagePayload;
      const wired = { ...PAGE, wiring_devices: [{ id: "d", name: "D", connectors: [{ name: "J", pins: [{ key: "d|J|1", label: "1" }] }] }, { id: "e", name: "E", connectors: [{ name: "K", pins: [{ key: "e||2", label: "2" }] }] }], wiring_wires: [{ id: "wire.a", name: "A", from_pin: "d|J|1", to_pin: "e||2", rail: null, rail_label: null, net: null }] } as unknown as PagePayload;
      const drawerTabs = () => [...container.querySelectorAll('#side-drawer [role="tab"]')].map((t) => t.textContent);
      for (const [page, listed] of [[PAGE, undefined], [withParts, ["Parts"]], [withJoint, ["Controls"]], [wired, ["Wiring"]]] as const) {
        await openDesign(page);
        Object.assign(window.lockwire, { getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: false, model: "", enabledRobots: [] }), onAssistantEvent: () => () => {}, onAgentEvent: () => () => {} });
        if (!listed) expect(button("Panels")).toBeUndefined();
        else { act(() => button("Panels").click()); expect(drawerTabs()).toEqual(listed); }
        for (const name of ["Power", "Controls", "Wiring", "Parts"]) expect(container.querySelector(".kit-slot-topRight")!.textContent).not.toContain(name);
        act(() => root.unmount()); container.remove();
      }
    });
    it("with a data panel showing, one tap on Assistant shows the Assistant view", async () => {
      const lens = (nodes: object[]) => ({ nodes, edges: [], label: "", caption: "" });
      const withParts = { ...PAGE, graph: { off: lens([]), mechanical: lens([{ id: "dev.servo", label: "dev.servo", sub: "", kind: "device" }]), power: lens([]), data: lens([]), health: lens([]) } } as unknown as PagePayload;
      await openDesign(withParts);
      Object.assign(window.lockwire, { getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: false, model: "", enabledRobots: [] }), onAssistantEvent: () => () => {}, onAgentEvent: () => () => {} });
      act(() => button("Panels").click());
      const tabs = () => [...container.querySelectorAll('#side-drawer [role="tab"]')].map((t) => t.textContent);
      expect(tabs()).toEqual(["Parts"]);
      act(() => button("Assistant").click());
      expect(tabs()).toEqual(["Assistant", "Agent", "Terminal"]);
    });
    it("two pin clicks in the Wiring panel write one wire, from then to, and reload the page", async () => {
      const bench = { ...PAGE, wiring_wires: [], wiring_devices: [{ id: "d", name: "D", connectors: [{ name: "J", pins: [{ key: "d|J|1", label: "1" }] }] }, { id: "e", name: "E", connectors: [{ name: "K", pins: [{ key: "e|K|2", label: "2" }] }] }] } as unknown as PagePayload;
      const { writeContract, getContractPage } = await openDesign(bench);
      Object.assign(window.lockwire, { getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: false, model: "", enabledRobots: [] }), onAssistantEvent: () => () => {}, onAgentEvent: () => () => {} });
      act(() => button("Panels").click());
      act(() => [...container.querySelectorAll<HTMLElement>('#side-drawer [role="tab"]')].find((t) => t.textContent === "Wiring")!.click());
      const reads = getContractPage.mock.calls.length;
      const tap = (key: string) => act(async () => { container.querySelector(`[data-pin="${key}"]`)!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
      await tap("d|J|1"); await tap("e|K|2");
      const adds = writeContract.mock.calls.filter(([r]) => r.command === "add");
      expect(adds).toHaveLength(1);
      expect(adds[0]![0].args).toEqual(expect.objectContaining({ kind: "wire", id: "wire.d-e", witness: "Maya", fields: expect.objectContaining({ from: { device: "d", connector: "J", pin: 1 }, to: { device: "e", connector: "K", pin: 2 } }) }));
      expect(getContractPage.mock.calls.length).toBeGreaterThan(reads);
    });
    it("with no saved name, Save efficiency in the Power panel declares with no witness and no formal flag", async () => {
      const stage = { rail: "rail.a", label: "Rail A", depth: 0, fed_by: "battery", volts_in: 14.8, volts_out: 5, amps_out: 6.42, efficiency: 0.8, watts_out: 32.1, watts_in: 40.125, watts_lost: 8.025, capacity_w: 15, unknown_loads: 0, loads: [] };
      const { writeContract } = await openDesign({ ...PAGE, power_stages: [stage] } as unknown as PagePayload, "");
      Object.assign(window.lockwire, { getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: false, model: "", enabledRobots: [] }), onAssistantEvent: () => () => {}, onAgentEvent: () => () => {} });
      act(() => button("Panels").click());
      act(() => [...container.querySelectorAll<HTMLElement>('#side-drawer [role="tab"]')].find((t) => t.textContent === "Power")!.click());
      type("efficiency for Rail A", "90");
      await act(async () => button("Save efficiency").click());
      const declares = writeContract.mock.calls.filter(([r]) => r.command === "declare");
      expect(declares).toHaveLength(1);
      expect(declares[0]![0].args).toEqual(expect.objectContaining({ path: "topology/rails/rail.a/efficiency", value: "0.9", witness: "" }));
      expect(declares[0]![0].args).not.toHaveProperty("formal"); // a quick edit, not the Declare form
    });
    it.each([["with a 3D scene", withServo("h1"), true], ["without one", bare, false]])("lists a device with no body under Not placed yet, %s", async (_n, page, frames) => {
      await openDesign(page as PagePayload);
      expect(container.textContent).toContain("Not placed yet");
      act(() => [...container.querySelectorAll<HTMLElement>(".kit-tree-row")].find((r) => r.textContent?.includes("Servo"))!.click());
      expect(!!field("Place on")).toBe(frames);
      expect(container.textContent?.includes("This robot has no 3D model yet, so there is nothing to place this on.")).toBe(!frames);
    });

    it.each([["with a 3D scene", withServo("after")], ["without one", bare]])("a successful add closes the form and selects the new part, %s", async (_n, after) => {
      const { writeContract, getContractPage } = await openDesign();
      getContractPage.mockResolvedValue(after);
      await addServo();
      expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({ args: expect.objectContaining({ id: "dev.servo", witness: "Maya" }) }));
      expect(field("catalog part")).toBeNull();
      expect(getSelectedPartId()).toBe("dev.servo");
    });

    describe("on a robot with no rail", () => {
      const blank = { rails: [], parts: [{ ...catalog.parts[0]!, fits: [] }] };
      const setIn = (label: string, v: string) => act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field(label), v); field(label)!.dispatchEvent(new Event("input", { bubbles: true })); });
      const addBlank = async (...replies: object[]) => {
        const { writeContract } = await openDesign({ ...PAGE, revision: "r1" } as PagePayload, "Maya", blank);
        replies.forEach((r) => writeContract.mockResolvedValueOnce(r));
        Object.assign(window.lockwire, { getDeclareForm: vi.fn().mockResolvedValue({ ...DECLARE_FORM, witness: { prefill: "Maya" }, add_part: { ...blank, rails: ["rail.main"] } }) });
        click("Add part"); chooseServo(); setIn("rail voltage", "12"); setIn("rail capacity", "20");
        await act(async () => button("Add part").click());
        return writeContract;
      };
      const arg = (w: ReturnType<typeof vi.fn>, n: number) => w.mock.calls[n]![0].args;

      it("writes the rail on the open revision, then the part on the revision that returned, and keeps the rail when the part is refused", async () => {
        const w = await addBlank({ payload: { written: true, revision: "r2" }, refusal: null }, { payload: null, refusal: "Rail is full." });
        expect(w).toHaveBeenCalledTimes(2);
        expect([arg(w, 0).kind, arg(w, 0).expected_revision, arg(w, 1).kind, arg(w, 1).expected_revision]).toEqual(["rail", "r1", "device", "r2"]);
        expect(container.textContent).toContain("Rail is full.");
        expect(window.lockwire.openLockfile).toHaveBeenCalled();
        expect(container.textContent).not.toContain("Power for this part");
        expect(field("rail")!.value).toBe("rail.main");
      });

      it("stops at the rail when the engine refuses it, and shows its line", async () => {
        const w = await addBlank({ payload: null, refusal: "Voltage is not a number." });
        expect(w).toHaveBeenCalledTimes(1);
        expect(container.textContent).toContain("Voltage is not a number.");
      });
    });

    it("with no saved name the add is sent at once, with no name prompt, and the engine's refusal shows in the form", async () => {
      const { writeContract } = await openDesign(PAGE, "");
      writeContract.mockImplementation(async ({ command }) => (command === "add" ? { payload: null, refusal: "Rail is full." } : { payload: { written: true }, refusal: null }));
      await addServo();
      expect(field("Your name")).toBeNull();
      expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({ command: "add", args: expect.objectContaining({ witness: "" }) }));
      expect(container.textContent).toContain("Rail is full.");
    });

    it("a save that moved an older file to format 0.7 shows the engine's line once", async () => {
      const LINE = "format moved to 0.7: this file now records an anonymous model";
      const { writeContract } = await openDesign(PAGE, "");
      writeContract.mockImplementation(async () => ({ payload: { written: true, lines: [LINE] }, refusal: null }));
      await addServo();
      expect(container.textContent?.split(LINE).length).toBe(2);
    });

    it("a pending select that found no part is dropped, so another robot's part with that id is not selected", async () => {
      await openDesign(PAGE_WITH_SCENE);
      await addServo();
      clickTab("Check");
      Object.assign(window.lockwire, { openLockfile: vi.fn().mockResolvedValue({ opened: true, path: "/tmp/b.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }), getContractPage: vi.fn().mockResolvedValue(withServo("b")) });
      await act(async () => button("Re-check").click());
      expect(getSelectedPartId()).toBeNull();
    });

    it("changing the selection with the form open keeps it open, entries intact", async () => {
      await openDesign(withServo("h1") as PagePayload);
      click("Add part");
      chooseServo();
      act(() => setSelectedPartId("dev.servo"));
      expect(field("catalog part")!.value).toBe("cat.servo");
    });

    it("Shift+A opens the form in place with no Checked by field, with the panel focused but not while typing", async () => {
      await openDesign();
      expect(container.textContent).toContain("No parts yet.");
      const press = () => act(() => { document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "A", shiftKey: true, bubbles: true })); });
      container.querySelector(".design-parts")!.appendChild(document.createElement("input")).focus();
      press();
      expect(field("catalog part")).toBeNull();
      button("Add part").focus();
      press();
      expect(field("catalog part")).not.toBeNull();
      expect(field("part checked by")).toBeNull();
    });
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
    for (const [label, next] of [["source", "caliper.csv"], ["checked by", "Maya"]]) {
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
        formal: true,
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
    expect(container.querySelector(".history-diff-result")?.textContent).toBe("No changes between these two versions. Pick another to compare.");
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
    act(() => [...container.querySelectorAll<HTMLLabelElement>('[aria-label="Operation"] label')].find((l) => l.textContent === "Mount")!.querySelector("input")!.click()); // a frame has no known position or turn, so Move and Rotate would refuse; Mount still forwards the write
    await act(async () => container.querySelector<HTMLFormElement>(".design-controls")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({
      command: "mount-shape",
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
  ])("drops the stale document when Undo applies but the reload %s", async (_case, reload, reason) => {
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
    expect(container.querySelector(".open-face-error")?.textContent).toContain(`document refresh failed: ${reason}`); // the same full reload every edit uses

    clickTab("Declare");
    expect(container.querySelector(".declare-face")).toBeNull(); // no stale revision is left to write with
    expect(writeContract).not.toHaveBeenCalled();

    clickTab("Design");
    expect(container.querySelector(".design-controls")).toBeNull();
  });
});
