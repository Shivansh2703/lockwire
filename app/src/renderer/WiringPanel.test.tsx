import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import type { PagePayload } from "../shared/api.gen";
import { visiblePanels } from "./panelRegistry";
import { WiringPanel, netOf, pinsCsv, type AddWire } from "./WiringPanel";

let root: Root;
let container: HTMLDivElement;
afterEach(() => { act(() => root.unmount()); container.remove(); });

const pin = (d: string, c: string, n: string, name = "") => ({ key: `${d}|${c}|${n}`, label: name ? `${n} ${name}` : n });
const wire = (id: string, from_pin: string, to_pin: string, rail: string | null = null, net: string | null = null) => ({ id, name: id, from_pin, to_pin, rail, rail_label: rail && "Rail 5 V", net });
// FC J1 pin 1 feeds the radio (power); pin 2 talks to its pin 7 (signal); the GPS has its own unrelated wire.
const page = {
  wiring_devices: [
    { id: "fc", name: "Flight controller", connectors: [{ name: "TELEM1", pins: [pin("fc", "J1", "1", "VCC"), pin("fc", "J1", "2", "TX")] }] },
    { id: "radio", name: "Telemetry radio", connectors: [{ name: "UART", pins: [pin("radio", "J2", "6", "VCC"), pin("radio", "J2", "7", "RX")] }] },
    { id: "gps", name: "GPS", connectors: [{ name: "Port", pins: [pin("gps", "J3", "1"), pin("gps", "J3", "2")] }] },
  ],
  wiring_wires: [wire("wire.feed", "fc|J1|1", "radio|J2|6", "rail.5v"), wire("wire.tx", "fc|J1|2", "radio|J2|7"), wire("wire.gps", "gps|J3|1", "gps|J3|2", null, "loop")],
} as unknown as PagePayload;
// A Pi header declared out of numeric order on purpose: an exporter that sorts would break the round trip.
const piPins = [
  { number: "8", name: "GPIO14", functions: ["TXD"], aliases: ["BCM14", "UART0_TX"], capabilities: [], os_device: "/dev/ttyAMA0" },
  { number: "10", name: "GPIO15", functions: ["RXD"], aliases: ["BCM15"], capabilities: ["pull, up"], os_device: null },
  { number: "1", name: "3V3", functions: [], aliases: [], capabilities: [], os_device: null },
];
const piPage = { wiring_devices: [{ id: "pi", name: "Raspberry Pi", connectors: [{ name: "GPIO header", type: "2x20", pins: [pin("pi", "J8", "8", "GPIO14")], card: piPins }] }, { id: "esc", name: "Motor driver", connectors: [{ name: "In", pins: [pin("esc", "J1", "1")], card: [] }] }], wiring_wires: [wire("w", "pi|J8|8", "esc|J1|1")] } as unknown as PagePayload;
const mount = (p: PagePayload = page, onAddWire?: AddWire) => { container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container); act(() => root.render(<WiringPanel page={p} onAddWire={onAddWire} />)); };
const q = (s: string) => Array.from(container.querySelectorAll<SVGElement>(s));
const hover = (key: string) => act(() => { q(`[data-pin="${key}"]`)[0]!.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })); });
const dim = (id: string) => q(`[data-wire="${id}"]`)[0]!.getAttribute("opacity");

