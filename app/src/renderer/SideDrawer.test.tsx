import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PRODUCT_NAME } from "../shared/branding";
import { mockMatchMedia } from "./App.testkit";
import { Shell } from "./Shell";
import { SideDrawer } from "./SideDrawer";

vi.mock("@xterm/xterm", () => ({ Terminal: class { cols = 80; rows = 24; loadAddon() {} open() {} write() {} focus() {} dispose() {} onData() {} } }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });

let container: HTMLDivElement;
let root: Root;
let agentPush: (p: unknown) => void = () => {};
const api = { getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: true, model: "", enabledRobots: ["/r/lockwire.yaml"] }), setAssistantSettings: vi.fn(), startAgent: vi.fn().mockResolvedValue({ ok: true }), stopAgent: vi.fn(), onAgentEvent: vi.fn((cb: (p: unknown) => void) => { agentPush = cb; return () => {}; }), startTerminal: vi.fn().mockResolvedValue({ ok: true }), resizeTerminal: vi.fn(), writeTerminal: vi.fn(), killTerminal: vi.fn(), onTerminalEvent: vi.fn(() => () => {}) };

const onReview = vi.fn();
function App() {
  const [open, setOpen] = React.useState(false);
  return <Shell mode="Check" onModeChange={() => {}} connected locked assistant={{ open, onToggle: () => setOpen((o) => !o) }}
    drawer={<SideDrawer open={open} onClose={() => setOpen(false)} cwd="/r" contract="/r/lockwire.yaml" revision="rev1" onReview={onReview} assistant={<p>assistant body</p>} />} />;
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
    expect(drawer().textContent).toContain("This is a real shell on your computer. Commands run as you.");
    await act(async () => { drawer().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(drawer().hidden).toBe(true);
  });

  it("the Agent tab with no key shows the same key screen instead of failing", async () => {
    api.getAssistantSettings.mockResolvedValueOnce({ hasKey: false, model: "", enabledRobots: [] });
    await click([...container.querySelectorAll("button")].find((b) => b.textContent === "Assistant")!);
    await click([...drawer().querySelectorAll("[role=tab]")].find((b) => b.textContent === "Agent")!);
    expect([...drawer().querySelectorAll("button")].map((b) => b.textContent)).toContain("Save key and allow for this robot");
  });

  it("the Agent tab sends, streams this robot's text, and stops", async () => {
    await click([...container.querySelectorAll("button")].find((b) => b.textContent === "Assistant")!);
    await click([...drawer().querySelectorAll("[role=tab]")].find((b) => b.textContent === "Agent")!);
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
