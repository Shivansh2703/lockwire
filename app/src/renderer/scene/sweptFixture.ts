import type { Graph } from "../../shared/api.gen";
import X500 from "./fixtures/x500-check-payload.json";

/** The X500 power graph with `rail_sweep` figures on two rails: the servo rail (12 V, 4.8 A or so) and the telemetry rail (5 V, about 1 A). */
export const swept = (servoVolts = 10.8, radioVolts = 4.9): Graph["power"] => ({
  ...(X500.graph as Graph).power,
  nodes: (X500.graph as Graph).power.nodes.map((n) =>
    n.id === "rail.servo-motor" ? { ...n, nominal_v: 12, sweep: [{ load: 0, volts: 12 }, { load: 1, volts: servoVolts }] }
    : n.id === "rail.pixhawk-telem1-5v" ? { ...n, nominal_v: 5, sweep: [{ load: 0, volts: 5 }, { load: 1, volts: radioVolts }] }
    : n),
});