describe("WiringPanel", () => {
  it("draws one block per device and one edge per wire, with plain pin names", () => {
    mount();
    expect(q(".wiring-device").map((g) => g.dataset.device)).toEqual(["fc", "radio", "gps"]);
    expect(q("path[data-wire]")).toHaveLength(3);
    expect(container.textContent).toContain("Flight controller");
    expect(container.textContent).toContain("1 VCC");
  });
  it("draws a power wire as a rail edge and a signal wire as a signal edge", () => {
    mount();
    expect(q("[data-wire='wire.feed']")[0]!.getAttribute("data-kind")).toBe("rail");
    expect(q("[data-wire='wire.tx']")[0]!.getAttribute("data-kind")).toBe("signal");
    expect(q("[data-wire='wire.feed']")[0]!.getAttribute("stroke-width")).not.toBe(q("[data-wire='wire.tx']")[0]!.getAttribute("stroke-width"));
  });
  it("lights the whole net of a hovered pin and dims the rest", () => {
    mount();
    hover("fc|J1|1");
    expect([dim("wire.feed"), dim("wire.tx"), dim("wire.gps")]).toEqual(["1", "0.15", "0.15"]);
    expect(container.textContent).toContain("2 pins on this net.");
  });
  it("joins wires that share a net name into one net", () => {
    const w = [wire("a", "x|1", "y|1", null, "uart"), wire("b", "p|1", "q|1", null, "uart"), wire("c", "m|1", "n|1")];
    expect([...netOf(w, "x|1")].sort()).toEqual(["p|1", "q|1", "x|1", "y|1"]);
  });
  it("is registered as the Wiring panel and hidden until the robot has wires", () => {
    expect(visiblePanels(page).map((p) => p.id)).toContain("wiring");
    expect(visiblePanels({ wiring_devices: [], wiring_wires: [] } as unknown as PagePayload).map((p) => p.id)).not.toContain("wiring");
  });
  it("shows the panel for a robot with connector pins and no wires yet", () => {
    const bare = { wiring_devices: page.wiring_devices, wiring_wires: [] } as unknown as PagePayload;
    expect(visiblePanels(bare).map((p) => p.id)).toContain("wiring");
  });
  describe("click to wire", () => {
    const tap = (key: string) => act(async () => { q(`[data-pin="${key}"]`)[0]!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    const esc = (key: string) => act(() => { q(`[data-pin="${key}"]`)[0]!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    const setup = (reply: string | null = null) => { const calls: [string, string, string][] = []; mount(page, async (...a) => { calls.push(a); return reply; }); return calls; };
    it("two clicks write one wire, endpoint A then B, and the redrawn page shows it", async () => {
      const calls = setup();
      await tap("fc|J1|1"); await tap("gps|J3|2");
      expect(calls).toEqual([["wire.fc-gps", "fc|J1|1", "gps|J3|2"]]);
      const more = { ...page, wiring_wires: [...page.wiring_wires!, wire("wire.fc-gps", "fc|J1|1", "gps|J3|2")] } as unknown as PagePayload;
      act(() => root.render(<WiringPanel page={more} onAddWire={async () => null} />));
      expect(q("path[data-wire]")).toHaveLength(4);
    });
    it("keeps the order the clicks came in", async () => {
      const calls = setup();
      await tap("gps|J3|2"); await tap("fc|J1|1");
      expect(calls[0]!.slice(1)).toEqual(["gps|J3|2", "fc|J1|1"]);
    });
    it("Esc, or a second click on pin A, cancels without writing", async () => {
      const calls = setup();
      await tap("fc|J1|1");
      expect(container.textContent).toContain("Esc cancels");
      await esc("fc|J1|1");
      await tap("fc|J1|1"); await tap("fc|J1|1");
      expect(calls).toHaveLength(0);
      expect(container.textContent).not.toContain("Esc cancels");
    });
    it("refuses two pins on one device and an already wired pair, in one sentence, writing nothing", async () => {
      const calls = setup();
      await tap("fc|J1|1"); await tap("fc|J1|2");
      expect(container.querySelector("[role=alert]")!.textContent).toBe("Both pins are on the same part. Pick a pin on a different part.");
      await tap("radio|J2|6"); // still pin A is fc 1: the feed wire joins them
      expect(container.querySelector("[role=alert]")!.textContent).toBe("These two pins are already wired together.");
      expect(calls).toHaveLength(0);
    });
    it("shows the engine's refusal as it came", async () => {
      mount(page, async () => "The engine said no.");
      await tap("fc|J1|1"); await tap("gps|J3|2");
      expect(container.querySelector("[role=alert]")!.textContent).toBe("The engine said no.");
    });
  });
  describe("live checks while wiring (WIR-09)", () => {
    // The same numbers tests/test_pins.py feeds the engine: a 5 V source pin, a 0 V pin, and a 3.3 V logic pin that takes 5.5 V.
    const fact = (key: string, level_v: number, limit_v: number, drives: boolean) => ({ key, level_v, limit_v, drives, receives: !drives });
    const live = { ...page, wiring_pins: [fact("fc|J1|1", 5, 5.3, true), fact("gps|J3|1", 0, 0.3, false), fact("gps|J3|2", 3.3, 5.5, false)], wiring_findings: [] } as unknown as PagePayload;
    const tap = async (key: string) => { await act(async () => { q(`[data-pin="${key}"]`)[0]!.dispatchEvent(new MouseEvent("click", { bubbles: true })); }); };
    const buttons = () => Array.from(container.querySelectorAll("button")).map((b) => b.textContent);
    const setup = (p: PagePayload = live) => { const calls: string[][] = []; mount(p, async (...a) => { calls.push(a); return null; }); return calls; };
    it("flags power to ground as soon as pin B is hovered, before anything is saved", async () => {
      const calls = setup();
      await tap("fc|J1|1"); hover("gps|J3|1");
      expect(container.querySelector(".wiring-short")!.textContent).toBe("This would short 5 V from Flight controller 1 VCC to ground at GPS 1.");
      expect(calls).toHaveLength(0);
    });
    it("needs a second click on Wire it anyway before a flagged wire is written", async () => {
      const calls = setup();
      await tap("fc|J1|1"); await tap("gps|J3|1");
      expect(calls).toHaveLength(0);
      expect(buttons()).toContain("Wire it anyway");
      await act(async () => { Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Wire it anyway")!.click(); });
      expect(calls).toEqual([["wire.fc-gps", "fc|J1|1", "gps|J3|1"]]);
    });
    it("Do not wire drops the flagged wire and writes nothing", async () => {
      const calls = setup();
      await tap("fc|J1|1"); await tap("gps|J3|1");
      act(() => { Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Do not wire")!.click(); });
      expect(calls).toHaveLength(0);
      expect(container.querySelector(".wiring-short")).toBeNull();
    });
    it("a safe pair is written on the second click with no warning", async () => {
      const calls = setup();
      await tap("fc|J1|1"); hover("gps|J3|2");
      expect(container.querySelector(".wiring-short")).toBeNull();
      await tap("gps|J3|2");
      expect(calls).toHaveLength(1);
    });
    it("shows the engine's pin findings beside the diagram, and says so when the re-check is clean", async () => {
      const bad = { ...live, wiring_findings: [{ wire: "wire.feed", wire_name: "Feed", problem: "1 is 0 V; reg 1 drives 5 V", limit: "<= 0.3 V" }] } as unknown as PagePayload;
      mount(bad);
      expect(container.querySelector(".wiring-findings")!.textContent).toContain("Feed: 1 is 0 V; reg 1 drives 5 V. Expected <= 0.3 V.");
      act(() => root.unmount()); container.remove();
      const calls = setup(); await tap("fc|J1|1"); await tap("gps|J3|2");
      expect(calls).toHaveLength(1);
      expect(container.querySelector(".wiring-findings")!.textContent).toContain("No pin findings");
    });
  });
  it("the diagram svg has no height=auto attribute, which the browser logs as an error", () => {
    mount();
    expect(q("svg.wiring-diagram")[0]!.getAttribute("height")).toBeNull();
  });
  describe("pin card", () => {
    const click = (sel: string) => act(() => { q(sel)[0]!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    const search = (v: string) => act(() => { const i = container.querySelector("input")!; Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(i, v); i.dispatchEvent(new Event("input", { bubbles: true })); });
    const rows = () => Array.from(container.querySelectorAll("tbody tr")).map((r) => r.textContent);
    const openPi = () => { mount(piPage); click("[data-device='pi']"); };
    const parse = (csv: string) => { // a reader of the file format, independent of the exporter
      const out: string[][] = []; let row: string[] = [], f = "", quoted = false;
      for (let i = 0; i < csv.length; i++) { const c = csv[i]!;
        if (quoted) { if (c === '"' && csv[i + 1] === '"') { f += '"'; i++; } else if (c === '"') quoted = false; else f += c; }
        else if (c === '"') quoted = true; else if (c === ",") { row.push(f); f = ""; } else if (c === "\n") { row.push(f); out.push(row); row = []; f = ""; } else f += c; }
      return out;
    };
    it("opens from a click on a device, in the same panel, showing name, pin number, functions, aliases and device file", () => {
      openPi();
      expect(q("svg.wiring-diagram")).toHaveLength(0);
      const text = container.textContent ?? "";
      for (const want of ["Raspberry Pi", "GPIO14", "TXD", "BCM14", "/dev/ttyAMA0", "GPIO header (2x20)"]) expect(text).toContain(want);
      expect(rows()[0]).toMatch(/^8GPIO14/);
      act(() => { Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Back to diagram")!.click(); });
      expect(q("svg.wiring-diagram")).toHaveLength(1);
    });
    it("searches names, functions and aliases", () => {
      openPi();
      search("bcm15");
      expect(rows()).toHaveLength(1);
      expect(rows()[0]).toContain("GPIO15");
      search("rxd");
      expect(rows()[0]).toContain("GPIO15");
      search("nothing here");
      expect(rows()).toHaveLength(0);
      expect(container.textContent).toContain("No pin matches");
    });
    it("says so when a device has no pins listed", () => {
      mount(piPage); click("[data-device='esc']");
      expect(container.textContent).toContain("has not listed any pins for Motor driver yet. Pins come from the robot file.");
      expect(container.textContent).not.toContain("Export CSV");
    });
    it("exports every pin in declared order and reads back exactly", () => {
      const rowsBack = parse(pinsCsv(piPage.wiring_devices![0]!));
      expect(rowsBack[0]).toEqual(["connector", "pin", "name", "aliases", "functions", "capabilities", "device file"]);
      expect(rowsBack.slice(1).map((r) => r[1])).toEqual(["8", "10", "1"]);
      expect(rowsBack.slice(1).map((r) => ({ number: r[1], name: r[2], aliases: r[3] ? r[3].split(";") : [], functions: r[4] ? r[4].split(";") : [], capabilities: r[5] ? r[5].split(";") : [], os_device: r[6] || null }))).toEqual(piPins);
    });
  });
});
