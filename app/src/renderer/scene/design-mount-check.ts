/**
 * Runs the REAL, production `electron-vite build` output — `out/renderer/index.html`, the exact
 * file the shipped app loads — inside a hidden `BrowserWindow` (never a screenshot,
 * never a window on the owner's screen), injects a real `CheckPayload` (the X500 fixture, with
 * its real geometry) through `window.__lockwireLoadCheckPayload` (App.tsx), and reads back the
 * canvas's center pixel with `gl.readPixels`.
 *
 * The distinction this box exists for: every other hidden e2e in this file bundles a SEPARATE
 * renderer entry (`e2e-pixel-entry.ts`, `lens-e2e-entry.tsx`) that mounts `Canvas` directly —
 * proving the mount works in a test page. This script loads no bundle of its own; it opens the
 * app's own built `index.html`, so a pixel read here proves the mount works in the app the owner
 * opens, not in a harness standing in for it.
 */
import { readFileSync } from "node:fs";

import { app, BrowserWindow } from "electron";

void app.whenReady().then(async () => {
  app.dock?.hide();
  // Unlike the sibling harnesses (64x64, a bare canvas with no chrome around it), this window
  // loads the REAL app: Frame's titlebar/mode-tabs/headline/health-strip/agents-line/footer
  // plus the lens bar all take vertical space before `.design-face-canvas` gets any -- a 64x64
  // window leaves it 0px tall, so the app must actually be given room to lay out, same size as
  // the capture step below.
  const win = new BrowserWindow({
    show: false,
    width: 1280,
    height: 800,
    webPreferences: { offscreen: false },
  });

  const htmlPath = process.env.LOCKWIRE_E2E_APP_HTML_PATH;
  if (!htmlPath) throw new Error("LOCKWIRE_E2E_APP_HTML_PATH not set");
  await win.loadFile(htmlPath);

  const payloadPath = process.env.LOCKWIRE_E2E_PAYLOAD_PATH;
  if (!payloadPath) throw new Error("LOCKWIRE_E2E_PAYLOAD_PATH not set");
  const payloadJson = readFileSync(payloadPath, "utf8");

  await win.webContents.executeJavaScript(
    `window.__lockwireLoadCheckPayload(${JSON.stringify(payloadJson)})`,
  );

  // E2-j2 rebase: App.tsx now defaults to the Check tab (e2-app-r2's OpenFace/CheckFace flow
  // lives there), not Design -- DesignPanel, and its `__lockwireReadNonBackgroundPixel` hook,
  // only mount once the Design tab is selected. A click is now required where none was before.
  await win.webContents.executeJavaScript(
    `Array.from(document.querySelectorAll('[role="tab"]')).find((b) => b.textContent === "Design")?.click();`,
  );

  // Poll rather than a fixed frame count: `Canvas`'s own mount effect (buildScene + setCtx)
  // fires one commit AFTER the one that first renders `<Canvas>`, and `SceneCapture`'s effect
  // (which sets `liveCtx`) fires on a commit after THAT -- three effect passes deep, not the
  // "React commits once, browser paints once" two frames a synthetic-part harness needs
  // (measured: two `requestAnimationFrame`s alone read "scene not mounted" intermittently, same
  // idiom as capture.template.ts's `.check-headline` poll).
  const pixel = (await win.webContents.executeJavaScript(`
    new Promise((resolve, reject) => {
      const startedAt = Date.now();
      (function poll() {
        try {
          resolve(window.__lockwireReadNonBackgroundPixel());
        } catch (err) {
          if (Date.now() - startedAt > 8000) return reject(err);
          setTimeout(poll, 50);
        }
      })();
    });
  `)) as [number, number, number, number];

  const silhouette = (await win.webContents.executeJavaScript(
    `window.__lockwireReadSilhouetteBounds()`,
  )) as {
    width: number;
    height: number;
    minX: number;
    maxX: number;
    minY: number;
    maxY: number;
    inset: { top: number; right: number; bottom: number; left: number };
  } | null;
  const result = { visible: win.isVisible(), pixel, silhouette };
  process.stdout.write(`${JSON.stringify(result)}\n`);
  app.quit();
});
