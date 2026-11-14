import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PRODUCT_NAME } from "../shared/branding";
import { mockMatchMedia } from "./App.testkit";
import { Shell } from "./Shell";
import { panels, registerPanel, visiblePanels } from "./panelRegistry";
import { isPowerSpec } from "./powerSpec";
import { SideDrawer } from "./SideDrawer";

vi.mock("@xterm/xterm", () => ({ Terminal: class { cols = 80; rows = 24; loadAddon() {} open() {} write() {} focus() {} dispose() {} onData() {} } }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });

let container: HTMLDivElement;
let root: Root;
let agentPush: (p: unknown) => void = () => {};
const api = { designPower: vi.fn().mockResolvedValue({ ok: true }), getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: true, model: "", enabledRobots: ["/r/lockwire.yaml"] }), setAssistantSettings: vi.fn(), startAgent: vi.fn().mockResolvedValue({ ok: true }), stopAgent: vi.fn(), onAgentEvent: vi.fn((cb: (p: unknown) => void) => { agentPush = cb; return () => {}; }), startTerminal: vi.fn().mockResolvedValue({ ok: true }), resizeTerminal: vi.fn(), writeTerminal: vi.fn(), killTerminal: vi.fn(), onTerminalEvent: vi.fn(() => () => {}) };

const onReview = vi.fn();
let page: unknown = null;
function App() {
  const [open, setOpen] = React.useState(false);
  const [want, setWant] = React.useState<{ id: string } | undefined>();
  const [tab, setTab] = React.useState("assistant");
  const shown = visiblePanels(page as never);
  const onPanel = shown.some((p) => p.id === tab);
  const go = (id: string, isOn: boolean) => () => { if (open && isOn) setOpen(false); else { setWant({ id }); setOpen(true); } };
  return <Shell mode="Check" onModeChange={() => {}} connected locked assistant={{ open: open && !onPanel, onToggle: go("@assist", !onPanel) }} panels={shown.length ? { open: open && onPanel, onToggle: go("@panels", onPanel) } : undefined}
    drawer={<SideDrawer open={open} page={page as never} onClose={() => setOpen(false)} cwd="/r" contract="/r/lockwire.yaml" revision="rev1" onReview={onReview} want={want} onTab={setTab} assistant={<p>assistant body</p>} />} />;
}
const click = (el: Element) => act(async () => { (el as HTMLElement).click(); });
const btn = (name: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === name)!;
const tabBtn = (name: string) => [...drawer().querySelectorAll("[role=tab]")].find((b) => b.textContent === name)!;
const pick = (name: string) => click(tabBtn(name)); // Agent and Terminal are tabs beside Assistant, as in 0.2.9
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

  it("the Agent tab with no key shows the same key screen instead of failing", async () => {
    api.getAssistantSettings.mockResolvedValueOnce({ hasKey: false, model: "", enabledRobots: [] });
    await click(btn("Assistant"));
    await pick("Agent");
    expect([...drawer().querySelectorAll("button")].map((b) => b.textContent)).toContain("Save key and allow for this robot");
    expect(drawer().textContent).toContain("needs your own Anthropic key");
  });

  it("a typed robot description refused for no key lands on the key screen, with its plain line", async () => {
    await click(btn("Assistant"));
    await pick("Agent");
    api.startAgent.mockResolvedValueOnce({ ok: false, error: "The Assistant is off for this robot." });
    api.getAssistantSettings.mockResolvedValueOnce({ hasKey: false, model: "", enabledRobots: [] });
    const box = drawer().querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, "a four-wheel rover"); box.dispatchEvent(new Event("input", { bubbles: true })); });
    await click([...drawer().querySelectorAll("button")].find((b) => b.textContent === "Send")!);
    expect(drawer().textContent).toContain("sent only to Anthropic");
    expect([...drawer().querySelectorAll("button")].map((b) => b.textContent)).toContain("Save key and allow for this robot");
  });

  it("a typed power spec refused for no key lands on the key screen too", async () => {
    await click(btn("Assistant"));
    await pick("Agent");
    api.designPower.mockResolvedValueOnce({ ok: false, error: "The Assistant is off for this robot." });
    api.getAssistantSettings.mockResolvedValueOnce({ hasKey: false, model: "", enabledRobots: [] });
    const box = drawer().querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, "4S, 5 V 3 A, 12 V 1 A"); box.dispatchEvent(new Event("input", { bubbles: true })); });
    await click([...drawer().querySelectorAll("button")].find((b) => b.textContent === "Send")!);
    expect([...drawer().querySelectorAll("button")].map((b) => b.textContent)).toContain("Save key and allow for this robot");
  });

  it("a typed power spec starts power design with that spec; a plain question does not", async () => {
    await click(btn("Assistant"));
    await pick("Agent");
    const box = drawer().querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, "4S, 5 V 3 A, 12 V 1 A"); box.dispatchEvent(new Event("input", { bubbles: true })); });
    await click([...drawer().querySelectorAll("button")].find((b) => b.textContent === "Send")!);
    expect(api.designPower).toHaveBeenCalledWith("/r/lockwire.yaml", "4S, 5 V 3 A, 12 V 1 A", "rev1");
    expect(api.startAgent).not.toHaveBeenCalled();
    expect(["what is wrong?", "the hip draws 12 V 2 A, ok?", "4S pack", "why does my 4S pack sag to 12 V at 3 A?", "My 4S pack reads 16 V but sags to 12 V at 3 A, why?", "Is a 4S battery at 12 V 3 A ok for my servo?", "Can a 2S cell run 5 V 2 A on the wrist?"].map(isPowerSpec)).toEqual(Array(7).fill(false));
    expect(["4S, 5 V 3 A, 12 V 1 A", "4S 5V 3A 12V 1A", "4s: 5 V 3 A; 12 V 1 A", "4S, rails 5 V 3 A and 12 V 1 A"].map(isPowerSpec)).toEqual([true, true, true, true]);
  });

  it("the Agent tab sends, streams this robot's text, and stops", async () => {
    await click(btn("Assistant"));
    await pick("Agent");
    const box = drawer().querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, "what is wrong?"); box.dispatchEvent(new Event("input", { bubbles: true })); });
    await click([...drawer().querySelectorAll("button")].find((b) => b.textContent === "Send")!);
    expect(api.startAgent).toHaveBeenCalledWith("/r/lockwire.yaml", "what is wrong?", "rev1");
    await act(async () => { agentPush({ contract: "/other/lockwire.yaml", event: { type: "text", text: "not mine" } }); agentPush({ contract: "/r/lockwire.yaml", event: { type: "text", text: "The hip mass drifted" } }); });
    expect(drawer().textContent).toContain("The hip mass drifted");
    expect(drawer().textContent).not.toContain("not mine");
    await click([...drawer().querySelectorAll("button")].find((b) => b.textContent === "Stop")!);
    expect(api.stopAgent).toHaveBeenCalledWith("/r/lockwire.yaml");
    // plan first, then a card; "Review and sign" hands it to the Assistant tab's signing step
    const proposal = { id: "p1", command: "declare", args: {}, revision: "rev1", diff: "-mass: 1\n+mass: 2\n" };
    await act(async () => {
      agentPush({ contract: "/r/lockwire.yaml", event: { type: "text", text: "Plan: check the hip, then propose its mass" } });
      agentPush({ contract: "/r/lockwire.yaml", event: { type: "proposal", proposal, check: `${PRODUCT_NAME} check: PASS now, FAIL with this change.` } });
      agentPush({ contract: "/r/lockwire.yaml", event: { type: "done", usage: "about 12k tokens, ~$0.04 this session" } });
    });
    expect(drawer().querySelector("details summary")?.textContent).toBe("Plan");
    expect(drawer().textContent).toContain(`${PRODUCT_NAME} check: PASS now, FAIL with this change.`);
    expect(drawer().textContent).toContain("about 12k tokens, ~$0.04 this session");
    await click([...drawer().querySelectorAll("button")].find((b) => b.textContent === "Review and sign")!);
    expect(onReview).toHaveBeenCalledWith(proposal);
    expect(drawer().textContent).toContain("assistant body");
    expect([...drawer().querySelectorAll<HTMLElement>(".drawer-body")].filter((b) => !b.hidden).map((b) => b.textContent)).toEqual(["assistant body"]);
  });
});

