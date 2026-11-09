import { DayOneNotice } from "./DayOneNotice";
import { DeviceRows } from "./DeviceRows";
import type { RunFacePayload } from "./runFacePayload";

// One empty-state sentence on every robot and mode (the engine's "Nothing watched yet" and the device row's say the same thing).
const NOTHING = "Nothing compared yet";

export interface RunFaceProps {
  payload: RunFacePayload;
  robotName?: string;
  /** Whether a live preflight/scan job (e3-q) is running right now -- not part of the
   * health document, which knows nothing about a job in flight; sourced the same way
   * `CheckFace` sources `connected` from outside its payload. */
  running?: boolean;
  onCancel?: () => void;
}

// Content only: App owns the single frame, verdict and cancellation slots.
export function RunFace({ payload, running }: RunFaceProps) {
  return (
    <section className="run-face">
      {running && payload.devices.every((d) => d.state === "not_watched" || d.state === "absent") ? (
          <DayOneNotice nextStep="Waiting for data" />
        ) : payload.nothing_watched ? (
          <DayOneNotice nextStep={payload.next_step && NOTHING} />
        ) : (
          <DeviceRows devices={payload.devices} />
        )}
    </section>
  );
}
