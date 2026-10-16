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

import { DeclareFace, type DeclareFormPayload } from "./DeclareFace";
import { OpenFace } from "./OpenFace";
import { PlanFace } from "./PlanFace";
import { ReferenceFace } from "./ReferenceFace";
import { Shell } from "./Shell";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import type { Finding, PlanPayload } from "../shared/api.gen";

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
