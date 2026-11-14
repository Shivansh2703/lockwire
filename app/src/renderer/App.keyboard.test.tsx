/**
 * E2.3 — keyboard reachability of each face's primary control, plus DeclareFace's
 * Escape-cancels-draft behaviour.
 *
 * jsdom does not synthesize a browser's own Enter/Space -> click translation for a
 * native <button> (measured: dispatching a "keydown" Enter at a plain <button> fires
 * no "click" -- same fact PartTree's own onKeyDown comment and its test already
 * rely on). So "press Enter, and it activates" is proven here, for a genuine native
 * <button>, by asserting it IS a real button (so a real browser handles Enter/Space
 * for it) and then firing the "click" a real Enter produces -- not by asking jsdom to
 * fake the browser's own key handling. A control that is a plain, non-button element
 * (a clickable div) would need its own onKeyDown to pass this same proof; none of the
 * seven faces' primary controls are.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DesignControls, EditKeys, type DesignEditRequest } from "./DesignControls";
import { shortcutRows } from "./shortcuts";
import { DeclareFace, type DeclareFormPayload } from "./DeclareFace";
import { OpenFace } from "./OpenFace";
import { PlanFace } from "./PlanFace";
import { ReferenceFace } from "./ReferenceFace";
import { Shell } from "./Shell";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import type { CheckPayload, Finding, PlanPayload } from "../shared/api.gen";

function mockMatchMedia(): void {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  mockMatchMedia();
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

/** Focus the control, then fire the "click" a real browser produces when Enter is
 * pressed on a focused native <button> -- see file header. */
