import { describe, expect, it } from "vitest";

import type { RobotJoint } from "../../shared/api.gen";
import { highlightsFromDevices } from "./RunPanel";

const joint = (name: string) => ({ name }) as RobotJoint;
const dev = (id: string, state: string, drives_joint: string | null) => ({ id, state, drives_joint }) as never;

describe("highlightsFromDevices", () => {
  it("lights the joint an unhealthy device drives, by the contract's drives_joint not by name, bad outranking warn", () => {
    const joints = [joint("shoulder"), joint("elbow")];
    expect(highlightsFromDevices([
      dev("dev.motor_a", "faulty", "shoulder"), dev("dev.motor_b", "degrading", "shoulder"),
      dev("dev.elbow", "faulty", null), dev("dev.motor_c", "healthy", "elbow"), dev("dev.motor_d", "faulty", "gone"),
    ], joints)).toEqual([{ id: "shoulder", level: "bad" }, { id: "elbow", level: "pass" }]); // a healthy device lights green
  });
  it("lights the part a mesh-less device is mounted on", () => {
    expect(highlightsFromDevices([{ id: "dev.imu", kind: "device", state: "faulty", mount_frame: "base" } as never], [])).toContainEqual({ id: "base", level: "bad" });
  });
  it("does not light the mount of a device that has its own scene part", () => {
    expect(highlightsFromDevices([{ id: "dev.imu", kind: "device", state: "faulty", mount_frame: "base" } as never], [], ["dev.imu"])).toEqual([{ id: "dev.imu", level: "bad" }]);
  });
  it("lights a healthy device green, a watched one only while a run goes", () => {
    const w = [{ id: "dev.imu", kind: "device", state: "watched" } as never];
    expect(highlightsFromDevices(w, [])).toEqual([]);
    expect(highlightsFromDevices(w, [], [], true)).toEqual([{ id: "dev.imu", level: "selected" }]);
    expect(highlightsFromDevices([{ id: "dev.imu", kind: "device", state: "not_watched" } as never], [], [], true)).toEqual([]); // not_watched = nothing compared: never lit, even while running
    expect(highlightsFromDevices([{ id: "dev.imu", kind: "device", state: "not_watched" } as never], [], [], true, ["dev.imu"])).toEqual([{ id: "dev.imu", level: "selected" }]); // the run's own list of devices it records lights them before samples arrive
  });
});
