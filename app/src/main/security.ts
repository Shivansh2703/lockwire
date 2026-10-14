import { existsSync } from "node:fs";

import { app, session } from "electron";

import { PRODUCT_NAME } from "../shared/branding";
import { PACKAGED_RENDERER_URL, resolveRendererRequest } from "./renderer-protocol";
import { windowOptions } from "./window-options";

const CSP = [
  "default-src 'self'",
  "base-uri 'none'",
  "connect-src 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  "img-src 'self' data:",
  "object-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
].join("; ");

function assertPackagedResources(): void {
  if (!app.isPackaged) return;
  const preload = windowOptions(false).webPreferences?.preload;
  const renderer = resolveRendererRequest(PACKAGED_RENDERER_URL)?.path;
  // Do not preflight the sidecar here: startSidecar reports spawn failures through
  // the renderer's actionable failure banner. Missing UI resources remain fatal;
  // they are required to create the trusted surface that can report that failure.
  // startSidecar still resolves only the packaged resource and ignores the dev override.
  // Its packaged resolver keeps the existing platform/architecture resource path.
  const resources = [
    ["preload", typeof preload === "string" ? preload : ""],
    ["renderer index", renderer ?? ""],
  ] as const;
  for (const [name, path] of resources) {
    if (!path || !existsSync(path)) {
      throw new Error(`${PRODUCT_NAME} startup refusal: required ${name} missing: ${path}`);
    }
  }
}

export function applySecurity(): void {
  assertPackagedResources();

  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({ responseHeaders: { ...details.responseHeaders, "Content-Security-Policy": [CSP] } });
  });
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);

  app.on("web-contents-created", (_event, contents) => {
    contents.on("will-navigate", (event) => event.preventDefault());
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
  });
}
