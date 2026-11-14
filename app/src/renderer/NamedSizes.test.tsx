import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NamedSizes, sizeEditRequest } from "./NamedSizes";

let root: Root;
let container: HTMLDivElement;
afterEach(() => { if (root) act(() => root.unmount()); container?.remove(); root = undefined as never; });

const links = [{ name: "arm", position: [0, 0, 0], solids: [], mass_note: "mass 0.678 kg, derived from its shapes and material" }, { name: "fore", position: [0, 0, 0], solids: [], mass_note: null }] as never;
const mount = (onSave = vi.fn().mockResolvedValue({ written: true })) => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<NamedSizes params={[{ name: "arm_length", value: 220, unit: "mm" }]} links={links} onSave={onSave} />));
  return onSave;
};
function type(label: string, value: string) {
  const el = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
  act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value); el.dispatchEvent(new Event("input", { bubbles: true })); });
}
const save = async () => act(async () => { container.querySelector<HTMLButtonElement>('[aria-label="save arm_length"]')!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });

describe("NamedSizes", () => {
  it("asks the engine to declare params/arm_length/value in the size's own unit", () => {
    expect(sizeEditRequest("/r/lock.yaml", "rev1", "arm_length", 300, "Maya")).toEqual({
      command: "declare",
      args: { contract: "/r/lock.yaml", expected_revision: "rev1", path: "params/arm_length/value", value: 300, source: "changed in the app", witness: "Maya" },
    });
  });
  it("saves 220 -> 300 as the number 300, and refuses a size that is not a positive number", async () => {
    const onSave = mount();
    expect(container.querySelector<HTMLInputElement>('[aria-label="size arm_length"]')!.value).toBe("220");
    type("size arm_length", "tall");
    await save();
    expect(onSave).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Type a size greater than zero.");
    type("size arm_length", "300");
    await save();
    expect(onSave).toHaveBeenCalledWith("arm_length", 300);
  });
  it("lists a derived mass in the engine's words and nothing for a declared one", () => {
    mount();
    expect(container.textContent).toContain("arm: mass 0.678 kg, derived from its shapes and material");
    expect(container.textContent).not.toContain("fore:");
  });
});
