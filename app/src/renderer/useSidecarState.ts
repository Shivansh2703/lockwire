import { useEffect, useState } from "react";

import type { SidecarState } from "../main/sidecar";

const UNKNOWN_STATE: SidecarState = { phase: "starting" };

// e9-join: the renderer is sandboxed (contextIsolation + no nodeIntegration, main/window-options.ts)
// and cannot import "../main/sidecar" itself -- doing that pulled main-process code (node:child_process
// et al.) into the renderer bundle and broke `npm run build` ("__vite-browser-external"). This hook is
// the one place that reads the sidecar's state through the preload bridge instead (main/ipc.ts's
// "sidecar:state"/onSidecarState push, preload/index.ts's getSidecarState/onSidecarState). CheckFace
// and FailureBanner both need it -- shared here rather than each polling the bridge separately.
//
// `window.lockwire` is only guaranteed once Electron's real contextBridge preload has run, and
// even then only with the two sidecar methods this hook needs: a test that renders these
// components with no bridge stubbed at all, or with a partial one covering some other box's own
// preload calls only (e.g. App.test.tsx's `openLockfile`-only stubs), gets UNKNOWN_STATE and no
// subscription, never a throw.
export function useSidecarState(): SidecarState {
  const [state, setState] = useState<SidecarState>(UNKNOWN_STATE);

  useEffect(() => {
    const bridge = window.lockwire;
    if (!bridge?.getSidecarState || !bridge.onSidecarState) return;
    let cancelled = false;
    void bridge.getSidecarState().then((next) => {
      if (!cancelled) setState(next);
    });
    const unsubscribe = bridge.onSidecarState((next) => setState(next));
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  return state;
}
