import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Finding } from "../shared/api.gen";
import { CheckPanel } from "./CheckPanel";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { PlanFace } from "./PlanFace";
import { ReferenceFace } from "./ReferenceFace";
import { RobotFile } from "./shown";
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Engine text naming the robot's file by its full path never reaches the screen (text or tooltip).
const C = "/Users/someone/robots/servo-bench/servo-bench.yaml";
const SWAP = `find a stronger servo: lockwire swap ${C} wrist`;
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as never;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });
const show = (node: ReactNode) => {
  act(() => root.render(<RobotFile.Provider value={C}>{node}</RobotFile.Provider>));
  act(() => { container.querySelectorAll("details").forEach((d) => { d.open = true; }); });
};
const finding: Finding = { severity: "violation", quantity: "model/joints/wrist/limits/effort", observed: "0.40 N*m", expected: "<= 0.226 N*m", action: SWAP, source: "/Users/someone/sheets/scs0009.pdf", witness: "you" };

describe("no machine path on screen", () => {
  it("Check: Fix line, raw output and the command line read relative; Copy still copies the full command", async () => {
    const writeText = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText } });
    const card = { ...X500_CHECK_PAYLOAD.cards[0]!, fix: SWAP, raw: `VIOLATION ...\n  action  ${SWAP}`, body: "", figure_note: null };
    show(<CheckPanel payload={{ ...X500_CHECK_PAYLOAD, cards: [card], cli_line: `lockwire check ${C}` }} selected={0} onSelect={() => {}} />);
    expect(container.innerHTML).not.toContain("/Users/");
    expect(container.querySelector(".finding-fix")!.textContent).toBe("find a stronger servo: lockwire swap servo-bench.yaml wrist");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-cli button")!.click());
    expect(writeText).toHaveBeenCalledWith(`lockwire check ${C}`);
  });

  it("Plan: the open finding's Fix, Source and exact finding", () => {
    show(<PlanFace payload={{ findings: [finding], finding_labels: [], steps: [] } as never} joints={[]} selected={0} onSelect={() => {}} />);
    expect(container.textContent).toContain("lockwire swap servo-bench.yaml wrist");
    expect(container.innerHTML).not.toContain("/Users/");
  });

  it("Docs: the selected plan finding and a drawer's command line", () => {
    const drawer = { title: "Engineer report", content: "report", provenance: "generated", cli_line: `lockwire report ${C} -o report.html --no-open` };
    show(<ReferenceFace payload={{ title: "Reference", drawers: [drawer, drawer], devices: [], power_tree: null } as never} selectedPlanFinding={finding} />);
    expect(container.textContent).toContain("scs0009.pdf");
    expect(container.innerHTML).not.toContain("/Users/");
  });
});
