import { useEffect, useRef, useState } from "react";

import type { RobotJoint } from "../../shared/api.gen";
import type { RunStatePayload } from "../../shared/pro/types";
import { proApi } from "./api";
import { DEVICE_STATUS } from "./DeviceRows";
import { Button, Disclosure, EmptyState, Field, FilePick, JobBar, Notice, Panel, Segmented, StatusPill, type Status } from "../kit";
import { RunFace } from "./RunFace";
import type { Highlight } from "../scene/robotScene";

type Source = "recording" | "docker" | "ssh";
const SOURCES: { value: Source; label: string }[] = [
  { value: "recording", label: "Recording" }, { value: "docker", label: "Docker" }, { value: "ssh", label: "SSH" },
];
const VERDICT: Record<string, [Status, string]> = {
  GO: ["go", "GO"], GO_WITH_NOTES: ["warn", "GO with notes"], "NO-GO": ["bad", "NO-GO"], REFUSED: ["bad", "REFUSED"], NOT_SCORED: ["muted", "Not scored"],
};
const JOB: Record<RunStatePayload["status"], [Status, string]> = {
  running: ["warn", "Running"], completed: ["go", "Done"], failed: ["bad", "Failed"], cancelled: ["muted", "Cancelled"],
};
// The engine requires identity, known hosts, target and image for a live SSH run (jobs.py), so
// the secondary ones stay required: they sit under "More", never dropped.
const SSH_MAIN = [["host", "Host", "SSH host", "robot.local"], ["user", "User", "SSH user"], ["identity", "Key file", "SSH private key file", "~/.ssh/id_ed25519"]] as const;
const SSH_MORE = [
  ["port", "Port", "SSH port"], ["knownHosts", "Known hosts", "SSH known hosts file", "~/.ssh/known_hosts"],
  ["target", "Target", "SSH declared target"], ["image", "Image", "SSH declared image"],
  ["keepBag", "Keep capture", "SSH keep bag"],
] as const;
const SSH_FIELDS = [...SSH_MAIN, ...SSH_MORE] as const;
type SshKey = (typeof SSH_FIELDS)[number][0];

