import { describe, expect, it } from "vitest";

import type { Graph } from "../../shared/api.gen";
import X500 from "./fixtures/x500-check-payload.json";
import { swept } from "./sweptFixture";
import { connectionRows, connectionTubes, DASH_RATE, sagHeat, sagRows, type Vec3 } from "./connections";

const graph = X500.graph as Graph;
const at = (id: string): Vec3 | undefined => (id.startsWith("battery") || id.endsWith("-computer") || id === "flight-controller" ? undefined : [id.length, 0, 0]);

describe("connection rows", () => {
  it("power: battery -> rails -> loads, the overloaded rail red with used of capacity", () => {
    const rows = connectionRows(graph.power, "power");
    const rail = rows.find((r) => r.id === "rail.pm02-5v")!;
    expect(rows[0]!.id).toBe("battery");
    expect(rail).toMatchObject({ depth: 1, meta: "35.2 / 15.6 W", state: "bad" });
    expect(rail.members).toEqual(expect.arrayContaining(["dev.companion.jetson-orin-nano", "dev.camera.oak-d-lite", "rail.pixhawk-telem1-5v", "dev.radio.rfd900x"]));
    expect(rows.find((r) => r.id === "dev.companion.jetson-orin-nano")).toMatchObject({ depth: 2, meta: "25 W" });
    expect(rows.find((r) => r.id === "rail.servo-motor")!.state).toBeUndefined();
  });
  it("data shows link speed; mechanical says mass unknown in amber", () => {
    expect(connectionRows(graph.data, "data").find((r) => r.id === "bus.uart.companion")!.meta).toBe("serial 921.6 kbit/s");
    expect(connectionRows(graph.mechanical, "mechanical").find((r) => r.id === "dev.camera.oak-d-lite")).toMatchObject({ meta: "mass unknown", state: "warn" });
  });
});

describe("connection tubes", () => {
  it("one tube per placed edge, thicker for more watts, red off the overloaded rail", () => {
    const tubes = connectionTubes(graph.power, at, 1);
    expect(tubes).toHaveLength(graph.power.edges.length);
    const r = (to: string) => tubes.find((t) => t.to === to)!;
    expect(r("dev.motor.m1").radius).toBeGreaterThan(r("dev.camera.oak-d-lite").radius);
    expect(Math.max(...tubes.map((t) => t.radius))).toBeLessThanOrEqual(0.008);
    expect(Math.min(...tubes.map((t) => t.radius))).toBeGreaterThanOrEqual(0.002);
    expect(tubes.filter((t) => t.over).map((t) => t.to).sort()).toEqual(["dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "rail.pixhawk-telem1-5v"]);
  });
  it("an edge with an end the robot does not draw gets no tube", () => {
    expect(connectionTubes(graph.data, at, 1)).toHaveLength(5); // bus -> device only; hosts have no part
  });
});

const tube = (g: Graph["power"], to: string) => connectionTubes(g, at, 1).find((t) => t.to === to)!;

describe("voltage colours and current flow (PWR-15u)", () => {
  it("a swept rail's wires carry the payload's sagged fraction of nominal, never the nominal itself", () => {
    expect(tube(swept(9), "dev.motor.m1").sag).toBeCloseTo(0.75);
    expect(tube(swept(11.4), "dev.motor.m1").sag).toBeCloseTo(0.95);
    expect(sagRows(swept(9)).find((r) => r.id === "rail.servo-motor")).toMatchObject({ nominal: 12, volts: 9, sag: 0.75 });
  });
  it("the colour scale runs from nominal (0) to a quarter low (1): more sag is hotter", () => {
    expect(sagHeat(tube(swept(12), "dev.motor.m1").sag!)).toBe(0);
    expect(sagHeat(tube(swept(9), "dev.motor.m1").sag!)).toBe(1);
    expect(sagHeat(tube(swept(10.8), "dev.motor.m1").sag!)).toBeGreaterThan(sagHeat(tube(swept(11.4), "dev.motor.m1").sag!));
  });
  it("dashes go faster for more current, inside the rate range", () => {
    const g = swept();
    const [motor, radio] = [tube(g, "dev.motor.m1").rate!, tube(g, "dev.radio.rfd900x").rate!];
    expect(motor).toBeGreaterThan(radio); // 61.67 W at 12 V against 5.2 W at 5 V
    expect(motor).toBe(DASH_RATE[1]);
    expect(radio).toBeGreaterThan(DASH_RATE[0]);
  });
  it("rails with no sweep are as before: no sag, no dashes, nothing in the legend", () => {
    const plain = connectionTubes(graph.power, at, 1);
    expect(plain.every((t) => t.sag === undefined && t.rate === undefined)).toBe(true);
    const g = swept();
    expect(tube(g, "dev.camera.oak-d-lite")).toEqual(plain.find((t) => t.to === "dev.camera.oak-d-lite"));
    expect(sagRows(g).map((r) => r.id).sort()).toEqual(["rail.pixhawk-telem1-5v", "rail.servo-motor"]);
    expect(sagRows(graph.power)).toEqual([]);
  });
  it("the colour does not depend on where the parts are (posed or not)", () => {
    const moved = connectionTubes(swept(9), (id) => (at(id) ? [1, 2, 3] : undefined), 1).find((t) => t.to === "dev.motor.m1")!;
    expect(moved.sag).toBe(tube(swept(9), "dev.motor.m1").sag);
  });
});
