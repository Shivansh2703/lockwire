import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { CheckPanel, barOf, servoStall } from "./CheckPanel";
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
    expect([...rows[0]!.parentElement!.querySelectorAll(".kit-chip")].map((c) => c.textContent)).toEqual(["Elbow joint"]); // chips are buttons beside the row
    expect(rows[0]!.parentElement!.querySelector(".kit-chip")!.getAttribute("title")).toBe("elbow"); // the full id is the tooltip
    let props = robotView.mock.lastCall![0];
    expect(props.selected).toBe("elbow"); // the first problem is open on landing: 0 taps to see it
    expect(container.querySelector(".check-verdict")!.textContent).toBe("VerdictNO-GO1 fault · 1 warningCompare with datasheetRe-check"); // the verdict card leads the panel
    expect(container.querySelector(".check-verdict-actions")!.textContent).toBe("Compare with datasheetRe-check"); // one wrapping group: never clipped at 1280x800

    act(() => rows[1]!.click());
    props = robotView.mock.lastCall![0];
    expect(props.selected).toBe("wrist");
    expect(props.focus).toEqual(["wrist"]);
    const after = [...container.querySelectorAll<HTMLButtonElement>(".kit-finding")];
    expect(after.map((r) => r.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
    expect(after[1]!.classList.contains("is-selected")).toBe(true);
    expect(container.querySelector(".check-count")!.textContent).toBe("1 fault · 1 warning");
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
    expect(container.querySelector(".check-count")!.textContent).toBe("1 fault");
    expect(container.querySelector(".check-headline")!.classList).toContain("verdict-go-with-notes"); // packaged e2e reads it
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    expect(onRecheck).toHaveBeenCalledTimes(1);
  });

  it("a grouped card titled with its own numbers shows each joint's line, with no shared note", async () => {
    const weak = (n: string) => ({ ...first!, severity: "violation" as const, shape: "devices/*/drives_joint", group_title: "{n} servos can't reach their joint limit", title: `${n}: servo 1.91 N·m, joint limit 10 N·m`, label: "Servo too weak", targets: [{ kind: "device" as const, id: `dev.${n}`, label: `Servo ${n}` }] });
    const page = { ...PAGE, cards: [weak("Elbow"), weak("Gripper")] };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={page as never} selected={0} onSelect={() => {}} onRecheck={() => {}} />));
    expect(container.querySelector(".kit-finding-title")!.textContent).toBe("2 servos can't reach their joint limit");
    expect([...container.querySelectorAll(".check-group .kit-finding-title")].map((e) => e.textContent)).toEqual(["Elbow: servo 1.91 N·m, joint limit 10 N·m", "Gripper: servo 1.91 N·m, joint limit 10 N·m"]);
    expect(container.querySelector(".check-group-note")).toBeNull();
  });

  it("a grouped stage-recording card set keeps its shared note and labels rows by device", () => {
    const rec = (n: string) => ({ ...first!, severity: "violation" as const, shape: "devices/*/rate_hz", group_title: "{n} recording differs from lockfile", title: `${n}: recording differs from lockfile`, label: "Rate", targets: [{ kind: "device" as const, id: `dev.${n}`, label: `Cam ${n}` }] });
    const page = { ...PAGE, cards: [rec("a"), rec("b")] };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={page as never} selected={0} onSelect={() => {}} onRecheck={() => {}} />));
    expect([...container.querySelectorAll(".check-group .kit-finding-title")].map((e) => e.textContent)).toEqual(["Cam a", "Cam b"]);
    expect(container.querySelectorAll(".check-group-note")).toHaveLength(1);
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
    expect([...rows[0]!.parentElement!.querySelectorAll(".kit-chip")]).toHaveLength(0);
    expect([...rows[1]!.parentElement!.querySelectorAll(".kit-chip")].map((c) => c.textContent)).toEqual(["Whole robot"]);
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
    const mk = (severity: "violation" | "not_scored", n: number) => ({ ...first!, severity, shape: `s${n}`, title: `t${n}`, label: `l${n}`, targets: [], quantity: n === 2 ? "devices/d/physical/mass" : "topology/x" });
    const page = { ...PAGE, cards: [mk("violation", 0), mk("violation", 1), mk("not_scored", 2), mk("not_scored", 3), mk("not_scored", 4)] };
    const onJump = Object.assign(vi.fn(), { can: () => true });
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={page as never} selected={null} onSelect={() => {}} onJump={onJump} onCompare={() => {}} />));
    expect(container.querySelector(".check-count")!.textContent).toBe("2 faults · 3 not scored");
    expect(container.querySelector(".check-verdict")!.textContent).toContain("Compare with datasheet"); // whole-robot compare stays on the verdict card
    expect(container.querySelector(".check-list > .kit-disclosure summary")!.textContent).toBe("Not scored (3)");
    expect([...container.querySelectorAll(".finding-card > .kit-btn")].map((b) => b.textContent)).toEqual(["Declare it", "Declare it", "Declare it"]); // a part row says Declare it
    expect(container.querySelectorAll(".check-list > .kit-disclosure .kit-finding")).toHaveLength(3);
    expect(container.querySelectorAll(".check-list > div > .kit-finding")).toHaveLength(2);
  });

  it("frames without mass is a quiet note under the rows, not a counted problem", () => {
    const raw = "WARNING model/links/massless_frames\n  observed   3 massless frames (sensor/mount frames, normal): base_footprint, imu_link, ...";
    const note = { ...first!, severity: "warning" as const, quantity: "model/links/massless_frames", raw, targets: [] };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...PAGE, cards: [cards[0]!, note] } as never} selected={null} onSelect={() => {}} />));
    expect(container.querySelector(".check-count")!.textContent).toBe("1 fault");
    expect(container.querySelectorAll(".kit-finding")).toHaveLength(1);
    expect(container.querySelector(".check-note")!.textContent).toBe("3 frames without mass — normal for sensor and mount frames");
  });

  it("a warning-only robot counts an amber warning, never a bare finding or a red fault", () => {
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...PAGE, verdict: "GO", cards: [cards[1]!] } as never} selected={null} onSelect={() => {}} />));
    expect(container.querySelector(".check-count")!.textContent).toBe("1 warning");
    expect([container.querySelectorAll(".check-count-warn").length, container.querySelectorAll(".check-count-fault").length]).toEqual([1, 0]);
  });

  it("faults count red and warnings amber, side by side", () => {
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={PAGE as never} selected={null} onSelect={() => {}} />));
    expect([container.querySelector(".check-count-fault")!.textContent, container.querySelector(".check-count-warn")!.textContent]).toEqual(["1 fault", "1 warning"]);
  });

  it("accepted findings show as a muted Accepted section, counted, never red", () => {
    const accepted = [{ title: "Total mass off", observed: "1.554", reason: "battery not modelled", witness: "sam", date: "2026-11-06" }];
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...PAGE, cards: [cards[0]!], accepted } as never} selected={null} onSelect={() => {}} />));
    expect(container.querySelector(".check-count")!.textContent).toBe("1 fault · 1 accepted");
    expect(container.querySelector(".kit-disclosure summary")!.textContent).toBe("Accepted (1)");
    expect(container.querySelector(".kit-disclosure .check-note")!.textContent).toBe("Total mass off: battery not modelled. Signed by sam, 2026-11-06");
  });
});

