import { useEffect, useState } from "react";

import type { Finding, MotionJoint, MotionPreview, PlanPayload, RobotJoint } from "../shared/api.gen";
import { FindingList, findingItems, useSelection } from "./FindingList";
import type { OnJump } from "./jump";
import { Button, Chart, StatusPill, type Status } from "./kit";
import { formatValue } from "./kit/chartMath";
import { angleNum, angleUnit, useUnits } from "./units";

const STEP: Record<string, [Status, string]> = { PASS: ["go", "Pass"], FAIL: ["bad", "Fails"], UNSCORED: ["muted", "Not scored"], ERROR: ["bad", "Error"] };

/** The step kind only ("plan/steps/0/rail_load/draw" -> "Rail load"), the same label everywhere. */
const humanise = (path: string) => {
  const text = (path.split("/")[3] ?? path).replace(/[_-]/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

/** `rate`: 0 not an angle, 1 per second, 2 per second squared. The engine sends radians; the chart shows the chosen angle unit. */
type Quantity = { key: "velocity" | "acceleration" | "torque"; title: string; yLabel: string; unit: string; rate: 0 | 1 | 2 };
const QUANTITIES: Quantity[] = [
  { key: "torque", title: "Torque needed", yLabel: "Torque", unit: "N·m", rate: 0 },
  { key: "velocity", title: "Speed", yLabel: "Speed", unit: "rad/s", rate: 1 },
  { key: "acceleration", title: "Acceleration", yLabel: "Acceleration", unit: "rad/s²", rate: 2 },
];
const limitOf = (j: MotionJoint, key: Quantity["key"]) => j[`${key}_limit`];
const per = (q: Quantity) => (q.rate ? `${angleUnit()}${q.rate === 2 ? "/s²" : "/s"}` : q.unit);
const asShown = (v: number, q: Quantity) => (q.rate ? Number(angleNum(v, true)) : v); // 3 decimals in rad, 1 in deg

/** First sample, over all joints, whose value is past its own joint's limit, in words. */
function firstCrossing(m: MotionPreview, q: Quantity): string {
  for (let i = 0; i < m.time_s.length; i++) {
    const j = m.joints.find((jt) => { const lim = limitOf(jt, q.key); return lim != null && jt[q.key][i]! > lim; });
    if (j) return `${q.title} goes over its limit first at ${formatValue(m.time_s[i]!, "s")}: ${j.name} reaches ${formatValue(asShown(j[q.key][i]!, q), per(q))}, its limit is ${formatValue(asShown(limitOf(j, q.key)!, q), per(q))}.`;
  }
  return m.joints.some((j) => limitOf(j, q.key) != null) ? `${q.title} stays inside every limit.` : `No ${q.title.toLowerCase()} limit is declared.`;
}

/** The move as three charts against time, each joint's limit drawn in; a scrub bar marks "now" on all three.
 * ponytail: this scrub does not pose the 3D robot; Design's Pose sliders do (POSE-1). */
function MotionCharts({ motion }: { motion: MotionPreview }) {
  useUnits();
  const end = motion.time_s[motion.time_s.length - 1]!;
  const [now, setNow] = useState(0);
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => setNow((t) => (t + end / 60 >= end ? (setPlaying(false), end) : t + end / 60)), 50);
    return () => window.clearInterval(id);
  }, [playing, end]);
  return (
    <section className="plan-motion" aria-label="How the move runs">
      <p className="plan-total">The fastest move the limits allow, one step after another.</p>
      <div className="plan-motion-controls">
        <Button variant="quiet" onClick={() => { if (now >= end) setNow(0); setPlaying(!playing); }}>{playing ? "Pause" : "Play"}</Button>
        <input type="range" aria-label="Move to a time" min={0} max={end} step={end / 120} value={now} onChange={(e) => { setPlaying(false); setNow(Number(e.target.value)); }} />
        <span>{formatValue(now, "s")}</span>
      </div>
      {QUANTITIES.map((q) => (
        <div key={q.key}>
          <Chart
            title={q.title}
            xLabel="Time"
            xUnit="s"
            yLabel={q.yLabel}
            series={motion.joints.map((j) => ({ id: j.name, label: j.name, unit: per(q), points: motion.time_s.map((t, i) => [t, asShown(j[q.key][i]!, q)] as [number, number]) }))}
            limits={motion.joints.flatMap((j) => { const y = limitOf(j, q.key); return y == null ? [] : [{ y: asShown(y, q), label: `${j.name} limit`, kind: "max" as const }]; })}
            bands={[{ from: now, to: now + end / 100, label: "Now" }]}
            height={170}
          />
          <p className="plan-total">{firstCrossing(motion, q)}</p>
        </div>
      ))}
    </section>
  );
}