/** The newest run time any device reports, as local "HH:MM" (shown once, in the JobBar). */
function updatedAt(devices: RunStatePayload["health"]["devices"]): string {
  const newest = devices.map((d) => Date.parse(d.run ?? "")).filter((t) => !Number.isNaN(t)).sort((a, b) => b - a)[0];
  return newest === undefined ? "" : new Date(newest).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** An unhealthy device lights the joint the contract says it drives (devices[].drives_joint) and
 * its own mesh part (a scene part's id is the device id, scene_view.resolve_scene) and the part it
 * is mounted on (mount_frame), only for a device with no scene part of its own. */
export function highlightsFromDevices(devices: RunStatePayload["health"]["devices"], joints: RobotJoint[], sceneIds: string[] = [], running = false, watching: string[] = []): Highlight[] {
  const out = new Map<string, Highlight["level"]>();
  for (const d of devices) {
    const status = DEVICE_STATUS[d.state as keyof typeof DEVICE_STATUS];
    // Green only for a device that reported healthy; while a run goes, a watched device shows as selected (what is being watched); not_watched and absent mean nothing was compared, so it stays plain; the rest stay plain.
    const level: Highlight["level"] | null = status === "warn" || status === "bad" ? status : d.state === "healthy" ? "pass" : running && (d.state === "watched" || watching.includes(d.id)) ? "selected" : null;
    if (!level) continue;
    const ids = [...(d.drives_joint && joints.some((j) => j.name === d.drives_joint) ? [d.drives_joint] : []), ...(d.kind === "device" ? [d.id, ...(d.mount_frame && !sceneIds.includes(d.id) ? [d.mount_frame] : [])] : [])];
    for (const id of ids) if (out.get(id) !== "bad" && !(level !== "warn" && level !== "bad" && out.has(id))) out.set(id, level);
  }
  return [...out].map(([id, level]) => ({ id, level }));
}

// Run: pick one source, Start, watch one job bar. Mounted for the whole session
// (`active` only hides it) so a running job and half-typed fields survive a tab switch.
export function RunPanel({
  active, contract, joints, sceneIds, onHighlight,
}: {
  active: boolean;
  contract?: string;
  joints: RobotJoint[];
  sceneIds?: string[];
  onHighlight: (h: Highlight[]) => void;
}) {
  const [source, setSource] = useState<Source>("recording");
  const [chosen, setChosen] = useState(false); // the hint shows only until a source is picked
  const [recording, setRecording] = useState<string | null>(null);
  const [container, setContainer] = useState("");
  const [ssh, setSsh] = useState<Record<SshKey, string>>({
    host: "", port: "22", user: "root", identity: "", knownHosts: "", target: "", image: "", keepBag: "",
  });
  const [state, setState] = useState<RunStatePayload | null>(null);
  const [ranSource, setRanSource] = useState<Source | null>(null); // a result belongs to the source it ran on, so a tab looks the same whenever you arrive at it
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState<Set<string>>(new Set());

  const port = Number(ssh.port);
  // Required SSH fields in form order; the engine refuses a live run without any of them.
  const SSH_REQUIRED = ["host", "user", "identity", "knownHosts", "target", "image"] as const;
  const labelOf = (k: SshKey) => SSH_FIELDS.find((f) => f[0] === k)![1];
  const missing: string[] =
    source === "recording" ? (recording ? [] : ["Recording"])
    : source === "docker" ? (container.trim() ? [] : ["Container"])
    : [
        ...SSH_REQUIRED.filter((k) => !ssh[k].trim()).map(labelOf),
        ...(Number.isInteger(port) && port >= 1 && port <= 65535 ? [] : ["Port"]),
      ];
  const ready = missing.length === 0;
  const hint = ready ? "" : `Needs: ${missing.slice(0, 3).join(", ")}`;
  const moreLabels: string[] = SSH_MORE.map((f) => f[1]);
  const hiddenMissing = source === "ssh" && missing.some((m) => moreLabels.includes(m));
  const controls = useRef<HTMLDivElement>(null);
  const showMore = () => { const d = controls.current?.querySelector("details"); if (d) d.open = true; };

  const shown = ranSource === source ? state : null;
  const devices = shown?.health.devices;
  // Report only when the lit set changes (a fresh [] each render would loop through App).
  const lit = JSON.stringify(active && devices ? highlightsFromDevices(devices, joints, sceneIds, shown?.status === "running", shown?.watching ?? []) : []);
  useEffect(() => { onHighlight(JSON.parse(lit)); }, [lit]); // eslint-disable-line react-hooks/exhaustive-deps

  async function call(fn: () => Promise<RunStatePayload>): Promise<void> {
    try { setState(await fn()); setError(null); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }

  // Polls itself while the run goes (fixed 1s interval, like DesignPanel).
  useEffect(() => {
    if (state?.status !== "running") return;
    const timer = setTimeout(() => void call(() => proApi().pollRun()), 1000);
    return () => clearTimeout(timer);
  }, [state]); // eslint-disable-line react-hooks/exhaustive-deps

  async function start(): Promise<void> {
    if (!contract || !ready) return;
    const t = (k: SshKey) => ssh[k].trim();
    const request =
      source === "recording" ? { recording }
      : source === "docker" ? { live: true as const, container: container.trim() }
      : {
          live: true as const, ssh_host: t("host"), ssh_port: port, ssh_user: t("user"), ssh_identity_file: t("identity"),
          ssh_known_hosts: t("knownHosts"), ssh_target: t("target"), ssh_image: t("image"),
          ...(t("keepBag") ? { keep_bag: t("keepBag") } : {}),
        };
    setError(null);
    setRanSource(source);
    await call(() => proApi().startRun({ contract, ...request }));
  }

  if (!active) return null;
  const [verdictStatus, verdictLabel]: [Status, string] = state ? VERDICT[state.health.verdict] ?? ["muted", state.health.verdict] : ["muted", ""];
  const running = state?.status === "running";
  const ownRun = !!shown && running; // the job bar lives on the tab its run started from; other tabs look idle (Start waits)
  const startActions = !ownRun && (
    <div className="run-actions">
      <Button variant="primary" className="kit-btn kit-btn-primary run-start" disabled={!ready || running} title={hint || undefined} onClick={() => void start()}>Start</Button>
      {shown && <Button variant="quiet" className="kit-btn kit-btn-quiet run-poll" onClick={() => void call(() => proApi().pollRun())}>Refresh</Button>}
    </div>
  );
  const startInline = source !== "ssh";
  const sshField = ([key, label, aria, placeholder]: readonly [SshKey, string, string, string?]) => (
    <Field key={key} label={label} hint={aria} aria-label={aria} placeholder={placeholder} onBlur={() => setTouched(new Set(touched).add(key))}
      error={touched.has(key) && missing.includes(label) ? "Required" : undefined} {...(key === "port" ? { type: "number", min: 1, max: 65535 } : {})}
      value={ssh[key]} onChange={(e: { target: { value: string } }) => setSsh({ ...ssh, [key]: e.target.value })} />
  );
  return (
    <>
      <Panel
        title="Run"
        footer={contract && (ownRun || !startInline) ? (
          <div className="run-footer">
            {ownRun && (
              <JobBar
                status={JOB[state.status][0]} word={JOB[state.status][1]}
                onCancel={() => void call(() => proApi().cancelRun())}
              />
            )}
            {!startInline && !shown && hint && (
              <p className={hiddenMissing ? "run-hint" : "run-hint kit-sr"}>{hint}{hiddenMissing && <> <button type="button" className="run-hint-show" onClick={showMore}>Show</button></>}</p>
            )}
            {!startInline && startActions}
          </div>
        ) : undefined}
        actions={shown && !running ? <span className="check-headline check-headline-left" aria-live="polite"><StatusPill status={shown.status === "completed" ? verdictStatus : JOB[shown.status][0]}>{shown.status === "completed" ? verdictLabel : JOB[shown.status][1]}</StatusPill></span> : undefined}
      >
        {!contract ? <EmptyState title="Open a robot first." /> : (
          <div className="run-panel">
            <Segmented label="Source" options={SOURCES} value={source} onChange={(v) => { setSource(v); setChosen(true); }} />
            <div className={startInline ? "run-controls run-controls-inline" : "run-controls"} ref={controls}>
              {source === "recording" && (
                <span className="run-choose-recording">
                  <FilePick label="ROS 2 bag (.mcap or bag folder)" path={recording?.split(/[\\/]/).pop() || undefined}
                    onPick={() => void proApi().chooseRecording().then(setRecording)} onRemove={() => setRecording(null)} />
                </span>
              )}
              {source === "docker" && (
                <Field label="Container" hint="Docker container name or ID" aria-label="Docker container name or ID" onBlur={() => setTouched(new Set(touched).add("container"))}
                  error={touched.has("container") && !container.trim() ? "Required" : undefined}
                  value={container} onChange={(e: { target: { value: string } }) => setContainer(e.target.value)} />
              )}
              {source === "ssh" && (
                <>
                  {SSH_MAIN.map(sshField)}
                  <Disclosure summary="More">{SSH_MORE.map(sshField)}</Disclosure>
                </>
              )}
              {startInline && startActions}
              {startInline && !shown && hint && <p className="run-hint kit-sr">{hint}</p>}
            </div>
            {error && <Notice kind="error">{error}</Notice>}
            {shown ? (
              <div className="scroll-fade run-rows"><RunFace payload={shown.health} running={running} /></div>
            ) : !chosen && !ready && (
              <EmptyState title="Pick a source, then Start." />
            )}
          </div>
        )}
      </Panel>
    </>
  );
}
