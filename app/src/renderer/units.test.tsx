import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CheckPayload, RobotJoint } from "../shared/api.gen";
import { AddShapeForm, sizeOf } from "./AddShapeForm";
import { DesignControls, TipReadout, type DesignEditReply, type DesignEditRequest } from "./DesignControls";
import { NamedSizes } from "./NamedSizes";
import { JointSlider } from "./PoseControls";
import { ViewMenu } from "./ViewMenu";
import { setUnits } from "./units";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("./scene/savePicture", () => ({ savePicture: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => { container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); setUnits({ length: "mm", angle: "deg" }); });

const payload = { robot: "r", scene: { parts: [] } } as unknown as CheckPayload;
const arm = { name: "shoulder", child_name: "arm", type: "revolute", lower: -1, upper: 1, tip: { xyz: [0.4, 0, 0], per_unit: [0, 0, 0.4] } } as unknown as RobotJoint;
const setField = (label: string, value: string) => {
  const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
  act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
};
const submit = () => act(async () => { container.querySelector<HTMLFormElement>("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
const press = (name: string) => act(() => [...container.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === name)!.click());

/** The form, the View menu and the readouts in one tree, with a spy on every way an edit could leave. */
function mount(position: [number, number, number] = [0, 0, 0]) {
  const onEdit = vi.fn(async (_r: DesignEditRequest): Promise<DesignEditReply> => ({ written: true, refusal: null }));
  const onReopen = vi.fn(async () => payload);
  const onPayload = vi.fn();
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
  act(() => root.render(<><ViewMenu design name="Arm" /><TipReadout joint={arm} /><DesignControls contract="r.yaml" expectedRevision="rev" selectedPartId="dev" positionMm={position} rpyRad={[0.1, 0.2, 0.3]} onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} /></>));
  return { onEdit, onReopen, onPayload, fetchSpy };
}
const chooseUnit = (name: string) => { if (!container.querySelector("[role=menu]")) act(() => container.querySelector<HTMLButtonElement>("button[aria-haspopup]")!.click()); act(() => [...container.querySelectorAll<HTMLButtonElement>("[role=menuitemradio]")].find((b) => b.textContent === name)!.click()); };

describe("Units setting (display only)", () => {
  it("with metres chosen a 400 mm tip reads 0.4 m, and typing 0.4 sends 400 mm", async () => {
    const { onEdit } = mount();
    expect(container.querySelector(".tip-readout")!.textContent).toContain("Tool tip at x 400, y 0, z 0 mm");
    chooseUnit("m");
    expect(container.querySelector(".tip-readout")!.textContent).toContain("Tool tip at x 0.4, y 0, z 0 m");
    setField("Move by X", "0.4");
    await submit();
    expect(onEdit.mock.calls[0]![0]).toMatchObject({ command: "move", args: { offset: [400, 0, 0], unit: "mm" } });
  });

  it("switching units writes nothing, and the same distance sends the same request in mm or m", async () => {
    const { onEdit, onReopen, onPayload, fetchSpy } = mount([100, 0, 0]);
    chooseUnit("m"); chooseUnit("rad"); chooseUnit("mm"); chooseUnit("deg");
    expect([onEdit, onReopen, onPayload, fetchSpy].map((s) => s.mock.calls.length)).toEqual([0, 0, 0, 0]);
    setField("Move by X", "50");
    await submit();
    chooseUnit("m");
    setField("Move by X", "0.05");
    await submit();
    const [inMm, inM] = onEdit.mock.calls.map((c) => c[0]);
    expect(inMm).toEqual(inM);
    expect(inMm).toMatchObject({ args: { offset: [150, 0, 0], unit: "mm" } });
  });

  it("a rotate request is in radians whether the angle was typed in degrees or radians", async () => {
    const { onEdit } = mount();
    act(() => [...container.querySelectorAll<HTMLLabelElement>('[aria-label="Operation"] label')].find((l) => l.textContent === "Rotate")!.querySelector("input")!.click());
    setField("Rotate by roll", "90");
    await submit();
    chooseUnit("rad");
    setField("Rotate by roll", String(Math.PI / 2));
    await submit();
    const [deg, rad] = onEdit.mock.calls.map((c) => (c[0].args as { rpy: number[]; unit: string }));
    expect(deg!.unit).toBe("rad");
    expect(deg!.rpy[0]).toBeCloseTo(0.1 + Math.PI / 2, 6);
    expect(rad!.rpy).toEqual(deg!.rpy);
  });

  it("a typed G move in metres sends millimetres", async () => {
    const { onEdit } = mount([100, 0, 0]);
    chooseUnit("m");
    for (const key of ["g", "x", "0", ".", "4", "Enter"]) await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })); });
    expect(onEdit.mock.calls[0]![0]).toMatchObject({ command: "move", args: { offset: [500, 0, 0], unit: "mm" } });
  });

  it("the tip readout and pose slider show radians when radians are chosen", () => {
    setUnits({ angle: "rad" });
    act(() => root.render(<><TipReadout joint={arm} /><JointSlider joint={arm} /></>));
    expect(container.querySelector(".tip-readout")!.textContent).toContain("Moving this joint 1 radian moves the tip by x 0, y 0, z 400 mm");
    expect(container.querySelector(".pose-value")!.textContent).toBe("0 rad");
    expect(container.querySelector<HTMLInputElement>("input[type=range]")!.max).toBe("1");
  });

  it("a typed named size is saved in the size's own unit, and a typed shape size goes out in metres", async () => {
    setUnits({ length: "m" });
    const onSave = vi.fn().mockResolvedValue({ written: true });
    act(() => root.render(<NamedSizes params={[{ name: "arm_length", value: 220, unit: "mm" }]} links={[] as never} onSave={onSave} />));
    expect(container.querySelector<HTMLInputElement>('[aria-label="size arm_length"]')!.value).toBe("0.22");
    setField("size arm_length", "0.3");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="save arm_length"]')!.click());
    expect(onSave).toHaveBeenCalledWith("arm_length", 300);
    expect(sizeOf("0.4", [])).toBe(0.4);
    setUnits({ length: "mm" });
    expect(sizeOf("400", [])).toBe(0.4);
  });

  it("the shape form labels its sizes in the chosen unit", () => {
    setUnits({ length: "m" });
    act(() => root.render(<AddShapeForm witness="" links={[]} params={[]} onAdd={vi.fn()} />));
    expect(container.textContent).toContain("Length (m)");
  });
});