/** Plan result: finding rows like Check's, then one compact row per plan step. */
export function PlanFace({
  payload, joints = [], selected, onSelect, onSelectFinding, onJump,
}: {
  payload: PlanPayload;
  joints?: RobotJoint[];
  selected?: number | null;
  onSelect?: (index: number | null) => void;
  onSelectFinding?: (finding: Finding) => void;
  onJump?: OnJump;
}) {
  const [sel, setSel] = useSelection(selected, onSelect);
  // A not-scored finding at a step path is the step's own row (with its reason); drop the finding row.
  // Items keep their engine index, so selection still speaks the unfiltered list.
  const stepPaths = new Set((payload.steps ?? []).map((st) => st.path));
  const dup = (f: Finding) => f.severity === "not_scored" && stepPaths.has(f.quantity);
  const items = findingItems(payload.findings, joints, onSelectFinding, payload.finding_labels, onJump, true).filter((it) => !dup(payload.findings[it.index!]!));
  // One row per step kind and status, unless a kept finding already names the step.
  const seen = new Set(payload.findings.filter((f) => !dup(f)).map((f) => f.quantity));
  const rows = (payload.steps ?? []).filter((step) => {
    const key = `${step.path.split("/").slice(0, 4).join("/")}/${step.status}`;
    if (seen.has(step.path) || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  // Count what the rows show: one step per kind and status (two servos on one rail are one finding row).
  const kinds = new Set<string>();
  const steps = (payload.steps ?? []).filter((st) => { const k = `${st.path.split("/")[3]}/${st.status}`; return !kinds.has(k) && kinds.add(k); });
  const count = (status: string) => steps.filter((st) => st.status === status).length;
  const parts = [["pass", count("PASS")], ["failed", count("FAIL") + count("ERROR")], ["not scored", count("UNSCORED")]] as const;
  const total = steps.length === 0 ? null
    : `${steps.length} step${steps.length > 1 ? "s" : ""} · ${parts.every(([w, n]) => w === "pass" || !n) ? "all pass" : parts.filter(([, n]) => n).map(([w, n]) => `${n} ${w}`).join(", ")}`;
  return (
    <>
      <FindingList items={items} selected={sel} onSelect={setSel} />
      {rows.map((step) => {
        const [status, word] = STEP[step.status] ?? ["muted", step.status];
        const inner = (
          <>
            <span className="plan-outcome-path">{humanise(step.path)}</span>
            <StatusPill status={status}>{word}</StatusPill>
            {step.not_scored_reason && <span className="plan-outcome-reason">{step.not_scored_reason}</span>}
          </>
        );
        // A step row lights the parts it touches (or opens its quantity in Declare, when it is one).
        const row = onJump && step.targets?.length
          ? <button type="button" className="plan-outcome-row plan-outcome-btn" data-status={step.status} key={step.path} title={step.path} aria-label={`Show ${humanise(step.path)}`} onClick={() => onJump({ path: step.path, targets: step.targets ?? [] })}>{inner}</button>
          : <div className="plan-outcome-row" data-status={step.status} key={step.path} title={step.path}>{inner}</div>;
        // A gap is closed in Declare: one link from the reason to the page that fills it (after the catch).
        return step.status === "UNSCORED" && onJump
          ? <div className="plan-outcome-wrap" key={step.path}>{row}<Button variant="quiet" onClick={() => onJump({ declare: true, targets: step.targets ?? [] })}>Declare it</Button></div>
          : row;
      })}
      {total && <p className="plan-total">{total}</p>}
      {payload.motion && <MotionCharts motion={payload.motion} />}
    </>
  );
}