describe("Check filter", () => {
  const mk = (severity: "violation" | "warning" | "not_scored", n: number, involves: string[]) => ({ ...first!, severity, shape: `s${n}`, title: `t${n}`, label: `l${n}`, targets: [], involves });
  const page = { ...PAGE, cards: [mk("violation", 0, ["device:cam"]), mk("warning", 1, ["device:cam", "rail:5v"]), mk("not_scored", 2, ["rail:5v"])] };
  const pick = (label: string, value: string) => {
    const sel = [...container.querySelectorAll("label")].find((l) => l.textContent!.startsWith(label))!.querySelector("select")!;
    act(() => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(sel, value); sel.dispatchEvent(new Event("change", { bubbles: true })); });
  };
  const titles = () => [...container.querySelectorAll(".kit-finding")].map((r) => /t\d/.exec(r.textContent!)![0]);
  const verdict = () => container.querySelector(".check-headline")!.textContent;
  const render = () => { root = createRoot(container); act(() => root.render(<CheckPanel payload={page as never} selected={null} onSelect={() => {}} />)); };

  it("NO-GO only shows exactly the NO-GO findings and Clear restores all three; the verdict never moves", () => {
    render();
    const before = verdict();
    expect(titles()).toHaveLength(3);
    expect(container.querySelector(".check-filter-count")).toBeNull(); // default is All: no count, nothing changed
    pick("Show", "violation");
    expect(titles()).toEqual(["t0"]);
    expect(container.querySelector(".check-filter-count")!.textContent).toBe("Showing 1 of 3");
    expect(verdict()).toBe(before);
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Clear")!.click());
    expect(titles()).toHaveLength(3);
    expect(container.querySelector(".check-filter-count")).toBeNull();
  });

  it("the part list names parts from the robot graph, never a rail. or wire. id", () => {
    const graph = { nodes: [{ id: "rail:5v", label: "Camera supply" }, { id: "device:cam", label: "Camera" }], edges: [] };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={page as never} graph={graph as never} selected={null} onSelect={() => {}} />));
    const opts = [...container.querySelectorAll(".check-filter option")].map((o) => o.textContent);
    expect(opts).toContain("Camera supply");
    expect(opts.join("|")).not.toMatch(/rail\.|wire\.|5v/);
  });

  it("a part with no name anywhere reads as its kind and id, e.g. rail 5v", () => {
    render();
    expect([...container.querySelectorAll(".check-filter option")].map((o) => o.textContent)).toContain("rail 5v");
  });

  it("Clear resets the part filter too: all rows back and the part select reads Any part", () => {
    render();
    pick("Part", "rail:5v");
    expect(titles()).toHaveLength(2);
    act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Clear")!.click());
    expect(titles()).toHaveLength(3);
    expect(container.querySelectorAll<HTMLSelectElement>(".check-filter select")[1]!.selectedOptions[0]!.textContent).toBe("Any part");
  });

  it("the part filter shows only findings whose involves include that part", () => {
    render();
    pick("Part", "rail:5v");
    expect(titles()).toEqual(["t1", "t2"]);
    expect(container.querySelector(".check-filter-count")!.textContent).toBe("Showing 2 of 3");
  });
});

