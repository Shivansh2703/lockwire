import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockMatchMedia } from "./App.testkit";
import { Shell } from "./Shell";
import { SideDrawer } from "./SideDrawer";

vi.mock("@xterm/xterm", () => ({ Terminal: class { cols = 80; rows = 24; loadAddon() {} open() {} write() {} focus() {} dispose() {} onData() {} } }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });

let container: HTMLDivElement;
let root: Root;
const api = { startTerminal: vi.fn().mockResolvedValue({ ok: true }), resizeTerminal: vi.fn(), writeTerminal: vi.fn(), killTerminal: vi.fn(), onTerminalEvent: vi.fn(() => () => {}) };

function App() {
  const [open, setOpen] = React.useState(false);
  return <Shell mode="Check" onModeChange={() => {}} connected locked assistant={{ open, onToggle: () => setOpen((o) => !o) }}
    drawer={<SideDrawer open={open} onClose={() => setOpen(false)} cwd="/r" assistant={<p>assistant body</p>} />} />;
}
const click = (el: Element) => act(async () => { (el as HTMLElement).click(); });
const drawer = () => container.querySelector<HTMLElement>("#side-drawer")!;

beforeEach(async () => {
  mockMatchMedia();
  Object.defineProperty(window, "lockwire", { configurable: true, value: api });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.clearAllMocks(); });

describe("SideDrawer", () => {
  it("opens from the top-bar button, switches tabs, starts the shell once, and closes on Esc", async () => {
    const button = [...container.querySelectorAll("button")].find((b) => b.textContent === "Assistant")!;
    expect(drawer().hidden).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");
    await click(button);
    expect(drawer().hidden).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(drawer().textContent).toContain("assistant body");
    expect(api.startTerminal).not.toHaveBeenCalled();
    await click([...drawer().querySelectorAll("[role=tab]")].find((b) => b.textContent === "Terminal")!);
    expect(api.startTerminal).toHaveBeenCalledWith({ cols: 80, rows: 24, cwd: "/r" });
    await act(async () => { drawer().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(drawer().hidden).toBe(true);
  });
});
