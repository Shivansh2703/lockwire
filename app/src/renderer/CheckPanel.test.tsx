import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { CheckPanel } from "./CheckPanel";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";

const robotView = vi.fn((_props: Record<string, unknown>) => null);
vi.mock("./scene/RobotView", () => ({ RobotView: (props: Record<string, unknown>) => robotView(props) }));
vi.mock("./scene/Canvas", () => ({ Canvas: () => null, useScene: () => { throw new Error("unused"); } }));

const joint = (name: string) => ({ name, type: "revolute", parent: "base", child: `${name}_link`, position: [0, 0, 0], axis: [0, 0, 1], lower: null, upper: null, effort: null });
const [first, ...rest] = X500_CHECK_PAYLOAD.cards;
const cards = [
  { ...first!, severity: "violation" as const, targets: [{ kind: "joint" as const, id: "elbow", label: "Elbow joint" }] },
  { ...rest[0]!, severity: "warning" as const, targets: [{ kind: "joint" as const, id: "wrist", label: "Wrist joint" }] },
];
const PAGE = {
  ...X500_CHECK_PAYLOAD, revision: "r", cards, joints: [joint("elbow"), joint("wrist")], links: [], scene: null,
  faces: {}, health: null,
};

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as never;
  container = document.createElement("div");
  document.body.appendChild(container);
  robotView.mockClear();
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  Reflect.deleteProperty(window, "lockwire");
});

describe("Check panel", () => {
  it("renders one row per finding with target chips and selecting one hands its joint to RobotView", async () => {
    Object.assign(window, { lockwire: {
      startFromUrdf: vi.fn().mockResolvedValue({ started: false, opened: { opened: true, path: "/tmp/r.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD } }),
      getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue({}), getHistory: vi.fn().mockResolvedValue([]),
    } });
    root = createRoot(container);
    act(() => root.render(<App />));
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Open robot")!.click());
    await act(async () => {});

    const rows = [...container.querySelectorAll<HTMLButtonElement>(".kit-finding")];
    expect(rows).toHaveLength(2);
    expect([...rows[0]!.querySelectorAll(".kit-chip")].map((c) => c.textContent)).toEqual(["Elbow joint"]);
    expect(rows[0]!.querySelector(".kit-chip")!.getAttribute("title")).toBe("elbow"); // the full id is the tooltip
    let props = robotView.mock.lastCall![0];
    expect(props.highlight).toEqual([{ id: "elbow", level: "bad" }, { id: "wrist", level: "warn" }]);
    expect(props.selected).toBeNull();

    act(() => rows[1]!.click());
    props = robotView.mock.lastCall![0];
    expect(props.selected).toBe("wrist");
    expect(props.focus).toEqual(["wrist"]);
    const after = [...container.querySelectorAll<HTMLButtonElement>(".kit-finding")];
    expect(after.map((r) => r.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
    expect(after[1]!.classList.contains("is-selected")).toBe(true);
    expect(container.querySelector(".check-count")!.textContent).toBe("2 problems");
    expect(container.querySelectorAll(".finding-fix")).toHaveLength(1); // detail opens inline on the one selected row
  });

  it("groups repeated findings into one row and lets a member be picked (check 1)", async () => {
    const same = (id: string) => ({ ...first!, severity: "violation" as const, shape: "devices/*/drives_joint", title: "drives_joint doesn't match", label: "Drives joint", targets: [{ kind: "joint" as const, id, label: id }] });
    const grouped = { ...PAGE, verdict: "GO_WITH_NOTES", cards: [same("a"), same("b"), same("c")] };
    const onRecheck = vi.fn();
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={grouped as never} selected={null} onSelect={() => {}} onRecheck={onRecheck} />));
    const rows = [...container.querySelectorAll(".kit-finding")];
    expect(rows).toHaveLength(1);
    expect(rows[0]!.querySelector(".kit-finding-title")!.textContent).toBe("Drives joint x3");
    expect(container.querySelector(".check-count")!.textContent).toBe("1 problem");
    expect(container.querySelector(".check-headline")!.classList).toContain("verdict-go-with-notes"); // packaged e2e reads it
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    expect(onRecheck).toHaveBeenCalledTimes(1);
  });

  it("only an engine-flagged whole-robot card gets the Whole robot chip and tint", async () => {
    const mk = (n: number, whole: boolean) => ({ ...first!, severity: "violation" as const, shape: `s${n}`, title: `t${n}`, label: `l${n}`, targets: [], whole });
    const page = { ...PAGE, cards: [mk(0, false), mk(1, true)] };
    Object.assign(window, { lockwire: {
      startFromUrdf: vi.fn().mockResolvedValue({ started: false, opened: { opened: true, path: "/tmp/r.yaml", payload: page, checkPayload: page } }),
      getContractPage: vi.fn().mockResolvedValue(page), getDeclareForm: vi.fn().mockResolvedValue({}), getHistory: vi.fn().mockResolvedValue([]),
    } });
    root = createRoot(container);
    act(() => root.render(<App />));
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Open robot")!.click());
    await act(async () => {});
    const rows = [...container.querySelectorAll<HTMLButtonElement>(".kit-finding")];
    expect([...rows[0]!.querySelectorAll(".kit-chip")]).toHaveLength(0);
    expect([...rows[1]!.querySelectorAll(".kit-chip")].map((c) => c.textContent)).toEqual(["Whole robot"]);
    act(() => rows[0]!.click());
    expect(robotView.mock.lastCall![0].highlight).toEqual([]);
    act(() => rows[1]!.click());
    expect(robotView.mock.lastCall![0].highlight).toEqual([{ id: "elbow", level: "bad", faint: true }, { id: "wrist", level: "bad", faint: true }]);
  });

  it("an open card with no figures still says what was found in words", () => {
    const body = "Measured joint torque limit is unset (0) in the model; torque is not checked. Needs a positive joint effort limit.";
    const fig = (value: number) => ({ value, unit: "W", kind: "declared", by: "x" });
    const mk = (n: number, figures: boolean) => ({ ...first!, severity: "warning" as const, shape: `s${n}`, title: `t${n}`, label: `l${n}`, targets: [], body,
      observed: figures ? fig(1) : null, expected: figures ? fig(2) : null });
    const page = { ...PAGE, cards: [mk(0, false), mk(1, true)] };
    const render = (selected: number) => act(() => root.render(<CheckPanel payload={page as never} selected={selected} onSelect={() => {}} />));
    root = createRoot(container);
    render(0);
    expect(container.querySelector(".check-detail .finding-body")!.textContent).toBe(body);
    render(1);
    expect(container.querySelector(".check-detail .finding-fix")).not.toBeNull();
    expect(container.querySelector(".check-detail .finding-body")).toBeNull();
  });

  it("folds not-checked cards into one disclosure and counts only problems", () => {
    const mk = (severity: "violation" | "not_scored", n: number) => ({ ...first!, severity, shape: `s${n}`, title: `t${n}`, label: `l${n}`, targets: [] });
    const page = { ...PAGE, cards: [mk("violation", 0), mk("violation", 1), mk("not_scored", 2), mk("not_scored", 3), mk("not_scored", 4)] };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={page as never} selected={null} onSelect={() => {}} />));
    expect(container.querySelector(".check-count")!.textContent).toBe("2 problems \u00b7 3 not checked");
    expect(container.querySelector(".check-list > .kit-disclosure summary")!.textContent).toBe("3 not checked");
    expect(container.querySelectorAll(".check-list > .kit-disclosure .kit-finding")).toHaveLength(3);
    expect(container.querySelectorAll(".check-list > div > .kit-finding")).toHaveLength(2);
  });
});
