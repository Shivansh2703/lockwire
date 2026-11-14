import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setJointPose, zeroPose } from "./scene/viewState";
import { setUnits } from "./units";
import { DesignControls, rotateBy, PLACED_SOURCE, TipReadout, type DesignEditReply, type DesignEditRequest } from "./DesignControls";
import type { CheckPayload, RobotJoint } from "../shared/api.gen";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
const payload = { robot: "after-edit", scene: { parts: [] } } as unknown as CheckPayload;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); setUnits({ length: "mm", angle: "deg" }); });

function render(reply: DesignEditReply = { written: true, refusal: null }, positionMm: [number, number, number] | null = [100, 200, 300], rpyRad: [number, number, number] | null = [0.1, 0.2, 0.3]) {
  const onEdit = vi.fn(async (_request: DesignEditRequest) => reply);
  const onReopen = vi.fn(async () => payload);
  const onPayload = vi.fn();
  act(() => { root.render(<DesignControls contract="robot.yaml" expectedRevision="revision" selectedPartId="dev.gripper" positionMm={positionMm} rpyRad={rpyRad} onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />); });
  return { onEdit, onReopen, onPayload };
}

function set(label: string, value: string) {
  const input = container.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;
  const prototype = input instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLSelectElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, value);
  act(() => { input.dispatchEvent(new Event("input", { bubbles: true })); input.dispatchEvent(new Event("change", { bubbles: true })); });
}
const OP: Record<string, string> = { "Mount shape": "Mount" };
function pick(label: string) {
  const group = label === "Place" || label === "Reparent" ? '[aria-label="More operations"]' : '[aria-label="Operation"]';
  if (group.includes("More") && !container.querySelector(group)) pick("More");
  const radio = [...container.querySelectorAll<HTMLLabelElement>(`${group} label`)].find((l) => l.textContent === (OP[label] ?? label))!.querySelector("input")!;
  act(() => radio.click());
}
const addSource = () => act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Add a source")!.click());
async function submit() { await act(async () => { container.querySelector<HTMLFormElement>("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); }); }

describe("DesignControls", () => {
  it("distinguishes a saved write from refresh failure and retries only the read", async () => {
    const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
    const onReopen = vi.fn().mockRejectedValueOnce(new Error("sidecar stopped")).mockResolvedValue(payload);
    const onPayload = vi.fn();
    act(() => root.render(<DesignControls contract="robot.yaml" expectedRevision="revision" selectedPartId="dev.gripper" positionMm={[100, 200, 300]} onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />));
    expect(container.querySelector("button[type=submit]")).toBeNull(); // nothing changed: no Apply
    addSource(); set("Source", "caliper");
    await submit();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Write succeeded, refresh failed");
    expect(container.querySelector<HTMLButtonElement>("button[type=submit]")?.disabled).toBe(true);
    await submit();
    expect(onEdit).toHaveBeenCalledTimes(1);
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Retry refresh")!.click());
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onReopen).toHaveBeenCalledTimes(2);
    expect(onPayload).toHaveBeenCalledWith(payload);
    expect(container.querySelector("[role=alert]")).toBeNull();
  });
  it("sends the selected part and real move arguments, then replaces the scene with the reopened payload", async () => {
    const { onEdit, onReopen, onPayload } = render();
    set("Move by X", "10"); set("Move by Y", "20"); set("Move by Z", "30"); addSource(); set("Source", "caliper");
    await submit();
    expect(onEdit).toHaveBeenCalledWith({ command: "move", args: { contract: "robot.yaml", expected_revision: "revision", target: "dev.gripper", offset: [110, 220, 330], unit: "mm", source: "caliper", witness: "" } });
    expect(onReopen).toHaveBeenCalledWith("robot.yaml");
    expect(onPayload).toHaveBeenCalledWith(payload);
  });

  it("typing only X keeps the part's y and z, and a blank form does not start at zero", async () => {
    const { onEdit } = render();
    expect([..."XYZ"].map((a) => container.querySelector<HTMLInputElement>(`[aria-label="Move by ${a}"]`)!.value)).toEqual(["0", "0", "0"]);
    set("Move by X", "10");
    await submit();
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ offset: [110, 200, 300], unit: "mm" });
  });

  it("a distance in metres is added to the position in millimetres", async () => {
    const { onEdit } = render();
    act(() => setUnits({ length: "m" }));
    set("Move by Z", "0.5");
    await submit();
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ offset: [100, 200, 800], unit: "mm" });
  });

  const snapOn = (step?: string) => {
    act(() => container.querySelector<HTMLInputElement>('[aria-label="Snap to grid"]')!.click());
    if (step) act(() => [...container.querySelectorAll<HTMLLabelElement>('[aria-label="Grid step"] label')].find((l) => l.textContent === step)!.querySelector("input")!.click());
  };
  const moved = async (x: string) => { set("Move by X", x); await submit(); };

  it("snap to grid is off by default and a 7.3 mm move moves 7.3 mm", async () => {
    const { onEdit } = render();
    expect(container.querySelector<HTMLInputElement>('[aria-label="Snap to grid"]')!.checked).toBe(false);
    await moved("7.3");
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ offset: [107.3, 200, 300] });
  });

  it("snap rounds to the nearest step: 7.3 mm at 5 moves 5, and 8 mm at 5 moves 10", async () => {
    const { onEdit } = render();
    snapOn("5 mm");
    await moved("7.3");
    await moved("8");
    expect(onEdit.mock.calls.map((c) => (c[0].args as { offset: number[] }).offset)).toEqual([[105, 200, 300], [110, 200, 300]]);
  });

  it("snap rounds a negative move to the nearest step too: -7.3 mm at 5 moves -5, and -8 moves -10", async () => {
    const { onEdit } = render();
    snapOn("5 mm");
    await moved("-7.3");
    await moved("-8");
    expect(onEdit.mock.calls.map((c) => (c[0].args as { offset: number[] }).offset)).toEqual([[95, 200, 300], [90, 200, 300]]);
  });

  it("snap also covers metres and the typed G move, in mm", async () => {
    const { onEdit } = render();
    snapOn("10 mm");
    act(() => setUnits({ length: "m" }));
    set("Move by Z", "0.0123");
    await submit();
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ offset: [100, 200, 310] });
    const key = async (k: string) => act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true })); });
    for (const k of ["g", "x", "0", ".", "0", "0", "8", "Enter"]) await key(k);
    expect(onEdit.mock.calls[1]![0].args).toMatchObject({ offset: [110, 200, 300] });
  });

  it("with metres chosen the grid steps read 0.001, 0.005 and 0.01 m, and snap 5 mm turns a typed 0.0073 into 5 mm", async () => {
    setUnits({ length: "m" });
    const { onEdit } = render();
    snapOn("0.005 m");
    expect([...container.querySelectorAll('[aria-label="Grid step"] label')].map((l) => l.textContent)).toEqual(["0.001 m", "0.005 m", "0.01 m"]);
    await moved("0.0073");
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ offset: [105, 200, 300], unit: "mm" });
  });

  it("typing only roll keeps the part's pitch and yaw, in radians", async () => {
    const { onEdit } = render();
    pick("Rotate");
    set("Rotate by roll", "90");
    await submit();
    const args = onEdit.mock.calls[0]![0].args as { rpy: number[]; unit: string };
    expect(args.unit).toBe("rad");
    expect(args.rpy[0]).toBeCloseTo(0.1 + Math.PI / 2, 8);
    expect(args.rpy.slice(1)).toEqual([0.2, 0.3]);
  });

  it("Rotate starts blank after Move was typed, and Apply clears the delta so a second Apply is not a repeat", async () => {
    const { onEdit } = render();
    set("Move by X", "50");
    pick("Rotate");
    expect(container.querySelector<HTMLInputElement>('[aria-label="Rotate by roll"]')!.value).toBe("0");
    pick("Move");
    expect(container.querySelector<HTMLInputElement>('[aria-label="Move by X"]')!.value).toBe("50");
    await submit();
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(container.querySelector<HTMLInputElement>('[aria-label="Move by X"]')!.value).toBe("0");
  });

  it("a refused Apply keeps the typed numbers", async () => {
    render({ written: false, refusal: "refused: nope" });
    set("Move by X", "50");
    await submit();
    expect(container.querySelector("[role=alert]")!.textContent).toContain("nope");
    expect(container.querySelector<HTMLInputElement>('[aria-label="Move by X"]')!.value).toBe("50");
  });

  it("Enter while the scene is still refreshing applies nothing and keeps the typed numbers", async () => {
    const { onEdit, onReopen } = render();
    onReopen.mockRejectedValueOnce(new Error("offline"));
    set("Move by X", "50");
    await submit();
    set("Move by X", "70");
    await submit();
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(container.querySelector<HTMLInputElement>('[aria-label="Move by X"]')!.value).toBe("70");
  });

  it("Enter while an apply is still in flight applies nothing and keeps the typed numbers", async () => {
    const { onEdit } = render();
    let finish!: (r: DesignEditReply) => void;
    onEdit.mockImplementationOnce(() => new Promise<DesignEditReply>((r) => { finish = r; }));
    set("Move by X", "50");
    await submit();
    set("Move by X", "70");
    await submit();
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(container.querySelector<HTMLInputElement>('[aria-label="Move by X"]')!.value).toBe("70");
    await act(async () => finish({ written: false, refusal: "refused: nope" }));
  });

  it("an unknown turn writes nothing and says so", async () => {
    const { onEdit } = render(undefined, [100, 200, 300], null);
    pick("Rotate");
    set("Rotate by roll", "10");
    await submit();
    expect(onEdit).not.toHaveBeenCalled();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("can't tell how this part is turned");
  });

  it("rotateBy composes about the part's own axes, also for pitch and near the pitch limit", () => {
    const cases: [number[], number[]][] = [[[0.3, -0.4, 1.2], [0.2, 0.5, -0.7]], [[0, 0, Math.PI / 2], [0, 0.4, 0]], [[0.3, Math.PI / 2, 0], [0.5, 0, 0]], [[0, 0, 0], [0.1, 0.2, 0.3]]];
    const turn = (v: number[]) => { const [r, p, y] = v as [number, number, number]; const c = Math.cos, s = Math.sin; return [c(y) * c(p), c(y) * s(p) * s(r) - s(y) * c(r), c(y) * s(p) * c(r) + s(y) * s(r), s(y) * c(p), s(y) * s(p) * s(r) + c(y) * c(r), s(y) * s(p) * c(r) - c(y) * s(r), -s(p), c(p) * s(r), c(p) * c(r)]; };
    const matmul = (a: number[], b: number[]) => [0, 1, 2].flatMap((i) => [0, 1, 2].map((j) => a[3 * i]! * b[j]! + a[3 * i + 1]! * b[3 + j]! + a[3 * i + 2]! * b[6 + j]!));
    for (const [cur, by] of cases) {
      const got = turn(rotateBy(cur as [number, number, number], by as [number, number, number]));
      matmul(turn(cur), turn(by)).forEach((v, i) => expect(got[i]).toBeCloseTo(v, 6));
    }
  });

  it("Apply shows only while a field differs from its start, and goes away when it is typed back", () => {
    render();
    set("Move by X", "10");
    expect(container.querySelector("button[type=submit]")).not.toBeNull();
    set("Move by X", "0");
    expect(container.querySelector("button[type=submit]")).toBeNull();
  });

  it("an unknown position writes nothing and says so", async () => {
    const { onEdit } = render(undefined, null);
    set("Move by X", "10");
    await submit();
    expect(onEdit).not.toHaveBeenCalled();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("can't tell where this part sits");
  });

  it("shows no Source or Checked by field by default and sends the constant source", async () => {
    const { onEdit } = render();
    set("Move by X", "10");
    expect(["Source", "checked by"].map((l) => container.querySelector(`[aria-label="${l}"]`))).toEqual([null, null]);
    await submit();
    expect(onEdit).toHaveBeenLastCalledWith(expect.objectContaining({ args: expect.objectContaining({ source: PLACED_SOURCE }) }));
  });

  it("leaves cycle and orphan decisions to the backend and shows its refusal", async () => {
    const { onEdit, onReopen } = render({ written: false, refusal: "refused: elbow would create a cycle" });
    pick("Reparent"); set("New parent", "elbow"); addSource(); set("Source", "layout.md");
    await submit();
    expect(onEdit).toHaveBeenCalledWith({ command: "reparent", args: { contract: "robot.yaml", expected_revision: "revision", target: "dev.gripper", to: "elbow", source: "layout.md", witness: "" } });
    expect(container.querySelector("[role=alert]")!.textContent).toContain("cycle");
    expect((container.querySelector('[aria-label="New parent"]') as HTMLInputElement).value).toBe("elbow");
    expect((container.querySelector('[aria-label="Source"]') as HTMLInputElement).value).toBe("layout.md");
    expect(onReopen).not.toHaveBeenCalled();
  });

  it("shows the backend refusal for an absolute custom mesh path", async () => {
    const { onEdit, onReopen } = render({ written: false, refusal: "custom-mesh mesh_path must be non-absolute" });
    pick("Mount shape"); set("Primitive", "custom-mesh"); set("Mesh path", "/tmp/bracket.stl");
    await submit();
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ command: "mount-shape" }));
    expect(container.querySelector("[role=alert]")!.textContent).toContain("non-absolute");
    expect(onReopen).not.toHaveBeenCalled();
  });

  it("reopens a successful write while showing a post-write readback diagnostic", async () => {
    const { onReopen, onPayload } = render({
      written: true,
      refusal: null,
      diagnostic: "written, but the contract hash could not be read back",
    });
    await submit();
    expect(onReopen).toHaveBeenCalledWith("robot.yaml");
    expect(onPayload).toHaveBeenCalledWith(payload);
    expect(container.querySelector("[role=status]")?.textContent).toContain("hash could not be read back");
    expect(container.querySelector("[role=alert]")).toBeNull();
  });
});

