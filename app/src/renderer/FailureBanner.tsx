// Plan v8 lines 396-398: when the sidecar dies, the window shows a plain banner, no
// in-flight call hangs, exactly one restart is offered, and nothing keeps showing a
// number whose source is gone. Reuse, not re-walk: the child-exit event and the
// restart entry point are the sidecar-lifecycle box's own exported surface.
import { useEffect, useState } from "react";

import { PRODUCT_NAME } from "../shared/branding";

import { Notice } from "./kit";
import { useSidecarState } from "./useSidecarState";

/** True once the sidecar is no longer usable -- failed to start, or died mid-session.
 * Any face showing a robot reading reuses this to stop showing one once its source is
 * gone, instead of leaving a stale number on screen. */
export function useSidecarFailed(): boolean {
  const state = useSidecarState();
  return state.phase === "stopped" || state.phase === "error";
}

export function FailureBanner() {
  const state = useSidecarState();
  const [restartOffered, setRestartOffered] = useState(true);

  useEffect(() => {
    // A fresh failure (a new "stopped"/"error" after having left that phase) earns its
    // one restart offer back. useSidecarState already tracks `state` itself; this only
    // needs to notice the same transitions to reset the offer.
    if (state.phase === "starting" || state.phase === "ready") setRestartOffered(true);
  }, [state.phase]);

  if (state.phase !== "stopped" && state.phase !== "error") return null;

  function restart() {
    setRestartOffered(false);
    void window.lockwire?.restartSidecar();
  }

  return (
    <div className="failure-banner">
      <Notice kind="error" action={restartOffered ? { label: "Restart", onClick: restart } : undefined}>
        <span title={state.phase === "error" ? state.message : `exit ${state.code ?? state.signal}`}>{PRODUCT_NAME} stopped working.</span>
      </Notice>
    </div>
  );
}