describe("Check mode header", () => {
  it("a rail's budget card and its current-limit card are one row, both findings open", () => {
    const [b] = X500_CHECK_PAYLOAD.cards, amps = { title: "Over its current limit", raw: "7.04 A (mode: nominal)", observed: { ...b!.observed!, value: 7.04, unit: "A" } };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...PAGE, cards: [{ ...b!, cluster_root: "r" }, { ...b!, ...amps, cluster_root: "r" }] } as never} selected={0} onSelect={() => {}} />));
    expect([container.querySelector(".check-count")!.textContent, container.querySelectorAll(".check-detail").length]).toEqual(["1 fault", 2]); // bites two red rows for one rail
  });
  it("one rail's per-mode findings are one row; its mode blocks lead with the typed numbers, the figures behind the exact finding", () => {
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={X500_CHECK_PAYLOAD} selected={0} onSelect={() => {}} />));
    const rows = [...container.querySelectorAll(".kit-finding")].map((r) => r.textContent);
    expect(rows.filter((t) => /rail overloaded/.test(t!))).toHaveLength(1);
    expect(rows.join("|")).not.toMatch(/ mode/);
    expect(container.querySelector(".check-count")!.textContent).toBe("1 fault · 4 not scored"); // plain words, no engine arithmetic ("2 modes")
    expect(container.querySelectorAll(".check-detail .finding-alt").length).toBeGreaterThan(0);
    expect([...container.querySelectorAll(".check-detail .finding-alt")].every((a) => a.closest(".check-detail > .kit-disclosure") === null)).toBe(true); // Instead sits under Fix, not inside the exact-finding disclosure
    const heads = [...container.querySelectorAll(".check-detail")].map((d) => d.closest("details")!.querySelector("summary")!.textContent);
    expect(heads).toEqual(["Nominal \u00b7 35.2 W of 15.6 W", "7W \u00b7 17.2 W of 15.6 W"]);
  });
});

describe("Check raw output", () => {
  const raw = "VIOLATION topology/rails/rail.5v/brownout\n  observed  1";
  const show = (c: object) => {
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...PAGE, cards: [{ ...cards[0]!, ...c }] } as never} selected={0} onSelect={() => {}} />));
  };
  const NOTE = "Numbers not shown: this check can't yet say where they came from.";

  it("the machine block is labelled Raw output and shut until opened", () => {
    show({ raw, figure_note: null });
    const d = container.querySelector<HTMLDetailsElement>("details:has(.finding-raw) details, details details:has(.finding-raw)")!;
    expect(d.querySelector("summary")!.textContent).toBe("Raw output");
    expect(d.open).toBe(false);
  });

  it("'Numbers not shown' hides while other words exist", () => {
    show({ raw, observed: null, expected: null, body: "The rail browns out.", figure_note: NOTE });
    expect(container.textContent).not.toMatch(/Numbers not shown/);
  });

  it("a fault with no drawn figures states its measured and needed values outside the closed disclosure", () => {
    show({ raw, observed: null, expected: null, severity: "violation", body: "Measured 5 V sags to 4.65 V. Needs >= 4.75 V.", figure_note: null });
    const p = [...container.querySelectorAll(".finding-body")].find((e) => /4\.65 V/.test(e.textContent!))!;
    expect(p.closest("details")).toBeNull();
  });

  it("'Numbers not shown' shows when it is all there is", () => {
    show({ raw, observed: null, expected: null, body: "", figure_note: NOTE });
    expect(container.textContent).toMatch(/Numbers not shown/);
  });
});

