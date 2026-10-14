import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  isPackaged: false,
  handle: vi.fn(),
  readFile: vi.fn().mockResolvedValue(Buffer.from("asset bytes")),
  registerSchemesAsPrivileged: vi.fn(),
}));

vi.mock("node:fs/promises", () => ({ default: { readFile: mocks.readFile }, readFile: mocks.readFile }));
vi.mock("electron", () => ({
  app: {
    get isPackaged() {
      return mocks.isPackaged;
    },
  },
  protocol: { handle: mocks.handle, registerSchemesAsPrivileged: mocks.registerSchemesAsPrivileged },
}));

import {
  PACKAGED_RENDERER_URL,
  registerRendererProtocol,
  registerRendererScheme,
  rendererUrl,
} from "./renderer-protocol";

describe("packaged renderer protocol", () => {
  it("uses the environment renderer only in an explicitly nonpackaged app", () => {
    process.env.ELECTRON_RENDERER_URL = "https://attacker.example/";
    mocks.isPackaged = true;
    expect(rendererUrl()).toBe(PACKAGED_RENDERER_URL);
    mocks.isPackaged = false;
    expect(rendererUrl()).toBe("https://attacker.example/");
    delete process.env.ELECTRON_RENDERER_URL;
  });

  it("registers a standard secure scheme before app readiness", () => {
    registerRendererScheme();
    expect(mocks.registerSchemesAsPrivileged).toHaveBeenCalledWith([
      { scheme: "lockwire", privileges: { standard: true, secure: true } },
    ]);
  });

  it("serves typed renderer assets and refuses traversal outside that root", async () => {
    registerRendererProtocol();
    const handler = mocks.handle.mock.calls[0]![1] as (request: Request) => Promise<Response>;

    const asset = await handler(new Request("lockwire://app/assets/index.js"));
    expect(asset.status).toBe(200);
    expect(asset.headers.get("content-type")).toBe("text/javascript; charset=utf-8");
    expect(asset.headers.get("x-content-type-options")).toBe("nosniff");
    expect(await asset.text()).toBe("asset bytes");
    expect(mocks.readFile).toHaveBeenCalledOnce();

    const traversal = await handler(new Request("lockwire://app/assets/%2e%2e%2f%2e%2e%2fmain/index.js"));
    expect(traversal.type).toBe("error");
    expect(mocks.readFile).toHaveBeenCalledOnce();

    mocks.readFile.mockRejectedValueOnce(new Error("missing"));
    const missing = await handler(new Request("lockwire://app/assets/missing.js"));
    expect(missing.type).toBe("error");
  });
});
