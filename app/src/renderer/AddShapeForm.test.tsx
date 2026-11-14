import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddShapeForm, geometryOf, newSizeOf, sizeOf } from "./AddShapeForm";

let root: Root;
let container: HTMLDivElement;
afterEach(() => { if (root) act(() => root.unmount()); container?.remove(); root = undefined as never; });

const field = (label: string) => container.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;
function type(label: string, value: string) {
  const el = field(label);
  const set = Object.getOwnPropertyDescriptor(el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, "value")!.set!;
  act(() => { set.call(el, value); el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true })); });
}
const mount = (onAdd = vi.fn().mockResolvedValue({ written: true }), links = ["base"]) => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<AddShapeForm witness="Maya" links={links} params={[{ name: "arm_length", value: 220, unit: "mm" }]} onAdd={onAdd} />));
  return onAdd;
};
const press = async () => act(async () => { Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Add shape")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });

describe("AddShapeForm", () => {
  it("reads a size as mm, or as the name of a named size, and nothing else", () => {
    expect(sizeOf("50", ["arm_length"])).toBe(0.05);
    expect(sizeOf(" arm_length ", ["arm_length"])).toBe("arm_length");
    expect([sizeOf("fifty", ["arm_length"]), sizeOf("0", []), sizeOf("-3", []), sizeOf("1e3", [])]).toEqual([null, null, null, null]);
  });
  it("turns each kind into the model's box, cylinder or sphere", () => {
    expect(geometryOf("plate", [0.1, 0.05, 0.003])).toEqual([{ kind: "box", size: [0.1, 0.05, 0.003] }]);
    expect(geometryOf("tube", [0.01, "arm_length"])).toEqual([{ kind: "cylinder", radius: 0.01, length: "arm_length" }]);
    expect(geometryOf("sphere", [0.02])).toEqual([{ kind: "sphere", radius: 0.02 }]);
  });
  it("sends a cylinder sized by a number and a name, attached to a link", async () => {
    const onAdd = mount();
    type("shape type", "cylinder");
    type("shape name", "upper_arm");
    type("radius", "20");
    type("length", "arm_length");
    type("attach to", "base");
    await press();
    expect(onAdd).toHaveBeenCalledWith({ name: "upper_arm", geometry: [{ kind: "cylinder", radius: 0.02, length: "arm_length" }], attachTo: "base", witness: "Maya", newSizes: [] });
  });
  it("makes a new named size from name=mm, once, and refuses a name that already exists", async () => {
    expect([newSizeOf(" reach = 150 "), newSizeOf("reach"), newSizeOf("reach=0"), newSizeOf("1=5")]).toEqual([{ name: "reach", mm: 150 }, null, null, null]);
    const onAdd = mount();
    type("shape type", "box");
    type("shape name", "rail");
    type("length", "reach=150");
    type("width", "20");
    type("height", "reach=150");
    await press();
    expect(onAdd).toHaveBeenCalledWith({ name: "rail", geometry: [{ kind: "box", size: ["reach", 0.02, "reach"] }], attachTo: "", witness: "Maya", newSizes: [{ name: "reach", mm: 150 }] });
    type("shape name", "rail2");
    type("length", "arm_length=300");
    await press();
    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("arm_length is already a named size");
  });
  it("sends nothing and says what is missing", async () => {
    const onAdd = mount(undefined, ["upper_arm"]);
    type("shape name", "upper_arm");
    await press();
    expect(onAdd).not.toHaveBeenCalled();
    expect(container.textContent).toContain("A link called upper_arm already exists.");
    expect(container.textContent).toContain("Type a size in mm or a named size.");
  });
});
