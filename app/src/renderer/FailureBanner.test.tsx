import { createServer } from "node:http";
import { createRequire } from "node:module";

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import * as sidecar from "../main/sidecar";
import { FailureBanner, useSidecarFailed } from "./FailureBanner";

// Real sidecar.ts driven by the same fake-child harness sidecar.test.ts uses (reuse
// the sidecar-lifecycle box's own exported surface rather than re-walking child-exit
// handling here). createRequire, not `new URL(x, import.meta.url)`: the latter throws
// "URL must be of scheme file" under this worktree's jsdom test environment -- an
// environment defect that reproduces even on the unmodified sidecar.test.ts, so it's
// worked around here rather than fixed there (out of this box's file list).
const FAKE = createRequire(import.meta.url).resolve("../../test/fake-sidecar.mjs");

function waitFor(predicate: () => boolean, timeoutMs = 2000): Promise<void> {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      if (predicate()) return resolve();
      if (Date.now() - start > timeoutMs) return reject(new Error("waitFor timed out"));
      setTimeout(tick, 10);
    };
    tick();
  });
}

function start(env: NodeJS.ProcessEnv = {}): void {
  sidecar.startSidecar({ command: process.execPath, args: [FAKE], env: { ...process.env, ...env } });
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  // e9-join: FailureBanner/useSidecarFailed now read the sidecar through the preload
  // bridge (window.lockwire), never by importing "../main/sidecar" directly -- jsdom has
  // no real preload, so this stubs the bridge, but backs it with the SAME real sidecar.ts
  // module and real child process the tests below already drive. Only the plumbing layer
  // (contextBridge) is faked; the sidecar lifecycle under test is real.
  (window as unknown as { lockwire: object }).lockwire = {
    getSidecarState: async () => sidecar.getSidecarState(),
    onSidecarState: (cb: (state: sidecar.SidecarState) => void) => sidecar.onSidecarState(cb),
    restartSidecar: async () => {
      sidecar.startSidecar({ command: process.execPath, args: [FAKE] });
      return { accepted: true as const };
    },
  };
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  sidecar.stopSidecar();
  delete process.env.LOCKWIRE_SIDECAR_COMMAND;
  delete process.env.LOCKWIRE_SIDECAR_ARGS;
  Reflect.deleteProperty(window, "lockwire");
});

function renderBanner() {
  root = createRoot(container);
  act(() => {
    root.render(<FailureBanner />);
  });
}

describe("FailureBanner (e2-h)", () => {
  it("test_a_child_exit_shows_the_banner_and_offers_exactly_one_restart", async () => {
    // The restart button calls the plain startSidecar() a real menu/hotkey would --
    // point resolveSidecarCommand() at the fake child via its env escape hatch so the
    // follow-up spawn doesn't ENOENT.
    process.env.LOCKWIRE_SIDECAR_COMMAND = process.execPath;
    process.env.LOCKWIRE_SIDECAR_ARGS = FAKE;
    renderBanner();
    start({ FAKE_SIDECAR_MODE: "ready" });
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
    expect(container.querySelector(".failure-banner")).toBeNull();

    sidecar.stopSidecar();
    await waitFor(() => sidecar.getSidecarState().phase === "stopped");
    await waitFor(() => container.querySelector(".failure-banner") !== null);
    expect(container.textContent).toContain("the lockwire engine stopped");

    const restartButtons = () => [...container.querySelectorAll(".failure-banner button")];
    expect(restartButtons().length).toBe(1);

    act(() => {
      (restartButtons()[0] as HTMLButtonElement).click();
    });
    // Exactly one restart offered: the button is gone immediately, whether or not the
    // follow-up start is still becoming ready.
    expect(restartButtons().length).toBe(0);
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
  });

  it("test_no_in_flight_call_hangs_after_the_child_exits", async () => {
    // A real server that never answers, so the in-flight fetch is genuinely pending
    // when the child dies mid-command -- the brief's decisive case, not idle failure.
    const server = createServer(() => {});
    await new Promise<void>((resolve) => server.listen(0, resolve));
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("no port");
    try {
      start({ FAKE_SIDECAR_MODE: "ready", FAKE_SIDECAR_PORT: String(address.port) });
      await waitFor(() => sidecar.getSidecarState().phase === "ready");
      const inFlight = sidecar.callSidecar("/v1/slow-command");
      let settled = false;
      inFlight.catch(() => {
        settled = true;
      });
      sidecar.stopSidecar();
      await waitFor(() => sidecar.getSidecarState().phase === "stopped");
      await expect(inFlight).rejects.toThrow(/sidecar stopped/);
      expect(settled).toBe(true);
    } finally {
      server.close();
    }
  });

  it("test_no_number_remains_on_screen_after_the_engine_stops", async () => {
    // A stand-in reading, gated the way a real health strip/verdict number would be --
    // proves useSidecarFailed() actually flips, not just that FailureBanner renders text.
    function Reading() {
      const failed = useSidecarFailed();
      return <div className="reading">{failed ? null : "42"}</div>;
    }
    root = createRoot(container);
    act(() => {
      root.render(<Reading />);
    });
    start({ FAKE_SIDECAR_MODE: "ready" });
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
    await waitFor(() => container.querySelector(".reading")?.textContent === "42");

    sidecar.stopSidecar();
    await waitFor(() => sidecar.getSidecarState().phase === "stopped");
    await waitFor(() => container.querySelector(".reading")?.textContent === "");
    expect(container.querySelector(".reading")?.textContent).toBe("");
  });
});
