import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { PRODUCT_NAME } from "../shared/branding";

// The free app: no pro/ folder was found, so the registry is empty.
vi.mock("./proRegistry", () => ({ pro: null }));

const { Shell } = await import("./Shell");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("Shell without the pro folder", () => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as never;
  it("shows the planning tabs and greyed, inert Test and Run tabs with a Pro badge", () => {
    const box = document.createElement("div");
    document.body.appendChild(box);
    const root = createRoot(box);
    const onModeChange = vi.fn();
    act(() => root.render(<Shell mode="Check" onModeChange={onModeChange} connected robotName="QArm" />));
    expect([...box.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).toEqual(["Design", "Declare", "Check", "Plan", "Docs"]);
    const tabs = [...box.querySelectorAll<HTMLElement>(".shell-pro-tab")];
    expect(tabs.map((t) => t.textContent)).toEqual(["TestPro", "RunPro"]);
    for (const t of tabs) {
      expect(t.getAttribute("aria-disabled")).toBe("true");
      expect(t.title).toBe(`Test and Run are in ${PRODUCT_NAME} Pro`);
      expect(t.querySelector(".shell-pro-badge")!.textContent).toBe("Pro");
      expect(t.closest("button, a, [role='tab']")).toBeNull();
      expect(t.tabIndex).toBe(-1);
      act(() => t.click());
    }
    expect(onModeChange).not.toHaveBeenCalled();
    act(() => root.unmount());
    box.remove();
  });
});
