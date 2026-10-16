import { describe, expect, it } from "vitest";

import type { Graph } from "../../shared/api.gen";
import X500 from "./fixtures/x500-check-payload.json";
import { connectionRows, connectionTubes, type Vec3 } from "./connections";

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