describe("the panel slot", () => {
  const tabNames = () => [...drawer().querySelectorAll("[role=tab]")].map((t) => t.textContent);
  const render = () => act(async () => root.render(<App />));
  const testPanel = () => registerPanel({ id: "power", label: "Power", hasData: (p) => (p as { ok?: boolean }).ok === true, Body: () => <p>power body</p> });

  it("with nothing registered the top bar is Assistant only (no Panels) and the drawer has Assistant, Agent, Terminal on one row", async () => {
    page = { ok: true };
    await render();
    expect(btn("Panels")).toBeUndefined();
    await click(btn("Assistant"));
    expect(tabNames()).toEqual(["Assistant", "Agent", "Terminal"]);
  });

  it("a registered panel stays hidden until the robot has something for it, then opens on demand", async () => {
    testPanel();
    page = { ok: false };
    await render();
    await click(btn("Assistant"));
    expect(tabNames()).toEqual(["Assistant", "Agent", "Terminal"]);
    expect(drawer().textContent).not.toContain("power body");
    expect(btn("Panels")).toBeUndefined();
    page = { ok: true };
    await render();
    expect(tabNames()).toEqual(["Assistant", "Agent", "Terminal"]);
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
  it("the Assistant view is Assistant | Agent | Terminal and the Panels view is the data panels: two views, one row each", async () => {
    await withPanels("power", "controls", "wiring", "parts");
    await click(btn("Assistant"));
    expect([...drawer().querySelectorAll("[role=tab]")].map((t) => t.textContent)).toEqual(["Assistant", "Agent", "Terminal"]);
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
    await click(tabBtn("Agent"));
    await click(btn("Panels"));
    await click(btn("Assistant"));
    expect(selected()).toBe("Agent");
  });
  it("nothing opens by itself", async () => {
    await withPanels("power");
    expect(drawer().hidden).toBe(true);
  });
});
