import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockMatchMedia } from "./App.testkit";
import { Shell } from "./Shell";
import { panels, registerPanel, visiblePanels } from "./panelRegistry";
import { SideDrawer } from "./SideDrawer";

vi.mock("@xterm/xterm", () => ({ Terminal: class { cols = 80; rows = 24; loadAddon() {} open() {} write() {} focus() {} dispose() {} onData() {} } }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });

let container: HTMLDivElement;
let root: Root;
const api = { startTerminal: vi.fn().mockResolvedValue({ ok: true }), resizeTerminal: vi.fn(), writeTerminal: vi.fn(), killTerminal: vi.fn(), onTerminalEvent: vi.fn(() => () => {}) };

let page: unknown = null;
function App() {
  const [open, setOpen] = React.useState(false);
  const [want, setWant] = React.useState<{ id: string } | undefined>();
  const [tab, setTab] = React.useState("assistant");
  const shown = visiblePanels(page as never);
  const onPanel = shown.some((p) => p.id === tab);
  const go = (id: string, isOn: boolean) => () => { if (open && isOn) setOpen(false); else { setWant({ id }); setOpen(true); } };
  return <Shell mode="Check" onModeChange={() => {}} connected locked assistant={{ open: open && !onPanel, onToggle: go("@assist", !onPanel) }} panels={shown.length ? { open: open && onPanel, onToggle: go("@panels", onPanel) } : undefined}
    drawer={<SideDrawer open={open} page={page as never} onClose={() => setOpen(false)} cwd="/r" contract="/r/lockwire.yaml" revision="rev1" want={want} onTab={setTab} assistant={<p>assistant body</p>} />} />;
}
const click = (el: Element) => act(async () => { (el as HTMLElement).click(); });
const btn = (name: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === name)!;
const tabBtn = (name: string) => [...drawer().querySelectorAll("[role=tab]")].find((b) => b.textContent === name)!;
const pick = (name: string) => click(tabBtn(name)); // Terminal is a tab beside Assistant, as in 0.2.9
const drawer = () => container.querySelector<HTMLElement>("#side-drawer")!;

beforeEach(async () => {
  mockMatchMedia();
  Object.defineProperty(window, "lockwire", { configurable: true, value: api });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
});
afterEach(() => { panels.length = 0; page = null; act(() => root.unmount()); container.remove(); vi.clearAllMocks(); });

