import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { Shell, useShellCommand } from "./Shell";

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
