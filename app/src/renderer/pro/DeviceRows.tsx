import { StatusPill, type Status } from "../kit";
import type { DeviceRow } from "./runFacePayload";

export const DEVICE_STATUS: Record<DeviceRow["state"], Status> = {
  healthy: "go", watched: "go", degrading: "warn", stale: "warn", faulty: "bad", not_watched: "muted", absent: "muted",
};

// The health ledger's LATEST row per device (e3-h's `latest_by_device` reader, already
// walked once in Python -- this component does not re-walk the ledger or recompute a
// state). One line per device: the engine's plain name and its status word. The reason
// is the tooltip; the run time is the JobBar's, once. Devices that read identically (same state,
// same reason, e.g. twelve "nothing compared yet") are one row, named by count.
export function DeviceRows({ devices }: { devices: DeviceRow[] }) {
  const rows: { key: string; name: string; title: string; device: DeviceRow }[] = [];
  const quiet = new Map<string, DeviceRow[]>();
  for (const d of devices) {
    if (d.state === "not_watched" || d.state === "absent") quiet.set(`${d.word}|${d.reason}`, [...(quiet.get(`${d.word}|${d.reason}`) ?? []), d]);
    else rows.push({ key: d.id, name: d.name || d.id, title: d.id, device: d });
  }
  for (const [key, group] of quiet) {
    const first = group[0]!;
    rows.push(group.length === 1
      ? { key: first.id, name: first.name || first.id, title: first.id, device: first }
      : { key, name: `${group.length} devices`, title: group.map((d) => d.name || d.id).join(", "), device: first });
  }
  return (
    <div className="device-rows">
      {rows.map(({ key, name, title, device }) => {
        // A silent device (no samples) says so; the engine's "over a limit" is for a measured breach (the CLI keeps it).
        const silent = device.coverage?.reason_code === "silent";
        return (
          <div key={key} className={`device-row state-${device.state}`} title={`${title}: ${device.reason}`}>
            <span className="device-id">{name}</span>
            {device.state === "faulty" && !silent && <span className="device-reason">{device.reason}</span>}
            <span className="device-word"><StatusPill status={DEVICE_STATUS[device.state]}>{silent ? "No data" : device.word.charAt(0).toUpperCase() + device.word.slice(1)}</StatusPill></span>
          </div>
        );
      })}
    </div>
  );
}
