import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OpenFace } from "./OpenFace";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function renderFace(props: Parameters<typeof OpenFace>[0] = {}) {
  root = createRoot(container);
  act(() => {
    root.render(<OpenFace {...props} />);
  });
}

describe("OpenFace", () => {
  it("test_the_recent_list_is_empty_and_says_so_on_first_launch", () => {
    renderFace({ recent: [] });
    expect(container.textContent).toContain("No recent robots");
  });

  it("a non-empty recent list renders real rows, not the empty message", () => {
    renderFace({ recent: [{ path: "/robots/arm/lockwire.yaml", name: "arm" }] });
    expect(container.querySelectorAll(".open-face-recents li").length).toBe(1);
    expect(container.querySelector(".open-face-recents li")!.textContent).toBe("arm");
  });

  it("a recent row is its name and how long ago it was opened", () => {
    renderFace({ recent: [{ path: "/a/x/l.yaml", name: "arm", at: Date.now() }, { path: "/a/y/l.yaml", name: "arm", at: Date.now() - 2 * 864e5 }, { path: "/a/z/l.yaml", name: "arm" }] });
    expect([...container.querySelectorAll(".open-face-recents li")].map((l) => l.textContent)).toEqual(["armtoday", "arm2 days ago", "arm"]);
  });

  it("one Open robot button calls its handler", () => {
    const onOpenRobot = vi.fn();
    renderFace({ onOpenRobot });
    const buttons = Array.from(container.querySelectorAll(".open-face-actions button"));
    expect(buttons.map((b) => b.textContent)).toEqual(["Open robot"]);
    act(() => buttons[0]!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onOpenRobot).toHaveBeenCalledTimes(1);
  });

  it("every way in is disabled until the engine is ready", () => {
    renderFace({ connected: false, starters: [{ name: "arm", title: "Arm", based_on: "x" }], recent: [{ path: "/a/l.yaml", name: "arm" }] });
    expect([...container.querySelectorAll("button")].map((b) => b.disabled)).toEqual([true, true, true, true]);
  });

  it("lists the starters as example chips plus Blank, and reports the clicked name", () => {
    const onNewRobot = vi.fn();
    const starters = [
      { name: "drone", title: "Drone", based_on: "Holybro X500 V2 PX4 reference quadrotor" },
      { name: "rover", title: "Rover", based_on: "ROBOTIS TurtleBot3 Burger" },
    ];
    renderFace({ starters, onNewRobot });
    const buttons = Array.from(container.querySelectorAll(".open-face-new button"));
    expect(buttons.map((b) => b.textContent)).toEqual(["Drone · Holybro X500", "Rover · TurtleBot3", "Blank"]);
    for (const b of [buttons[1]!, buttons[2]!]) act(() => b.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onNewRobot.mock.calls).toEqual([["rover"], [null]]);
    expect(container.textContent).not.toContain("Drag to turn"); // no robot here: the gesture hint waits for one
  });
});