describe("Check servo finding (Part Swap)", () => {
  it("offers Swap and Set limit; Set limit only shows the exact edit, Swap opens the panel for that joint", async () => {
    const swapPreview = vi.fn().mockResolvedValue({ joint: "wrist", current: null, load_nm: 0.15, chosen: null, delta_g: 0, candidates: [], knock_on: [], notes: [] });
    const writeContract = vi.fn();
    Object.assign(window, { lockwire: { swapPreview, writeContract } });
    const edit = "in arm.urdf: joint wrist <limit effort=\"0.4\"> → \"0.226\"";
    const raw = `VIOLATION model/joints/wrist/limits/effort\n  expected   <= 0.226 N*m, the feetech.scs0009 stall torque\n  action     a simulator will let wrist push 0.4 N*m, which a feetech.scs0009 cannot; ${edit}, then re-lock (ignore if another motor drives it)`;
    const servo = { ...cards[0]!, shape: "model/joints/*/limits/effort", quantity: "model/joints/wrist/limits/effort", raw };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...PAGE, cards: [servo] } as never} selected={0} onSelect={() => {}} contract="/r.yaml" expectedRevision="rev1" onSwapped={() => {}} />));
    const button = (t: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === t);
    act(() => button("Set limit to servo's stall")!.click());
    expect(container.querySelector(".check-edit code")!.textContent).toBe(edit);
    act(() => button("Copy")!.click());
    expect(writeContract).not.toHaveBeenCalled(); // the app never writes the model file
    expect(swapPreview).not.toHaveBeenCalled();
    await act(async () => button("Swap")!.click());
    expect(swapPreview).toHaveBeenCalledWith({ contract: "/r.yaml", joint: "wrist" });
    expect(container.textContent).toContain("Swap the wrist servo");
  });
});

describe("Check owner pills", () => {
  const html = (owners: string[]) => {
    const p = { ...X500_CHECK_PAYLOAD, findings: owners.map((owner) => ({ ...X500_CHECK_PAYLOAD.findings[0]!, owner })) };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={p as never} selected={null} onSelect={() => {}} />));
    return container.querySelector(".check-pills");
  };
  it("a lone owner shows no pill row; two owners do", () => {
    expect(html(["you"])).toBeNull();
    act(() => root.unmount());
    expect(html(["you", "lab"])).not.toBeNull();
  });
});
describe("servoStall", () => {
  it("keeps a joint name that contains a slash", () => {
    const raw = "expected <= 0.2 N*m, the feetech.scs0009 stall torque\naction  x; in m: joints a limits effort 0.4 → 0.2, then re-lock";
    const card = { ...first!, shape: "model/joints/*/limits/effort", quantity: "model/joints/left/wrist/limits/effort", raw } as never;
    expect(servoStall(card)?.joint).toBe("left/wrist");
  });
});
describe("Check limit bar", () => {
  const fig = (value: number, unit = "W") => ({ value, unit, kind: "derived" as const, by: "t" });
  it("width is value over limit, not limit over value", () => {
    const b = barOf({ observed: fig(30), expected: fig(60) })!;
    expect(b.fill).toBeCloseTo(0.5);
    expect(b.good).toBeCloseTo(0.5);
  });
  it("past the limit the bar is full and the good part shrinks", () => {
    const b = barOf({ observed: fig(90), expected: fig(60) })!;
    expect(b.fill).toBe(1);
    expect(b.good).toBeCloseTo(1 / 1.5);
    expect(barOf({ observed: fig(600), expected: fig(60) })!.good).toBe(0.5); // drawn at most twice the limit
  });
  it("no bar without both numbers, matching units, or a positive limit", () => {
    expect(barOf({ observed: null, expected: fig(60) })).toBeNull();
    expect(barOf({ observed: fig(1, "V"), expected: fig(60) })).toBeNull();
    expect(barOf({ observed: fig(1), expected: fig(0) })).toBeNull();
  });
  it("a servo-stall card without typed figures reads its own observed and expected lines", () => {
    const b = barOf({ observed: null, expected: null, raw: "model/joints/wrist/limits/effort\nobserved  0.40 N*m joint effort limit, expected  <= 0.226 N*m, the feetech.scs0009 stall torque" })!;
    expect(b.words).toBe("0.4 N*m against a limit of 0.226 N*m");
    expect(b.fill).toBe(1);
    expect(barOf({ observed: null, expected: null, raw: "observed  2 frames, expected  3 parts" })).toBeNull();
  });
  it("an over-limit card shows the bar and the numbers in words", () => {
    const card = { ...first!, severity: "violation" as const, observed: fig(35.2), expected: fig(15.6) };
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...X500_CHECK_PAYLOAD, cards: [card], findings: [X500_CHECK_PAYLOAD.findings[0]!] } as never} selected={0} onSelect={() => {}} />));
    expect(container.querySelector(".check-limit-words")!.textContent).toBe("35.2 W against a limit of 15.6 W, over by 126%");
    expect(container.querySelector(".conn-bar.is-over")).not.toBeNull();
    act(() => root.unmount());
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...X500_CHECK_PAYLOAD, cards: [{ ...card, observed: null, raw: "" }], findings: [X500_CHECK_PAYLOAD.findings[0]!] } as never} selected={0} onSelect={() => {}} />));
    expect(container.querySelector(".check-limit")).toBeNull();
  });
});

