import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { Shell } from "../Shell";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("Shell with the pro folder", () => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as never;
  it("shows real Test and Run tabs before Docs and no Pro line", () => {
    const box = document.createElement("div");
    document.body.appendChild(box);
    const root = createRoot(box);
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected robotName="QArm" />));
    expect([...box.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).toEqual(["Design", "Declare", "Check", "Plan", "Test", "Run", "Docs"]);
    expect(box.querySelector(".shell-pro-line")).toBeNull();
    act(() => root.unmount());
    box.remove();
  });
});
