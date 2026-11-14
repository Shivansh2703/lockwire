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

  it("while the engine starts the examples area says Starting…, and once examples load it does not", () => {
    renderFace({ connected: false, starters: [] });
    expect(container.querySelector(".open-face-new")!.textContent).toContain("Starting…");
    act(() => root.render(<OpenFace connected starters={[{ name: "arm", title: "Arm", based_on: "x" }]} />));
    expect(container.querySelector(".open-face-new")!.textContent).not.toContain("Starting…");
  });

  it("an engine that failed to start gets one error line, not an empty list", () => {
    renderFace({ connected: false, engineError: "port in use", starters: [] });
    const text = container.querySelector(".open-face-new")!.textContent!;
    expect(text).toContain("could not start its engine: port in use");
    expect(text).not.toContain("Starting…");
  });

  const ALL = [
    { name: "hexapod-param", title: "Power-focused hexapod", based_on: "Lockwire design example (no maker model)" },
    { name: "arm", title: "Arm", based_on: "SO-101 by TheRobotStudio" },
    { name: "rover-param", title: "Resizable rover", based_on: "Lockwire design example (no maker model)" },
    { name: "drone", title: "Drone", based_on: "Holybro X500 V2 PX4 reference quadrotor" },
    { name: "humanoid", title: "Humanoid", based_on: "ToddlerBot 2xm" },
    { name: "quadruped", title: "Quadruped", based_on: "Solo-12 (Open Dynamic Robot Initiative)" },
    { name: "rover", title: "Rover", based_on: "ROBOTIS TurtleBot3 Burger" },
  ];
  const items = () => [...container.querySelectorAll(".open-face-new li")];

  const WITH_FAILS = [...ALL, { name: "servo-bench", title: "Servo bench", based_on: "Lockwire design example (no maker model)" }, { name: "arm-power", title: "Two-joint desk arm", based_on: "Lockwire design example (no maker model)" }];

  it("the two examples that fail come first and say so, then real robots, then the design examples", () => {
    renderFace({ starters: WITH_FAILS });
    const labels = items().map((l) => l.querySelector("button")!.firstChild!.textContent!);
    expect(labels).toEqual(["Desk arm · findings to catch", "Servo bench · built to fail", "Arm · SO-101", "Drone · X500", "Humanoid · ToddlerBot", "Quadruped · Solo-12", "Rover · TurtleBot3", "Hexapod · design example", "Rover · resizable example"]);
    expect(labels.every((l) => /^[\w ]+ · .+/.test(l) && l.length <= 32)).toBe(true);
  });

  it("each example's muted line says what its check finds", () => {
    renderFace({ starters: WITH_FAILS });
    expect(items().map((l) => l.querySelector(".open-face-note")?.textContent)).toEqual(["A made-up arm with findings", "Two findings, on purpose", "Nothing disagrees", "Nothing disagrees", "Nothing disagrees", "One note to read", "One note to read", "Made-up, nothing disagrees", "Made-up, nothing disagrees"]);
  });

  it("the servo bench chip opens the bench by its starter name in one tap", () => {
    const onNewRobot = vi.fn();
    renderFace({ starters: WITH_FAILS, onNewRobot });
    act(() => (items()[1]!.querySelector("button") as HTMLButtonElement).dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onNewRobot.mock.calls).toEqual([["servo-bench"]]);
  });

  it("Blank is not in the grid; Start from blank sits under it and opens blank in one tap", () => {
    const onNewRobot = vi.fn();
    renderFace({ starters: ALL, onNewRobot });
    expect(items().some((l) => /blank/i.test(l.textContent!))).toBe(false);
    const blank = container.querySelector(".open-face-blank button") as HTMLButtonElement;
    expect(blank.textContent).toBe("Start from blank");
    act(() => blank.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onNewRobot.mock.calls).toEqual([[null]]);
  });

  it("one tap on an example opens it", () => {
    const onNewRobot = vi.fn();
    renderFace({ starters: ALL, onNewRobot });
    act(() => items()[6]!.querySelector("button")!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onNewRobot.mock.calls).toEqual([["rover-param"]]);
    expect(container.textContent).not.toContain("Drag to turn");
    expect(container.textContent).toContain("URDF, SDF, xacro, MuJoCo, Webots or a lockfile"); // lane E OPEN-FMT hint, kept through the merge
  });

  it("the Agent entry is one quiet item beside Start from blank, one tap, and absent unless the app offers it", () => {
    renderFace({ starters: ALL });
    expect(container.textContent).not.toContain("Agent");
    act(() => root.unmount());
    const onAgent = vi.fn();
    renderFace({ starters: ALL, onAgent });
    const agent = [...container.querySelectorAll("button")].filter((b) => b.textContent!.includes("Agent"));
    expect(agent.map((b) => b.textContent)).toEqual(["Ask the Agent to design one"]);
    act(() => agent[0]!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onAgent).toHaveBeenCalledTimes(1);
  });
});
