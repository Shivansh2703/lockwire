// The browser-side entry esbuild bundles (platform: browser) for the hidden-window pixel
// check. Exposes the REAL `buildScene` on `window` — nothing here is reimplemented or mocked;
// only `WebGLRenderer` was stubbed in the unit test, never here.
import { buildScene } from "./buildScene";

declare global {
  interface Window {
    __buildScene: typeof buildScene;
  }
}

window.__buildScene = buildScene;
