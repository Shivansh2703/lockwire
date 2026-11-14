import type { PagePayload } from "../shared/api.gen";
import { Chart, Notice, Panel } from "./kit";
import { registerPanel } from "./panelRegistry";
import { pro } from "./proRegistry";
import { angleNum, angleUnit, useUnits } from "./units";

type Motor = NonNullable<PagePayload["controls_motors"]>[number];
type Joint = NonNullable<PagePayload["controls_joints"]>[number];
type Fact = Motor["facts"][number];
const motorsOf = (p: PagePayload) => p.controls_motors ?? []; // absent on a payload from before this field
const jointsOf = (p: PagePayload) => p.controls_joints ?? [];
const pairs = (xs: number[], ys: number[]): [number, number][] => xs.map((x, i) => [x, ys[i] as number]);
const show = (f: Fact) => `${Number(f.value.toPrecision(3))}${f.unit ? ` ${f.unit}` : ""}`;

function Facts({ facts }: { facts: Fact[] }) {
  return <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 12px", margin: 0, font: "400 13px var(--sans)" }}>
    {facts.map((f) => <div key={f.label} style={{ display: "contents" }}><dt>{f.label}</dt><dd style={{ margin: 0 }}>{show(f)}</dd></div>)}
  </dl>;
}

const Advice = ({ lines }: { lines: string[] }) => <>{lines.map((a) => <Notice key={a} kind="info">{a}</Notice>)}</>;

/** One motor: its datasheet numbers, then speed and efficiency against torque (copper loss only). */
function MotorCard({ m }: { m: Motor }) {
  useUnits();
  const curve = m.torque_nm.length > 1;
  return (
    <section className="controls-motor" aria-label={m.label} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <strong>{m.label}</strong>
      <Facts facts={m.facts} />
      {curve && <>
        <Chart title={`${m.label}: efficiency against torque`} xLabel="Torque" xUnit="N*m" yLabel="Efficiency" series={[{ id: "eff", label: "Efficiency", unit: "%", points: pairs(m.torque_nm, m.efficiency_pct) }]} height={180} />
        <Chart title={`${m.label}: speed against torque`} xLabel="Torque" xUnit="N*m" yLabel="Speed" series={[{ id: "speed", label: "Speed", unit: `${angleUnit()}/s`, points: pairs(m.torque_nm, m.speed_rad_s.map((v) => Number(angleNum(v, true)))) }]} height={180} />
        <span className="assistant-note">Modelled from the card with copper loss only, so real efficiency is lower.</span>
      </>}
      <Advice lines={m.advice} />
    </section>
  );
}

/** One joint: its step response to a 1 unit move, the readouts and the loop margins. */
function JointCard({ j }: { j: Joint }) {
  const end = j.t_s[j.t_s.length - 1] ?? 1;
  return (
    <section className="controls-joint" aria-label={j.label} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <strong>{j.label}</strong>
      <Chart title={`${j.label}: step response`} xLabel="Time" xUnit="s" yLabel="Position (1 is the target)" height={180}
        series={[{ id: "pos", label: "Position", points: pairs(j.t_s, j.position) },
          { id: "hi", label: "Target plus 2%", points: [[0, 1.02], [end, 1.02]] },
          { id: "lo", label: "Target minus 2%", points: [[0, 0.98], [end, 0.98]] }]} />
      <Facts facts={j.facts} />
      <Advice lines={j.advice} />
    </section>
  );
}

export function ControlsPanel({ page, contract }: { page: PagePayload; contract?: string }) {
  return (
    <Panel title="Controls">
      <div className="controls-body" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {motorsOf(page).map((m) => <MotorCard key={m.id} m={m} />)}
        {jointsOf(page).map((j) => <JointCard key={j.joint} j={j} />)}
        {pro?.ControlsMore && <pro.ControlsMore page={page} contract={contract} />}
      </div>
    </Panel>
  );
}

const hasData = (p: PagePayload) => jointsOf(p).length > 0 || motorsOf(p).some((m) => m.torque_nm.length > 1);
registerPanel({ id: "controls", label: "Controls", hasData, Body: ({ page, contract }) => <ControlsPanel page={page} contract={contract} /> });
