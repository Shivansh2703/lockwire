import { writeFile } from "node:fs/promises";

import { BrowserWindow, Menu, app } from "electron";

import { forwardSidecarState, registerIpc } from "./ipc";
import { buildMenu } from "./menu";
import {
  registerRendererProtocol,
  registerRendererScheme,
  rendererUrl,
} from "./renderer-protocol";
import { applySecurity } from "./security";
import { startSidecar, stopSidecar } from "./sidecar";
import { windowOptions } from "./window-options";

registerRendererScheme();

function createWindow(hidden: boolean, onRendererLoaded?: () => void): BrowserWindow {
  const win = new BrowserWindow(windowOptions(hidden));
  let rendererLoadFailed = false;
  if (onRendererLoaded) {
    // did-finish-load also fires for Electron's error page.  Observe failure before
    // beginning the load, then accept the finish event only for the real renderer.
    win.webContents.once("did-fail-load", () => {
      rendererLoadFailed = true;
    });
    win.webContents.once("did-finish-load", () => {
      if (!rendererLoadFailed) onRendererLoaded();
    });
  }
  void win.loadURL(rendererUrl());
  // e9-join: push every sidecar state change to this window (FailureBanner/CheckFace's
  // side of the same bridge that "sidecar:state"/"sidecar:restart" answer on request).
  const stopForwarding = forwardSidecarState(win);
  win.once("closed", stopForwarding);
  return win;
}

void app
  .whenReady()
  .then(() => {
    const readyFile = process.env.LOCKWIRE_GATE_READY_FILE;
    const hidden = Boolean(readyFile);
    // Before ready,: the dock icon must never bounce, even for an instant.
    if (hidden && process.platform === "darwin") app.dock?.hide();
    applySecurity();
    registerRendererProtocol();
    registerIpc();
    Menu.setApplicationMenu(buildMenu());
    startSidecar();
    createWindow(hidden, readyFile ? () => void writeFile(readyFile, "") : undefined);
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow(hidden);
    });
  })
  .catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : String(error));
    app.quit();
  });

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// The prompt-teardown half (the pipe close is the orphan-proofing half,
// unaffected by this handler): stop the sidecar before this process actually exits,
// on every quit path -- clean quit, quit-from-dock, Cmd+Q, app.quit().
app.on("before-quit", () => {
  stopSidecar();
});
