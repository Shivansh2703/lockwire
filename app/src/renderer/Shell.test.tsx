import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { measureInset, measureSides, Shell, useShellCommand } from "./Shell";

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
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected notices={<p>n</p>} />));
    expect(box.querySelector<HTMLElement>(".shell-notices")!.style.maxWidth).toBe("calc(100% - 48px)"); // inside the uncovered canvas
    act(() => root.unmount());
    box.remove();
  });
  it("Hide panels leaves only the robot, theme, Connected and fit; one tap brings them back; the hint goes at the first touch", () => {
    const box = document.body.appendChild(document.createElement("div"));
    const root = createRoot(box);
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected robotName="QArm" assistant={{ open: false, onToggle: () => {} }} panels={{ open: false, onToggle: () => {} }} left={<p>parts</p>} notices={<p>note</p>}><p>panel</p></Shell>));
    const toggle = () => box.querySelector<HTMLButtonElement>(".kit-slot-bottomRight button")!;
    expect(box.querySelector(".shell-hint")?.textContent).toContain("pinch to zoom");
    act(() => box.querySelector(".shell-scene")!.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    expect(box.querySelector(".shell-hint")).toBeNull();
    act(() => toggle().click());
    expect(box.querySelector(".kit-slot-left, .kit-slot-right, .kit-slot-top, .shell-chip")).toBeNull();
    expect(box.querySelector(".shell-notices")?.textContent).toBe("note"); // errors and Restart never hide
    expect(box.querySelector(".kit-slot-topRight")?.textContent).not.toMatch(/Assistant|Panels/);
    expect(box.querySelector("button[title='Fit view']")).not.toBeNull();
    expect(box.textContent).toContain("Connected");
    expect(toggle().textContent).toBe("◐ Show panels");
    act(() => toggle().click());
    expect(box.querySelector(".kit-slot-right")?.textContent).toBe("panel");
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected center={<p>open</p>}><p>panel</p></Shell>));
    expect(box.querySelector(".kit-slot-bottomRight button")).toBeNull(); // nothing to hide on the open screen
    act(() => root.unmount());
    box.remove();
  });
});

describe("Shell top-right buttons", () => {
  it("the Assistant button greyed on the start screen says why", () => {
    const box = document.body.appendChild(document.createElement("div"));
    const root = createRoot(box);
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected assistant={{ open: false }} />));
    const b = [...box.querySelectorAll<HTMLButtonElement>(".kit-slot-topRight button")].find((x) => x.textContent === "Assistant")!;
    expect([b.disabled, b.title]).toEqual([true, "Open a robot first: the Assistant works on the robot you have open."]);
    act(() => root.unmount());
    box.remove();
  });

  it("Assistant is one tap from the top bar; Panels shows only when given, and each toggles its own view", () => {
    const box = document.body.appendChild(document.createElement("div"));
    const root = createRoot(box);
    const a = vi.fn(), p = vi.fn();
    const top = () => [...box.querySelectorAll<HTMLButtonElement>(".kit-slot-topRight button")].map((b) => b.textContent);
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected robotName="QArm" assistant={{ open: false, onToggle: a }} />));
    expect(top()).toContain("Assistant");
    expect(top()).not.toContain("Panels");
    act(() => root.render(<Shell mode="Check" onModeChange={() => {}} connected robotName="QArm" assistant={{ open: false, onToggle: a }} panels={{ open: false, onToggle: p }} />));
    const click = (t: string) => act(() => [...box.querySelectorAll<HTMLButtonElement>(".kit-slot-topRight button")].find((b) => b.textContent === t)!.click());
    click("Assistant");
    expect([a.mock.calls.length, p.mock.calls.length]).toEqual([1, 0]);
    click("Panels");
    expect([a.mock.calls.length, p.mock.calls.length]).toEqual([1, 1]);
    act(() => root.unmount());
    box.remove();
  });
});

describe("Shell placement", () => {
  it("notices and the hint sit by the slot column (measureSides), not the scene's free rect", () => {
    const orig = HTMLElement.prototype.getBoundingClientRect;
    const R = (l: number, t: number, w: number, h: number) => ({ left: l, top: t, right: l + w, bottom: t + h, width: w, height: h }) as DOMRect;
    HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
      if (this.classList.contains("kit-workspace")) return R(0, 0, 1440, 900);
      if (this.classList.contains("kit-slot-right")) return R(300, 0, 1140, 900); // tall column ...
      if (this.classList.contains("short-card")) return R(960, 100, 480, 170); // ... holding a short card
      return R(0, 0, 0, 0);
    };
    const box = document.createElement("div");
    document.body.appendChild(box);
    const root = createRoot(box);
    try {
      act(() => root.render(<Shell mode="Design" onModeChange={() => {}} connected robotName="Arm" notices={<p>note</p>}><div className="short-card">card</div></Shell>));
      const n = box.querySelector<HTMLElement>(".shell-notices")!, h = box.querySelector<HTMLElement>(".shell-hint")!;
      expect(n.style.maxWidth).toBe("calc(100% - 1176px)"); // sides 24 + 1152; the free rect would give 48px
      expect(n.style.left).toBe("calc(50% - 564px)");
      expect(h.style.left).toBe("calc(50% - 564px)");
    } finally {
      act(() => root.unmount());
      box.remove();
      HTMLElement.prototype.getBoundingClientRect = orig;
    }
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
  it("a short card in a tall slot leaves the space under it free", () => {
    const el = root({ right: rect(300, 0, 1140, 900) });
    const card = el.querySelector(".kit-slot-right")!.appendChild(document.createElement("div"));
    card.getBoundingClientRect = rect(960, 100, 480, 170); // ends at y=270
    expect(measureInset(el)).toMatchObject({ right: 24, top: 282, left: 24 });
  });
  it("notices stay beside the tall slot column, never over its card (they do not read the free rect)", () => {
    const el = root({ right: rect(960, 0, 480, 900) });
    el.querySelector(".kit-slot-right")!.appendChild(document.createElement("div")).getBoundingClientRect = rect(960, 100, 480, 170);
    expect(measureSides(el)).toEqual({ left: 24, right: 1440 - 960 + 12 }); // banner column ends before the card's left edge
    expect(measureInset(el).right).toBe(24); // while the scene may use the space below the card
  });
});