describe("TipReadout when posed", () => {
  it("says at zero pose, not the zero-joint wording, once a joint is posed", () => {
    const j = { name: "shoulder", type: "revolute", lower: -1, upper: 1, tip: { xyz: [0.4, 0, 0.05], per_unit: [0, 0, -0.4] } } as unknown as RobotJoint;
    act(() => root.render(<TipReadout joint={j} />));
    expect(container.textContent).not.toContain("at zero pose");
    act(() => setJointPose(j, 0.5));
    expect(container.textContent).toContain("at zero pose");
    act(() => zeroPose());
  });
});

describe("TipReadout", () => {
  const joint = (over: Partial<RobotJoint>) => ({ name: "shoulder", type: "revolute", tip: { xyz: [0.4, 0, 0.05], per_unit: [0, 0, -0.4] }, ...over }) as unknown as RobotJoint;
  const show = (j: RobotJoint) => { act(() => root.render(<TipReadout joint={j} />)); return container.textContent; };

  it("says where the tip is (far from the base) and what one degree of this joint does", () => {
    const text = show(joint({}));
    expect(text).toContain("Tool tip at x 400, y 0, z 50 mm, with every joint at zero");
    expect(text).toContain("Moving this joint 1 degree moves the tip by x 0, y 0, z -7 mm");
  });

  it("reads a sliding joint per millimetre and a joint that leaves the tip alone as such", () => {
    expect(show(joint({ type: "prismatic", tip: { xyz: [0, 0, 0.1], per_unit: [0, 0, 1] } }))).toContain("1 mm moves the tip by x 0, y 0, z 1 mm");
    expect(show(joint({ tip: { xyz: [0.4, 0, 0.05], per_unit: [0, 0, 0] } }))).toContain("does not move the tip");
  });

  it("shows nothing when the engine returned no tip", () => {
    expect(show(joint({ tip: null }))).toBe("");
  });
});
