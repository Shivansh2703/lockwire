import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Finding, ReferenceFace as ReferenceFacePayload } from "../shared/api.gen";
import { ReferenceFace } from "./ReferenceFace";

const payload: ReferenceFacePayload = {
  title: "Reference",
  drawers: [
    { title: "Reference sheets", content: "identity: robot-x\nprovenance: spec.md", provenance: "generated from contract", cli_line: "lockwire describe lockwire.yaml" },
    { title: "Engineer report", content: "report identity", provenance: "generated, deterministic", cli_line: "lockwire report lockwire.yaml -o report.html --no-open" },
    { title: "Saved baseline", content: "baseline content", provenance: "from a recording", cli_line: "lockwire baseline <recording-dir> --context <context>" },
    { title: "Hardware scan", content: "scan content", provenance: "from this machine, unreviewed", cli_line: "lockwire scan --robot robot-x" },
  ],
  devices: [{ id: "dev.camera", text: "identity: bus.usb0\nprovenance: camera.pdf\npower tree: rail.5v" }],
  power_tree: null,
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("ReferenceFace", () => {
  it("lays markdown out as rows and a table, never raw syntax", () => {
    const md = "# QARM\n\n- schema_version: 0.4\n- instance:\n  - witness: human\n\n## Joints\n\n| name | kind |\n| --- | --- |\n| j1 | revolute |";
    act(() => root.render(<ReferenceFace payload={{ ...payload, drawers: [{ ...payload.drawers[0]!, content: md }] }} />));
    const sheet = container.querySelector(".reference-drawer-content")!;
    expect(sheet.textContent).not.toMatch(/#|---|- schema/);
    expect([...sheet.querySelectorAll("dt")].map((d) => d.textContent)).toEqual(["instance", "instance · witness"]);
    expect(sheet.querySelector("td")?.textContent).toBe("j1");
  });

  it("groups devices under one searchable Devices disclosure and middle-ellipsises a long command", () => {
    const long = "lockwire describe /Users/someone/very/long/path/to/the/contract/lockwire.yaml";
    act(() => root.render(<ReferenceFace payload={{ ...payload, drawers: [{ ...payload.drawers[0]!, cli_line: long }] }} />));
    expect(container.querySelector(".kit-cli-text")!.textContent).toMatch(/^.{20}….+/);
    expect(container.querySelector(".kit-cli-text")!.textContent!.length).toBeLessThan(long.length);
    expect(container.querySelector(".reference-face-devices summary")!.textContent).toBe("Devices (1)");
    const input = container.querySelector<HTMLInputElement>('input[aria-label="Search devices"]')!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "zzz");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.querySelectorAll(".reference-device")).toHaveLength(0);
  });

  it("has no inner scroller: the panel body is the only one", () => {
    act(() => root.render(<ReferenceFace payload={payload} />));
    expect(container.querySelector(".reference-face .scroll-fade")).toBeNull();
  });

  it("renders all four backend drawers with their exact backend fields", () => {
    act(() => root.render(<ReferenceFace payload={payload} />));
    const drawers = container.querySelectorAll<HTMLElement>(".reference-drawer");
    expect(drawers).toHaveLength(4);
    payload.drawers.forEach((drawer, index) => {
      const rendered = drawers[index]!;
      expect(rendered.querySelector("summary")?.textContent).toBe(drawer.title);
      expect(rendered.querySelector<HTMLElement>(".reference-drawer summary [title]")?.title).toBe(drawer.provenance);
      expect(rendered.querySelector<HTMLElement>(".kit-cli")?.title).toBe(drawer.cli_line);
    });
  });

  it("uses native disclosures: closed by default, opening one leaves the rest closed", () => {
    act(() => root.render(<ReferenceFace payload={payload} />));
    const all = [...container.querySelectorAll<HTMLDetailsElement>(".reference-drawer details")];
    expect(all.map((d) => d.open)).toEqual([false, false, false, false]);
    act(() => { all[0]!.open = true; });
    expect(all.map((d) => d.open)).toEqual([true, false, false, false]);
  });

  it("renders no power tree when the optional backend field is null", () => {
    act(() => root.render(<ReferenceFace payload={payload} />));
    expect(container.querySelector(".reference-power-tree")).toBeNull();
  });

  it("shows the power tree once: as a sheet section, never a second standalone disclosure", () => {
    const drawers = [{ ...payload.drawers[0]!, content: "## Power tree\nrail.5v -> dev.camera" }];
    act(() => root.render(<ReferenceFace payload={{ ...payload, drawers, power_tree: "rail.5v -> dev.camera" }} />));
    expect([...container.querySelectorAll("*")].filter((e) => e.children.length === 0 && /power tree/i.test(e.textContent ?? "") && e.textContent!.trim().toLowerCase() === "power tree")).toHaveLength(1);
  });

  it("keeps the selected plan finding's citation, unsupported state, and alternatives intact", () => {
    const finding: Finding = {
      severity: "not_scored",
      quantity: "plan/steps/0/joint_target/effort",
      observed: "3 N*m requested",
      expected: "the contract declares no effort limit for this joint",
      action: "declare an effort limit before using this plan",
      source: "model/arm.urdf",
      witness: "model owner",
      locked_at: "2026-09-27",
      alternatives: [
        { text: "inspect the physical linkage", value: 1, unit: "count", kind: "physical" },
        { text: "calibrate the model", value: 1, unit: "count", kind: "model_calibration" },
        { text: "correct the design limit", value: 1, unit: "count", kind: "spec_design" },
      ],
    };
    act(() => root.render(<ReferenceFace payload={payload} selectedPlanFinding={finding} />));
    const selected = container.querySelector<HTMLElement>(".reference-plan-finding")!;
    expect(selected.querySelector("code")?.textContent).toBe(finding.quantity);
    expect(selected.querySelector<HTMLElement>(".reference-plan-finding-severity")?.dataset.severity).toBe("not_scored");
    expect(selected.querySelector(".reference-plan-finding-observed")?.textContent).toBe(finding.observed);
    expect(selected.querySelector(".reference-plan-finding-expected")?.textContent).toBe(finding.expected);
    expect(selected.querySelector(".reference-plan-finding-action")?.textContent).toBe(finding.action);
    expect(selected.textContent).toContain(finding.source);
    expect(selected.textContent).toContain(finding.witness);
    expect(selected.querySelector(".reference-plan-finding-locked-at")?.textContent).toBe(finding.locked_at);
    expect([...selected.querySelectorAll<HTMLElement>("li")].map((item) => item.dataset.kind)).toEqual([
      "physical", "model_calibration", "spec_design",
    ]);
  });
});
