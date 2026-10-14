import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";

import { app, protocol } from "electron";

export const PACKAGED_RENDERER_URL = "lockwire://app/index.html";

export function rendererUrl(): string {
  return !app.isPackaged && process.env.ELECTRON_RENDERER_URL
    ? process.env.ELECTRON_RENDERER_URL
    : PACKAGED_RENDERER_URL;
}

const RENDERER_ROOT = resolve(__dirname, "../renderer");
const CONTENT_TYPES: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

export function registerRendererScheme(): void {
  protocol.registerSchemesAsPrivileged([{ scheme: "lockwire", privileges: { standard: true, secure: true } }]);
}

export function resolveRendererRequest(requestUrl: string): { path: string; contentType: string } | undefined {
  try {
    const url = new URL(requestUrl);
    const relative = decodeURIComponent(url.pathname.slice(1));
    const path = resolve(RENDERER_ROOT, relative);
    const contentType = CONTENT_TYPES[extname(path)];
    if (
      url.protocol !== "lockwire:" ||
      url.host !== "app" ||
      url.username ||
      url.password ||
      (relative !== "index.html" && !relative.startsWith("assets/")) ||
      !path.startsWith(RENDERER_ROOT + sep) ||
      !contentType
    ) {
      return undefined;
    }
    return { path, contentType };
  } catch {
    return undefined;
  }
}

export function registerRendererProtocol(): void {
  protocol.handle("lockwire", async (request) => {
    const asset = request.method === "GET" ? resolveRendererRequest(request.url) : undefined;
    if (!asset) return Response.error();
    try {
      return new Response(await readFile(asset.path), {
        headers: { "Content-Type": asset.contentType, "X-Content-Type-Options": "nosniff" },
      });
    } catch {
      return Response.error();
    }
  });
}
