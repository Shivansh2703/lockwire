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
  it("shows only the planning tabs and one dim, non-clickable Pro line", () => {
    const box = document.createElement("div");
    document.body.appendChild(box);
    const root = createRoot(box);
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected robotName="QArm" />));
    expect([...box.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).toEqual(["Design", "Declare", "Check", "Plan", "Docs"]);
    const line = box.querySelectorAll(".shell-pro-line");
    expect(line).toHaveLength(1);
    expect(line[0]!.textContent).toBe(`Test / Run: ${PRODUCT_NAME} Pro`);
    expect(line[0]!.closest("button, a, [role='tab']")).toBeNull();
    act(() => root.unmount());
    box.remove();
  });
});
