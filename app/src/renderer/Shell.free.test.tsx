import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

// The free app: no pro/ folder was found, so the registry is empty.
vi.mock("./proRegistry", () => ({ pro: null }));

const { Shell } = await import("./Shell");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("Shell without the pro folder", () => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as never;
  it("shows the planning tabs and no greyed Pro tabs", () => {
    const box = document.createElement("div");
    document.body.appendChild(box);
    const root = createRoot(box);
    const onModeChange = vi.fn();
    act(() => root.render(<Shell mode="Check" onModeChange={onModeChange} connected robotName="QArm" />));
    expect([...box.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).toEqual(["Design", "Check", "Plan"]);
    expect(box.textContent).not.toContain("Pro");
    expect(box.querySelector('[role="dialog"]')).toBeNull();
    const more = () => box.querySelector<HTMLButtonElement>(".shell-more")!;
    expect(more().textContent).toContain("More");
    act(() => more().click());
    expect([...box.querySelectorAll('[role="menuitem"]')].map((t) => t.textContent)).toEqual(["Declare", "Docs"]);
    act(() => box.querySelectorAll<HTMLElement>('[role="menuitem"]')[1]!.click());
    expect(onModeChange).toHaveBeenCalledWith("Reference");
    expect(box.querySelector('[role="menu"]')).toBeNull();
    act(() => root.render(<Shell mode="Declare" onModeChange={onModeChange} connected robotName="QArm" />));
    expect(more().textContent).toContain("Declare"); // the active screen is named on the button
    expect(more().hasAttribute("data-active")).toBe(true);
    const info = () => box.querySelector<HTMLButtonElement>(".shell-info")!;
    act(() => info().click());
    expect(box.querySelector('[role="note"]')?.textContent).toContain("a number it should check against");
    act(() => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(box.querySelector('[role="note"]')).toBeNull();
    act(() => root.render(<Shell mode="Check" onModeChange={onModeChange} connected robotName="QArm" />));
    act(() => info().click());
    act(() => { document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })); });
    expect(box.querySelector('[role="note"]')).toBeNull(); // a click away closes it
    act(() => root.unmount());
    box.remove();
  });
});
