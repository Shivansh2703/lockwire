import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddPartForm } from "./AddPartForm";

let root: Root;
let container: HTMLDivElement;

afterEach(() => { act(() => root.unmount()); container.remove(); });

function type(label: string, value: string) {
  const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => { set.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
}

describe("AddPartForm", () => {
  it("submits name, draw, rail and witness with source defaulting to datasheet", async () => {
    const onAdd = vi.fn().mockResolvedValue({ written: true });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<AddPartForm witness="" onAdd={onAdd} />));
    const button = () => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Add part")!;
    expect(button().disabled).toBe(true);
    type("part name", "dev.servo5");
    type("power draw", "7");
    type("rail", "rail.servo-5v");
    type("part witness", "Maya");
    expect(button().disabled).toBe(false);
    await act(async () => { button().dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(onAdd).toHaveBeenCalledWith({ id: "dev.servo5", draw: "7", rail: "rail.servo-5v", source: "datasheet", witness: "Maya" });
    expect(container.textContent).toContain("Part added");
  });
});

describe("AddPartForm catalog picker", () => {
  const num = (key: string, value: number, unit: string, kind: "datasheet" | "assumed" = "datasheet") => ({ key, value, unit, kind, source: `src ${key}` });
  const catalog = {
    rails: ["rail.5v", "rail.12v"],
    parts: [
      { id: "cat.servo", name: "Servo", numbers: [num("draw_max", 7, "W", "assumed")], fits: ["rail.5v"] },
      { id: "cat.mystery", name: "Mystery", numbers: [num("draw_max", 3, "W")], fits: [] },
    ],
  };
  const pickPart = (value: string) => {
    const select = container.querySelector<HTMLSelectElement>('[aria-label="catalog part"]')!;
    act(() => { select.value = value; select.dispatchEvent(new Event("change", { bubbles: true })); });
  };
  const railValue = () => container.querySelector<HTMLSelectElement>('[aria-label="rail"]')!.value;

  it("preselects the rail whose voltage fits, badges assumed numbers, and never guesses", () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<AddPartForm witness="" catalog={catalog} onAdd={vi.fn()} />));
    pickPart("cat.servo");
    expect(railValue()).toBe("rail.5v"); // not rail.12v
    expect(container.textContent).toContain("Power draw 7 W (assumed) · src source"); // one plain line; the raw note sits under Sources
    expect(container.textContent).not.toContain("No rail matches");
    pickPart("cat.mystery");
    expect(railValue()).toBe("");
    expect(container.textContent).toContain("No rail matches this part's voltage — pick one");
  });
});
