// Not shipped in the app UI: only the screenshot script (scripts/chart-demo) mounts this.
import { createRoot } from "react-dom/client";

import { Chart } from "./Chart";

const wave = (n: number, f: (t: number) => number): [number, number][] => Array.from({ length: n }, (_, i) => [i, f(i)]);

function Demo() {
  return (
    <>
      <section className="demo-card">
        <Chart
          title="Shoulder motor current over the move"
          xLabel="Time" xUnit="s" yLabel="Current (A)"
          series={[{ id: "i", label: "Shoulder", unit: "A", points: wave(60, (t) => 1.4 + 0.5 * Math.sin(t / 5) + (t > 38 && t < 46 ? 1.1 : 0)) }]}
          limits={[{ y: 2.4, label: "Motor max 2.4 A", kind: "max" }]}
        />
      </section>
      <section className="demo-card">
        <Chart
          title="Power by rail during the mission"
          xLabel="Time" xUnit="min" yLabel="Power (W)"
          series={[
            { id: "a", label: "Drive", unit: "W", points: wave(40, (t) => 80 + 30 * Math.sin(t / 4)) },
            { id: "b", label: "Arm", unit: "W", kind: "step", points: wave(40, (t) => 20 + 15 * Math.floor(t / 10)) },
            { id: "c", label: "Compute", unit: "W", points: wave(40, () => 35) },
          ]}
          bands={[{ from: 0, to: 15, label: "Approach" }, { from: 15, to: 39, label: "Pick and place" }]}
        />
      </section>
      <section className="demo-card">
        <Chart
          title="Battery voltage during the run"
          xLabel="Time" xUnit="s" yLabel="Voltage (V)"
          series={[{ id: "v", label: "Battery", unit: "V", points: wave(50, (t) => 12.4 - t * 0.02 - (t > 30 && t < 38 ? 1.2 : 0)) }]}
          limits={[{ y: 11.5, label: "Brown-out 11.5 V", kind: "min" }]}
        />
      </section>
      <section className="demo-card">
        <Chart
          title="Fuse current, limit on the top tick"
          xLabel="Time" xUnit="s" yLabel="Current (A)"
          series={[{ id: "f", label: "Fuse", unit: "A", points: wave(30, (t) => 4 + 6 * Math.sin(t / 6) ** 2) }]}
          limits={[{ y: 10, label: "Fuse 10 A", kind: "max" }]}
        />
      </section>
      <section className="demo-card">
        <Chart title="Nothing recorded" xLabel="Time" yLabel="Torque" series={[]} />
      </section>
    </>
  );
}

createRoot(document.getElementById("root")!).render(<Demo />);