describe("Check limit bar from typed quantities (CHK-11t)", () => {
  const nm = (value: number) => ({ value, unit: "N*m", kind: "derived" as const, by: "t" });
  it("the wrist servo bar uses the typed torques, not the numbers in its prose", () => {
    const raw = "VIOLATION devices/dev.servo.wrist/drives_joint\n  observed   9.99 N*m continuous geared torque\n  expected   >= 99.00 N*m joint effort limit";
    const b = barOf({ observed: nm(0.2263), expected: nm(0.4), raw })!;
    expect(b.words).toBe("0.226 N*m available against 0.4 N*m needed");
    expect(b.pct).toBe(43);
  });
  it("a servo strong enough draws no red", () => {
    const b = barOf({ observed: nm(0.5), expected: nm(0.4), raw: "expected   >= 0.40 N*m joint effort limit" })!;
    expect(b.ratio).toBeLessThan(1);
  });
});

describe("Check requirements (Pro rows)", () => {
  const rows = [{ id: "r1", text: "Carry a 2 kg payload", check: "r1", status: "unproven" as const, why: "no check named r1" }];
  const show = (requirements?: typeof rows) => {
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...X500_CHECK_PAYLOAD, requirements } as never} selected={null} onSelect={() => {}} />));
  };
  it("a requirement no check proves says so in plain words", () => {
    show(rows);
    expect(container.querySelector(".check-requirements")!.textContent).toContain("Carry a 2 kg payload: Unproven: no check proves it yet");
  });
  it("the free app has no rows and shows no requirements section", () => {
    show([]);
    expect(container.querySelector(".check-requirements")).toBeNull();
  });
});

describe("Check panel, a GO that compared nothing physical", () => {
  const ROW = { name: "Mass", text: "1 vs 1", margin: "0", raw: "", mark: null };
  const draw = (p: Partial<typeof X500_CHECK_PAYLOAD>) => {
    root = createRoot(container);
    act(() => root.render(<CheckPanel payload={{ ...X500_CHECK_PAYLOAD, cards: [], findings: [], ...p }} selected={null} onSelect={() => {}} />));
    return container.querySelector(".check-headline")!.textContent!;
  };
  it("says what the GO covers when only the lock agreed with itself", () => {
    expect(draw({ verdict: "GO", headline: "Nothing could be compared yet", scored: 1, adds_up: [] })).toContain("Locked. Nothing physical compared yet. Declare the robot's mass");
  });
  it("decides on adds_up, not the headline words, in both directions", () => {
    expect(draw({ verdict: "GO", headline: "Locked", adds_up: [] })).toContain("Nothing physical compared");
    expect(draw({ verdict: "GO", headline: "Nothing could be compared yet", adds_up: [ROW] })).not.toContain("Nothing physical compared");
  });
  it("stays silent on a GO with real comparisons", () => {
    expect(draw({ verdict: "GO", headline: "Everything I checked agrees", scored: 3, adds_up: [ROW, ROW, ROW] })).not.toContain("Nothing physical compared");
  });
  it("leaves a NOT SCORED document unchanged", () => {
    expect(draw({ verdict: "NOT_SCORED", headline: "Nothing could be compared yet", scored: 0, adds_up: [] })).not.toContain("Nothing physical compared");
  });
});
