import type { PathLike } from "node:fs";

import { beforeEach, describe, expect, it, vi } from "vitest";

import { PRODUCT_NAME } from "../shared/branding";

const mocks = vi.hoisted(() => ({
  appOn: vi.fn(),
  existsSync: vi.fn<(path: PathLike) => boolean>(() => true),
  onHeadersReceived: vi.fn(),
  setPermissionCheckHandler: vi.fn(),
  setPermissionRequestHandler: vi.fn(),
}));

vi.mock("node:fs", async () => {
  const actual = await vi.importActual<typeof import("node:fs")>("node:fs");
  return { ...actual, default: { ...actual, existsSync: mocks.existsSync }, existsSync: mocks.existsSync };
});
vi.mock("electron", () => ({
  app: { isPackaged: true, on: mocks.appOn },
  session: {
    defaultSession: {
      webRequest: { onHeadersReceived: mocks.onHeadersReceived },
      setPermissionCheckHandler: mocks.setPermissionCheckHandler,
      setPermissionRequestHandler: mocks.setPermissionRequestHandler,
    },
  },
}));
vi.mock("./renderer-protocol", () => ({
  PACKAGED_RENDERER_URL: "lockwire://app/index.html",
  resolveRendererRequest: () => ({ path: "/app/renderer/index.html", contentType: "text/html" }),
}));
vi.mock("./sidecar", () => ({
  resolveSidecarCommand: () => ({ command: "/app/resources/sidecar/lockwire-sidecar", args: [] }),
}));
vi.mock("./window-options", () => ({
  windowOptions: () => ({ webPreferences: { preload: "/app/preload/index.js" } }),
}));

import { applySecurity } from "./security";

beforeEach(() => {
  vi.clearAllMocks();
  mocks.existsSync.mockReturnValue(true);
});

describe("the main-process security hook", () => {
  it("sets CSP and fails closed for navigation, new windows, and both permission APIs", () => {
    applySecurity();

    const headerHook = mocks.onHeadersReceived.mock.calls[0]![0];
    const headers = vi.fn();
    headerHook({ responseHeaders: { Existing: ["kept"] } }, headers);
    expect(headers).toHaveBeenCalledWith({
      responseHeaders: expect.objectContaining({
        Existing: ["kept"],
        "Content-Security-Policy": [expect.stringContaining("connect-src 'none'")],
      }),
    });

    const requestPermission = mocks.setPermissionRequestHandler.mock.calls[0]![0];
    const permissionResult = vi.fn();
    requestPermission({}, "media", permissionResult);
    expect(permissionResult).toHaveBeenCalledWith(false);
    expect(mocks.setPermissionCheckHandler.mock.calls[0]![0]({}, "media", "lockwire://app")).toBe(false);

    const contents = { on: vi.fn(), setWindowOpenHandler: vi.fn() };
    mocks.appOn.mock.calls.find(([event]) => event === "web-contents-created")![1]({}, contents);
    const navigation = { preventDefault: vi.fn() };
    contents.on.mock.calls.find(([event]) => event === "will-navigate")![1](navigation);
    expect(navigation.preventDefault).toHaveBeenCalledOnce();
    expect(contents.setWindowOpenHandler.mock.calls[0]![0]({})).toEqual({ action: "deny" });
  });

  it.each([
    ["preload", "/app/preload/index.js"],
    ["renderer index", "/app/renderer/index.html"],
  ])("refuses startup by exact required-resource name when %s is missing", (name, missingPath) => {
    mocks.existsSync.mockImplementation((path) => path !== missingPath);
    expect(() => applySecurity()).toThrow(`${PRODUCT_NAME} startup refusal: required ${name} missing: ${missingPath}`);
    expect(mocks.onHeadersReceived).not.toHaveBeenCalled();
  });

  it("allows the renderer to report a missing bundled sidecar", () => {
    mocks.existsSync.mockImplementation((path) => path !== "/app/resources/sidecar/lockwire-sidecar");
    expect(() => applySecurity()).not.toThrow();
  });
});