describe("SideDrawer", () => {
  it("opens from the top-bar button, switches tabs, starts the shell once, and closes on Esc", async () => {
    const button = btn("Assistant");
    expect(drawer().hidden).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");
    await click(button);
    expect(drawer().hidden).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(drawer().textContent).toContain("assistant body");
    expect(api.startTerminal).not.toHaveBeenCalled();
    await pick("Terminal");
    expect(api.startTerminal).toHaveBeenCalledWith({ cols: 80, rows: 24, cwd: "/r" });
    expect(drawer().textContent).toContain("This is a real shell on your computer. Commands run as you.");
    await act(async () => { drawer().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(drawer().hidden).toBe(true);
  });

  it("a remembered tab id from the old Agent tab lands on the Assistant tab, never a blank drawer", async () => {
    const box = document.createElement("div");
    document.body.append(box);
    const r = createRoot(box);
    await act(async () => r.render(<SideDrawer open onClose={() => {}} cwd="/r" contract="/r/lockwire.yaml" revision="rev1" want={{ id: "agent" }} assistant={<p>assistant body</p>} />));
    expect(box.querySelector("[role=tab][aria-selected=true]")!.textContent).toBe("Assistant");
    expect([...box.querySelectorAll<HTMLElement>(".drawer-body")].filter((b) => !b.hidden).map((b) => b.textContent)).toEqual(["assistant body"]);
    act(() => r.unmount());
    box.remove();
  });

  it("one assistant: the drawer has no Agent tab, and nothing in it says Agent", async () => {
    await click(btn("Assistant"));
    expect([...drawer().querySelectorAll("[role=tab]")].map((t) => t.textContent)).toEqual(["Assistant", "Terminal"]);
    expect(container.textContent).not.toMatch(/\bAgent\b/);
  });
});

describe("the panel slot", () => {
  const tabNames = () => [...drawer().querySelectorAll("[role=tab]")].map((t) => t.textContent);
  const render = () => act(async () => root.render(<App />));
  const testPanel = () => registerPanel({ id: "power", label: "Power", hasData: (p) => (p as { ok?: boolean }).ok === true, Body: () => <p>power body</p> });

  it("with nothing registered the top bar is Assistant only (no Panels) and the drawer has Assistant, Terminal on one row", async () => {
    page = { ok: true };
    await render();
    expect(btn("Panels")).toBeUndefined();
    await click(btn("Assistant"));
    expect(tabNames()).toEqual(["Assistant", "Terminal"]);
  });

  it("a registered panel stays hidden until the robot has something for it, then opens on demand", async () => {
    testPanel();
    page = { ok: false };
    await render();
    await click(btn("Assistant"));
    expect(tabNames()).toEqual(["Assistant", "Terminal"]);
    expect(drawer().textContent).not.toContain("power body");
    expect(btn("Panels")).toBeUndefined();
    page = { ok: true };
    await render();
    expect(tabNames()).toEqual(["Assistant", "Terminal"]);
    expect(drawer().textContent).toContain("assistant body"); // still on the Assistant tab: never opens by itself
    await click(btn("Panels"));
    expect(tabNames()).toEqual(["Power"]);
    expect([...drawer().querySelectorAll<HTMLElement>(".drawer-body")].filter((b) => !b.hidden).map((b) => b.textContent)).toEqual(["power body"]);
  });

  it("falls back to the Assistant tab when the open panel's data goes away", async () => {
    testPanel();
    page = { ok: true };
    await render();
    await click(btn("Panels"));
    expect(tabNames()).toEqual(["Power"]);
    page = { ok: false };
    await render();
    expect([...drawer().querySelectorAll<HTMLElement>(".drawer-body")].filter((b) => !b.hidden).map((b) => b.textContent)).toEqual(["assistant body"]);
  });
});

describe("the top bar", () => {
  const topBar = () => [...container.querySelectorAll(".kit-slot-topRight button")].map((b) => b.textContent).filter((t) => t !== "Light" && t !== "Dark" && !/theme/i.test(t ?? ""));
  const selected = () => drawer().querySelector("[role=tab][aria-selected=true]")?.textContent;
  const withPanels = async (...ids: string[]) => { for (const id of ids) registerPanel({ id, label: id, hasData: () => true, Body: () => <p>{id} body</p> }); page = { ok: true }; await act(async () => root.render(<App />)); };
  it("Assistant is its own one-tap button, Panels sits beside it, and no data panel has a button of its own", async () => {
    await withPanels("power", "controls", "wiring", "parts");
    expect(topBar()).toEqual(["Assistant", "Panels"]);
    await click(btn("Assistant"));
    expect(drawer().hidden).toBe(false);
    expect(selected()).toBe("Assistant");
  });
  it("the Assistant view is Assistant | Terminal and the Panels view is the data panels: two views, one row each", async () => {
    await withPanels("power", "controls", "wiring", "parts");
    await click(btn("Assistant"));
    expect([...drawer().querySelectorAll("[role=tab]")].map((t) => t.textContent)).toEqual(["Assistant", "Terminal"]);
    await click(btn("Panels"));
    expect([...drawer().querySelectorAll("[role=tab]")].map((t) => t.textContent)).toEqual(["power", "controls", "wiring", "parts"]);
    expect(drawer().querySelectorAll("[role=tablist]").length).toBe(1);
  });
  it("Panels opens on the last-used data panel; Assistant comes back on the last-used Assistant tab", async () => {
    await withPanels("power", "wiring");
    await click(btn("Panels"));
    await click(tabBtn("wiring"));
    await click(btn("Panels"));
    expect(drawer().hidden).toBe(true);
    await click(btn("Panels"));
    expect(selected()).toBe("wiring");
    await click(btn("Assistant"));
    expect(selected()).toBe("Assistant");
    await click(tabBtn("Terminal"));
    await click(btn("Panels"));
    await click(btn("Assistant"));
    expect(selected()).toBe("Terminal");
  });
  it("nothing opens by itself", async () => {
    await withPanels("power");
    expect(drawer().hidden).toBe(true);
  });
});
