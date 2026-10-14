// Types only, no runtime code: the renderer's window.lockwire is what the preload's
// contextBridge actually installs (../preload/index.ts). Importing the preload's own
// exported type keeps this from becoming a second, hand-typed copy of that surface.
import type { PreloadApi } from "../preload/index";

declare global {
  interface Window {
    lockwire: PreloadApi;
  }
}

export {};
