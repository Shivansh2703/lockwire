import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PagePayload } from "../shared/api.gen";

let price: number | undefined;
vi.mock("./proRegistry", () => ({ pro: { bomColumn: { label: "Cost", cell: (n: { id: string }) => (n.id === "dev.a" && price !== undefined ? "$" + price : null) } } }));
const { BomPanel, bomCsv, bomRows } = await import("./BomPanel");
const { visiblePanels } = await import("./panelRegistry");

let root: Root;
let container: HTMLDivElement;
afterEach(() => { act(() => root.unmount()); container.remove(); price = undefined; });

const node = (id: string, kind: string, sub = "") => ({ id, label: id, sub, kind });
const page = { graph: {
  mechanical: { nodes: [node("base", "link"), node("dev.a", "device", "mass unknown")] },
  power: { nodes: [node("rail.5v", "rail", "5 V"), node("dev.a", "device", "7 W")] },
  data: { nodes: [node("pi5", "host"), node("bus.ttl", "bus", "serial")] },
}, faces: { reference: { devices: [{ id: "dev.a", name: "SERVO1", text: "" }] } }, power_stages: [{ rail: "rail.5v", label: "Rail 5 V" }] } as unknown as PagePayload;
const mount = (p: PagePayload) => { container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container); act(() => root.render(<BomPanel page={p} />)); };
const bodyRows = () => Array.from(container.querySelectorAll("tbody tr")).map((r) => Array.from(r.children).map((c) => c.textContent));

describe("BomPanel", () => {
  it("lists parts, then rails and buses beside them, each part once, no links or hosts", () => {
    mount(page);
    expect(bodyRows()).toEqual([["SERVO1", "Part", "7 W"], ["Rail 5 V", "Power rail", "5 V"], ["bus.ttl", "Data bus", "serial"]]);
  });
  it("shows the Pro cost column only when some part has a price", () => {
    mount(page);
    expect(container.querySelector("th:nth-child(4)")).toBeNull();
    act(() => root.unmount()); container.remove();
    price = 12.5;
    mount(page);
    expect(bodyRows()[0]![3]).toBe("$12.5");
    expect(bodyRows()[1]![3]).toBe("");
  });
  it("shows names, never an internal id prefix, when a name exists", () => {
    mount(page);
    const items = bodyRows().map((r) => r[0]);
    expect(items.slice(0, 2).some((t) => /^(dev|rail)\./.test(t ?? ""))).toBe(false);
    expect(items[2]).toBe("bus.ttl"); // no declared name for a bus: the id is the fallback
  });
  it("the CSV carries the same rows and quotes commas", () => {
    expect(bomCsv(page).split("\n")[0]).toBe("Item,Kind,Detail,Id,Cost");
    expect(bomCsv(page)).toContain("Rail 5 V,Power rail,5 V,rail.5v,");
    expect(bomCsv({ graph: { power: { nodes: [node("r", "rail", "a, b")] } } } as unknown as PagePayload)).toContain('"a, b"');
    container = document.createElement("div"); root = createRoot(container); // afterEach needs one
  });
  it("the panel is offered only when there is something to list", () => {
    expect(visiblePanels(page).map((p) => p.id)).toContain("parts");
    expect(bomRows({ graph: {} } as unknown as PagePayload)).toEqual([]);
    expect(visiblePanels({ graph: {} } as unknown as PagePayload).map((p) => p.id)).not.toContain("parts");
    container = document.createElement("div"); root = createRoot(container);
  });
});
