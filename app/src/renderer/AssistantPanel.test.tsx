import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { DECLARE_FORM, PAGE, mockMatchMedia, picked } from "./App.testkit";
import { AssistantPanel, EMPTY_CHAT, type Chat } from "./AssistantPanel";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { Shell } from "./Shell";

vi.mock("./scene/Canvas", () => ({ Canvas: ({ children }: { children?: React.ReactNode }) => <div data-testid="scene-canvas">{children}</div>, useScene: () => ({}) }));
vi.mock("./scene/robotScene", async (orig) => ({ ...(await orig<typeof import("./scene/robotScene")>()), applyHighlight: vi.fn(), applyDim: vi.fn(), focusCamera: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
const settings = { hasKey: true, model: "claude-sonnet-5-5", enabledRobots: ["/r.yaml"] };
const turn = { text: "Try this", proposals: [{ id: "p1", command: "declare", args: { path: "mass", value: 2 }, revision: "r", diff: "--- a\n+++ a\n-mass: 1\n+mass: 2\n" }] };

function bridge(over: Record<string, unknown> = {}) {
  const api = {
    getAssistantSettings: vi.fn().mockResolvedValue(settings),
    setAssistantSettings: vi.fn().mockResolvedValue({ settings }),
    sendAssistant: vi.fn().mockResolvedValue(turn),
    applyAssistant: vi.fn().mockResolvedValue({ ok: true }),
    cancelAssistant: vi.fn(),
    onAssistantEvent: vi.fn(() => () => {}),
    ...over,
  };
  Object.defineProperty(window, "lockwire", { configurable: true, value: api });
  return api;
}
// A stand-in for App's state, so the panel can be unmounted and remounted over the same chat.
let chat: Chat;
const panel = (witness = "Ada") => <AssistantPanel contract="/r.yaml" revision="r" mode="Check" witness={witness} chat={chat} onChat={(f) => { chat = f(chat); void root.render(panel(witness)); }} onApplied={async () => {}} />;
async function openApp() {
  const api = bridge({ startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/r.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
    getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) });
  mockMatchMedia();
  await mount(<App />);
  await tap("Open robot");
  await act(async () => {});
  return api;
}
const mount = async (el: React.ReactElement) => { root = createRoot(container); await act(async () => root.render(el)); };
const type = async (label: string, value: string) => {
  const input = [...container.querySelectorAll("label")].find((l) => l.textContent?.startsWith(label))!.querySelector("input,textarea") as HTMLInputElement;
  const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")!.set!;
  await act(async () => { set.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
};
const tap = async (name: string) => act(async () => { [...container.querySelectorAll("button")].find((b) => b.textContent === name)!.click(); });

beforeEach(() => { chat = EMPTY_CHAT; container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); Reflect.deleteProperty(window, "lockwire"); });

describe("AssistantPanel", () => {
  it("a tool event from robot A's send does not mark robot B busy", async () => {
    let finish!: (t: typeof turn) => void;
    const live = new Set<(e: { type: "tool"; name: string }) => void>();
    bridge({ sendAssistant: vi.fn(() => new Promise((r) => { finish = r; })),
      getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, enabledRobots: ["/a.yaml", "/b.yaml"] }),
      onAssistantEvent: vi.fn((cb) => { live.add(cb); return () => live.delete(cb); }) });
    const chats: Record<string, Chat> = {};
    const view = (c: string) => <AssistantPanel contract={c} revision="r" mode="Check" witness="Ada" chat={chats[c] ?? EMPTY_CHAT} onApplied={async () => {}}
      onChat={(f) => { chats[c] = f(chats[c] ?? EMPTY_CHAT); void root.render(view(c)); }} />;
    await mount(view("/a.yaml"));
    await type("Ask the Assistant", "hi");
    await tap("Send");
    await act(async () => root.render(view("/b.yaml")));
    await act(async () => live.forEach((cb) => cb({ type: "tool", name: "read" })));
    expect(chats["/b.yaml"]?.busy ?? null).toBeNull();
    await act(async () => finish(turn));
  });

  it("is closed by default: no settings call, no right slot", async () => {
    const api = bridge();
    window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) as never;
    await mount(<Shell mode="Check" onModeChange={() => {}} connected assistant={{ open: false, onToggle: () => {} }} />);
    expect(api.getAssistantSettings).not.toHaveBeenCalled();
    expect(container.querySelector(".kit-slot-right")).toBeNull();
  });

  it("App: closed means no settings call and no slot; the thread survives close and reopen; Docs stays beside it", async () => {
    const api = await openApp();
    expect(api.getAssistantSettings).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("Ask the Assistant"); // the right slot holds the Check body here; the bare-Shell test above asserts no slot
    await tap("Assistant");
    await type("Ask the Assistant", "hi");
    await tap("Send");
    await tap("Assistant");
    expect(container.textContent).not.toContain("Ask the Assistant");
    await tap("Assistant");
    expect(container.textContent).toContain("Try this");
    expect(container.textContent).toContain("declare");
    await act(async () => ([...container.querySelectorAll('[role="tab"]')].find((t) => t.textContent === "Docs") as HTMLElement).click());
    expect(container.querySelector('[aria-label="Docs view"]')).not.toBeNull(); // it used to step aside
    expect(container.textContent).toContain("Ask the Assistant");
  });

  it("asks for the key on first open and saves it with the model", async () => {
    const api = bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, hasKey: false }) });
    await mount(panel());
    expect(container.textContent).toContain("console.anthropic.com");
    await type("Paste your Anthropic key", "sk-x");
    await tap("Save");
    expect(api.setAssistantSettings).toHaveBeenCalledWith({ key: "sk-x" });
  });

  it("the model starts empty and Send is off until it is filled", async () => {
    const api = bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, model: "" }) });
    await mount(panel());
    await type("Ask the Assistant", "hi");
    const sendBtn = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Send")!;
    expect(sendBtn().disabled).toBe(true);
    await type("Model", "m-9");
    expect(sendBtn().disabled).toBe(false);
    await tap("Send");
    expect(api.setAssistantSettings).toHaveBeenCalledWith({ model: "m-9" });
  });

  it("sends on a robot that is not enabled only after the consent tap", async () => {
    const api = bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, enabledRobots: [] }) });
    await mount(panel());
    await type("Ask the Assistant", "hi");
    await tap("Send");
    expect(container.textContent).toContain("Send this robot's contract to Anthropic with your key?");
    expect(api.sendAssistant).not.toHaveBeenCalled();
    await tap("Turn on for this robot");
    expect(api.setAssistantSettings).toHaveBeenCalledWith({ enableRobot: "/r.yaml" });
    expect(api.sendAssistant).toHaveBeenCalledWith({ contract: "/r.yaml", revision: "r", mode: "Check", text: "hi" });
  });

  it("Not now drops the pending send", async () => {
    const api = bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, enabledRobots: [] }) });
    await mount(panel());
    await type("Ask the Assistant", "hi");
    await tap("Send");
    await tap("Not now");
    expect(container.textContent).not.toContain("Send this robot's contract");
    expect(api.sendAssistant).not.toHaveBeenCalled();
  });

  it("renders a proposal, applies it with the witness and shows a refusal as one line", async () => {
    const api = bridge({ applyAssistant: vi.fn().mockResolvedValue({ ok: false, error: "stale revision" }) });
    await mount(panel(""));
    await type("Ask the Assistant", "fix");
    await tap("Send");
    expect(container.textContent).toContain("declare");
    await type("Your name", "Bo");
    await tap("Apply");
    expect(api.applyAssistant).toHaveBeenCalledWith({ id: "p1", witness: "Bo" });
    expect(container.textContent).toContain("Contract changed, ask again");
  });

  it("the card shows the diff, and the witness is a visible field the page's prefill only suggests", async () => {
    bridge();
    await mount(panel("Ada"));
    await type("Ask the Assistant", "fix");
    await tap("Send");
    expect(container.querySelector(".assistant-diff")!.textContent).toContain("+mass: 2");
    expect(container.querySelector(".assistant-diff [data-line='add']")!.textContent).toBe("+mass: 2");
    expect(container.querySelector(".assistant-diff [data-line='del']")!.textContent).toBe("-mass: 1");
    expect(container.textContent).not.toContain("value: 2"); // no raw args
    const name = [...container.querySelectorAll("label")].find((l) => l.textContent?.startsWith("Your name"))!.querySelector("input")!;
    expect(name.value).toBe("Ada");
    await type("Your name", "");
    expect([...container.querySelectorAll("button")].find((b) => b.textContent === "Apply")!.disabled).toBe(true);
    await type("Your name", "Bo");
    await tap("Apply");
    expect(window.lockwire.applyAssistant).toHaveBeenCalledWith({ id: "p1", witness: "Bo" });
  });

  it("with the Assistant open in Check mode the findings stay in the DOM", async () => {
    await openApp();
    expect(container.textContent).toContain("Findings");
    await tap("Assistant");
    expect(container.textContent).toContain("Ask the Assistant");
    expect(container.textContent).toContain("Findings");
    expect(container.textContent).toContain("Pm02 5 V rail overloaded");
  });
});