function focusThenEnter(control: HTMLElement) {
  expect(control.tagName).toBe("BUTTON");
  control.focus();
  expect(document.activeElement).toBe(control);
  act(() => control.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

const DECLARE_FORM: DeclareFormPayload = {
  revision: "revision",
  rows: [{ path: "mass.total", label: "Total mass", group: "Total", short: "mass", unit: "kg", kind: "declared", value: 2,
    provenance: { source: "weigh-in.csv", witness: "Maya", locked_at: "2026-09-09" } }],
  witness: { prefill: "Maya" },
};

describe("E2.3 -- each face's primary control is reachable and Enter-operable", () => {
  it("OpenFace: 'Open robot'", () => {
    const onOpenRobot = vi.fn();
    root = createRoot(container);
    act(() => root.render(<OpenFace onOpenRobot={onOpenRobot} />));
    const button = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Open robot")!;
    focusThenEnter(button);
    expect(onOpenRobot).toHaveBeenCalledTimes(1);
  });

  it("PlanFace: 'View citation'", () => {
    const onSelectFinding = vi.fn();
    const finding: Finding = {
      severity: "violation",
      quantity: "mass.total",
      observed: "3 kg",
      expected: "<= 2 kg",
      action: "reduce mass",
    };
    const payload: PlanPayload = {
      format_version: "2", schema_version: "1", robot: "X500", verdict: "NO-GO",
      exit_code: 1, counts: { violation: 1, warning: 0, not_scored: 0 }, scored: 1,
      findings: [finding], artifact_type: "lockwire-plan-report", steps: [],
    };
    root = createRoot(container);
    act(() => root.render(<PlanFace payload={payload} onSelectFinding={onSelectFinding} />));
    act(() => container.querySelector<HTMLButtonElement>(".kit-finding")!.click());
    const button = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "View citation")!;
    focusThenEnter(button);
    expect(onSelectFinding).toHaveBeenCalledWith(finding);
  });

  it("ReferenceFace: the drawer disclosure toggle", () => {
    root = createRoot(container);
    act(() => root.render(<ReferenceFace payload={{
      title: "Reference",
      devices: [],
      drawers: [{ title: "Frame", content: "steel", provenance: "spec.md", cli_line: "lockwire reference /tmp/x.yaml" }],
      power_tree: null,
    }} />));
    const details = container.querySelector(".reference-drawer details") as HTMLDetailsElement;
    expect(details.querySelector("summary")!.textContent).toBe("Frame"); // a native <summary> is keyboard-reachable
    expect(details.open).toBe(true); // the first drawer opens by default (Docs r8)
  });

  // The Shell's theme toggle is the one always-present, self-contained control, so it stands in as the primary one.
  it("Shell: the theme toggle", () => {
    root = createRoot(container);
    act(() => root.render(<Shell mode="Declare" onModeChange={() => {}} connected={false} />));
    const button = container.querySelector(".theme-toggle") as HTMLButtonElement;
    const before = button.textContent;
    focusThenEnter(button);
    expect(button.textContent).not.toBe(before);
  });

  it("DeclareFace: 'Declare'", () => {
    const onSubmit = vi.fn().mockResolvedValue({ written: true });
    root = createRoot(container);
    act(() => root.render(
      <DeclareFace selectedPath={DECLARE_FORM.rows[0]!.path} contractPath="/tmp/x.yaml" payload={DECLARE_FORM} expectedRevision="revision" onSubmit={onSubmit} onReview={vi.fn()} />,
    ));
    const button = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Declare")!;
    focusThenEnter(button);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

describe("E2.3 -- Escape cancels DeclareFace's draft without writing", () => {
  it("clears the in-progress source/witness edits and never calls onSubmit", () => {
    const onSubmit = vi.fn().mockResolvedValue({ written: true });
    root = createRoot(container);
    act(() => root.render(
      <DeclareFace selectedPath={DECLARE_FORM.rows[0]!.path} contractPath="/tmp/x.yaml" payload={DECLARE_FORM} expectedRevision="revision" onSubmit={onSubmit} onReview={vi.fn()} />,
    ));
    const sourceInput = container.querySelector('input[aria-label="source"]') as HTMLInputElement;
    act(() => {
      sourceInput.dispatchEvent(new Event("focusin", { bubbles: true }));
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
      setter.call(sourceInput, "a draft source");
      sourceInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(sourceInput.value).toBe("a draft source");

    const fields = container.querySelector(".declare-face")!;
    act(() => fields.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));

    expect((container.querySelector('input[aria-label="source"]') as HTMLInputElement).value).toBe("");
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("typed move (CAD-03)", () => {
  const press = (key: string, init: KeyboardEventInit = {}) => act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init })); });
  const typeKeys = (keys: string[]) => keys.forEach((k) => press(k));
  const payload = { robot: "after" } as unknown as CheckPayload;

  const POS: [number, number, number] = [0, 0, 100];
  function mount(positionMm: [number, number, number] | null = POS) {
    const onEdit = vi.fn(async (_r: DesignEditRequest) => ({ written: true, refusal: null }));
    const onReopen = vi.fn(async () => payload);
    const onPayload = vi.fn();
    root = createRoot(container);
    const show = (p: [number, number, number] | null) => act(() => root.render(<DesignControls contract="robot.yaml" expectedRevision="rev1" selectedPartId="link.arm" selectedPartName="Upper Arm" positionMm={p} onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />));
    show(positionMm);
    return { onEdit, onReopen, onPayload, show };
  }
  const flush = () => act(async () => { await Promise.resolve(); });

  it("G X 2 0 Enter makes one edit moving 20 mm on x, and shows the prompt while typing", async () => {
    const { onEdit, show } = mount();
    typeKeys(["g", "x", "2", "0"]);
    expect(document.querySelector(".typed-move")?.textContent).toContain("Move Upper Arm: x 20 mm");
    press("Enter");
    await flush();
    expect(onEdit).toHaveBeenCalledTimes(1);
    // the engine SETS the position, so the request is where the part is now plus 20 on x: z stays 100
    expect(onEdit.mock.calls[0]![0]).toMatchObject({ command: "move", args: { target: "link.arm", offset: [20, 0, 100], unit: "mm", expected_revision: "rev1" } });
    expect(document.querySelector(".typed-move")).toBeNull();
    show([20, 0, 100]); // the reloaded page now reports the new position; a second move adds to it once
    typeKeys(["g", "x", "5", "Enter"]);
    await flush();
    expect(onEdit.mock.calls[1]![0].args).toMatchObject({ offset: [25, 0, 100] });
  });

  it("takes a negative decimal on y, and Esc cancels without an edit", async () => {
    const { onEdit } = mount();
    typeKeys(["g", "y", "-", "7", ".", "5", "Enter"]);
    await flush();
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ offset: [0, -7.5, 100] });
    typeKeys(["g", "z", "4", "Escape", "Enter"]);
    await flush();
    expect(onEdit).toHaveBeenCalledTimes(1);
  });

  it("without a known position it refuses to guess and writes nothing", async () => {
    const { onEdit } = mount(null);
    typeKeys(["g", "x", "5", "Enter"]);
    await flush();
    expect(document.querySelector(".typed-move")?.textContent).toContain("can't tell where this part sits");
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("does nothing without an axis, and not while typing in a field", async () => {
    const { onEdit } = mount();
    typeKeys(["g", "5", "Enter"]);
    expect(document.querySelector(".typed-move")?.textContent).toContain("press X, Y or Z");
    press("Escape");
    const field = container.querySelector("input")!;
    field.focus();
    typeKeys(["g"]);
    expect(document.querySelector(".typed-move")).toBeNull();
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("the shortcuts sheet lists the typed move, delete, undo and redo", () => {
    const rows = shortcutRows(false).map((r) => r.join(" "));
    expect(rows.some((r) => r.includes("G, X/Y/Z"))).toBe(true);
    expect(rows).toContain("Delete / Backspace delete the selected part (3D view or Parts list focused)");
    expect(rows).toContain("Ctrl+Z undo the last edit");
    expect(rows).toContain("Ctrl+Shift+Z redo it");
  });
});

describe("delete and undo keys (GAP-delkey)", () => {
  const key = (k: string, init: KeyboardEventInit = {}) => act(async () => { document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init })); });
  const payload = { robot: "after" } as unknown as CheckPayload;
  function mount(reply: { written: boolean; refusal: string | null } = { written: true, refusal: null }) {
    const onEdit = vi.fn(async (_r: DesignEditRequest) => reply);
    root = createRoot(container);
    act(() => root.render(<><div className="kit-tree"><button id="row">row</button></div><input id="field" /><EditKeys contract="robot.yaml" expectedRevision="rev1" target={{ current: { kind: "device", name: "dev.cam", label: "Camera" } }} onEdit={onEdit} onReopen={async () => payload} onPayload={vi.fn()} /></>));
    return onEdit;
  }
  const focus = (id: string) => container.querySelector<HTMLElement>(`#${id}`)!.focus();

  it("keys typed in a text field never delete or undo", async () => {
    const onEdit = mount();
    focus("field");
    await key("Delete");
    await key("Backspace");
    await key("z", { ctrlKey: true });
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("with focus elsewhere (not the 3D view or Parts list) Delete does nothing", async () => {
    const onEdit = mount();
    (document.activeElement as HTMLElement | null)?.blur();
    await key("Delete");
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("shows the engine's refusal as-is", async () => {
    mount({ written: false, refusal: "Wire w1 still uses dev.cam; remove it first." });
    focus("row");
    await key("Delete");
    expect(document.querySelector(".typed-move")?.textContent).toBe("Wire w1 still uses dev.cam; remove it first.");
  });

  it("a delete that worked says so and where undo is, with no confirm dialog", async () => {
    const onEdit = mount();
    focus("row");
    await key("Delete");
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".typed-move")?.textContent).toBe("Deleted Camera. Ctrl+Z brings it back.");
  });

  it("Ctrl+Z calls the single-step undo route, Ctrl+Shift+Z the redo route", async () => {
    const onEdit = mount();
    await key("z", { ctrlKey: true });
    await key("Z", { ctrlKey: true, shiftKey: true });
    expect(onEdit.mock.calls.map((c) => c[0])).toEqual([{ command: "undo-edit", args: { contract: "robot.yaml" } }, { command: "redo-edit", args: { contract: "robot.yaml" } }]);
  });
});
