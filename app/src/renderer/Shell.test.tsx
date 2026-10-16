import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { measureInset, Shell, useShellCommand } from "./Shell";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function Page() {
  useShellCommand("lockwire declare x");
  return <p>page</p>;
}

describe("Shell", () => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as never;
  it("shows the page's own command in the fixed bar, and hides the tabs until a robot is open", () => {
    const box = document.createElement("div");
    document.body.appendChild(box);
    const root = createRoot(box);
    act(() => root.render(<Shell mode="Declare" onModeChange={() => {}} connected cliLine="lockwire check y" robotName="QArm"><Page /></Shell>));
    expect(box.querySelector(".kit-slot-bottom .kit-cli-text")!.textContent).toBe("lockwire declare x");
    expect(box.querySelector(".kit-slot-bottom button[title='Fit view']")).not.toBeNull();
    expect(box.querySelector(".shell-chip")!.textContent).toBe("QArm");
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected locked />));
    expect(box.querySelector(".shell-chip")).toBeNull();
    expect(box.querySelector('[role="tab"]')).toBeNull(); // Open: no page tabs until a robot is open
    act(() => root.unmount());
    box.remove();
  });
});

describe("measureInset", () => {
  const rect = (x: number, y: number, w: number, h: number) => () => ({ left: x, top: y, right: x + w, bottom: y + h, width: w, height: h }) as DOMRect;
  const root = (slots: Record<string, ReturnType<typeof rect>>) => {
    const el = document.createElement("div");
    el.getBoundingClientRect = rect(0, 0, 1440, 900);
    for (const [n, r] of Object.entries(slots)) { const s = el.appendChild(document.createElement("div")); s.className = `kit-slot-${n}`; s.getBoundingClientRect = r; }
    return el;
  };
  it("clears a present side panel by its rect + 12px, else keeps a 24px margin", () => {
    expect(measureInset(root({}))).toMatchObject({ right: 24, left: 24 });
    expect(measureInset(root({ right: rect(0, 0, 0, 0), left: rect(0, 0, 0, 0) }))).toMatchObject({ right: 24, left: 24 });
    expect(measureInset(root({ right: rect(960, 0, 480, 900), left: rect(0, 0, 300, 900) }))).toMatchObject({ right: 1440 - 960 + 12, left: 300 + 12 });
  });
});
