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
    expect([...box.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).toEqual(["Design", "Declare", "Check", "Plan", "Docs"]);
    expect(box.textContent).not.toContain("Pro");
    expect(box.querySelector('[role="dialog"]')).toBeNull();
    act(() => root.render(<Shell mode="Declare" tabs={["Check", "Reference"]} onModeChange={onModeChange} connected robotName="QArm" />));
    const tabs = () => [...box.querySelectorAll('[role="tab"]')].map((t) => t.textContent);
    expect(tabs()).toEqual(["Declare", "Check", "Docs"]); // the open tab always shows
    act(() => box.querySelector<HTMLButtonElement>(".shell-more")!.click());
    expect(tabs()).toEqual(["Design", "Declare", "Check", "Plan", "Docs"]);
    expect(box.querySelector(".shell-more")).toBeNull();
    act(() => root.unmount());
    box.remove();
  });
});
