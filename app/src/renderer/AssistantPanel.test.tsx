import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { DECLARE_FORM, PAGE, goMode, mockMatchMedia, picked } from "./App.testkit";
import { AssistantPanel, EMPTY_CHAT, type Chat } from "./AssistantPanel";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { setSelectedPartId } from "./scene/selectionStore";
import { Shell } from "./Shell";

vi.mock("./scene/Canvas", () => ({ Canvas: ({ children }: { children?: React.ReactNode }) => <div data-testid="scene-canvas">{children}</div>, useScene: () => ({ meshes: [] }) }));
vi.mock("./scene/robotScene", async (orig) => ({ ...(await orig<typeof import("./scene/robotScene")>()), applyHighlight: vi.fn(), applyDim: vi.fn(), focusCamera: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
const settings = { hasKey: true, model: "claude-sonnet-5-5", enabledRobots: ["/r.yaml"] };
const now = () => new Date().toISOString();
const turn = { text: "Try this", verdict: { word: "NO-GO", at: now() }, proposals: [{ id: "p1", command: "declare", args: { path: "mass", value: 2 }, revision: "r", diff: "--- a\n+++ a\n-mass: 1\n+mass: 2\n" }] };
const RAIL = "topology/rails/rail.5v/brownout", FUSE = "topology/wires/w.pack/fuse";
// The page's own check, as App holds it: each card's `quantity` is the engine's path, its title and body the plain words.
const check = { verdict: "NO-GO", violations: 2, warnings: 0, cards: [
  { quantity: RAIL, raw: `VIOLATION ${RAIL}`, title: "5 V rail sags under load", body: "It sags to 4.65 V. It needs 4.75 V. Everything on it browns out." },
  { quantity: FUSE, raw: `VIOLATION ${FUSE}`, title: "Fuse too big for its wire", body: "The fuse is 9 A and the wire is good for 7 A. The wire burns first." },
] } as never;
const found = [{ quantity: RAIL, severity: "violation" }, { quantity: FUSE, severity: "violation" }, { quantity: "model/sanity", severity: "not_scored" }];

function bridge(over: Record<string, unknown> = {}) {
  const api = {
    getAssistantSettings: vi.fn().mockResolvedValue(settings),
    setAssistantSettings: vi.fn().mockResolvedValue({ settings }),
    sendAssistant: vi.fn().mockResolvedValue(turn),
    applyAssistant: vi.fn().mockResolvedValue({ ok: true }),
    cancelAssistant: vi.fn(),
    showAssistantFinding: vi.fn().mockResolvedValue({ ok: true }),
    writeContract: vi.fn().mockResolvedValue({ payload: { written: true }, refusal: null }),
    getDeclareForm: vi.fn().mockResolvedValue({ rows: [], witness: { prefill: "Bo (default)" } }),
    onAssistantEvent: vi.fn(() => () => {}),
    ...over,
  };
  Object.defineProperty(window, "lockwire", { configurable: true, value: api });
  return api;
}
// A stand-in for App's state, so the panel can be unmounted and remounted over the same chat.
let chat: Chat;
const panel = (witness = "Ada (default)", more: Partial<React.ComponentProps<typeof AssistantPanel>> = {}): React.ReactElement =>
  <AssistantPanel contract="/r.yaml" revision="r" mode="Check" witness={witness} chat={chat} onChat={(f) => { chat = f(chat); void root.render(panel(witness, more)); }} onApplied={async () => {}} {...more} />;
const ASK = "Ask about this robot";
const ask = async (text: string) => { await type(ASK, text); await tap("Send"); };
const says = (api: ReturnType<typeof bridge>, t: object) => api.sendAssistant.mockResolvedValue({ ...turn, proposals: [], ...t });
const text = (sel: string) => [...container.querySelectorAll(sel)].map((e) => e.textContent);
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
  const input = (container.querySelector(`[aria-label="${label}"]`) ?? [...container.querySelectorAll("label")].find((l) => l.textContent?.startsWith(label))!.querySelector("input,textarea")) as HTMLInputElement;
  const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")!.set!;
  await act(async () => { set.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
};
const tap = async (name: string) => act(async () => { [...container.querySelectorAll("button")].find((b) => b.textContent === name || b.getAttribute("aria-label") === name)!.click(); });

beforeEach(() => { chat = EMPTY_CHAT; container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); Reflect.deleteProperty(window, "lockwire"); });

describe("AssistantPanel", () => {
  it("Enter sends and Shift+Enter does not; the arrow is the Send button", async () => {
    const api = bridge({});
    await mount(panel());
    await type(ASK, "why?");
    const box = container.querySelector(`[aria-label="${ASK}"]`)!;
    await act(async () => { box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true })); });
    expect(api.sendAssistant).not.toHaveBeenCalled();
    await act(async () => { box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })); });
    expect(api.sendAssistant).toHaveBeenCalledTimes(1);
    expect((box as HTMLTextAreaElement).rows).toBe(1); // the box starts as one line
    expect(container.querySelector('button[aria-label="Send"]')?.getAttribute("title")).toBe("Send (Enter)");
  });

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
    await ask("hi");
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
    const open = () => container.querySelector(`[aria-label="${ASK}"]`) !== null; // the question box
    expect(open()).toBe(false); // the right slot holds the Check body here; the bare-Shell test above asserts no slot
    await tap("Assistant");
    await ask("hi");
    await tap("Assistant");
    expect(open()).toBe(false);
    await tap("Assistant");
    expect(container.textContent).toContain("Try this");
    expect(container.querySelector(".assistant-card")).not.toBeNull();
    goMode(container, "Docs");
    expect(container.querySelector('[aria-label="Docs view"]')).not.toBeNull(); // it used to step aside
    expect(open()).toBe(true);
  });

  it("first use is one screen: the key, one tap that saves it and allows this robot, then the question", async () => {
    const api = bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ hasKey: false, model: "", enabledRobots: [] }) });
    await mount(panel());
    expect(container.textContent).toContain("The Assistant can use this robot's check results."); // no file tools in the tool table: files are not promised
    expect(container.textContent).toContain("Your robot's contract is sent to Anthropic with your key.");
    expect(container.textContent).toContain("needs your own Anthropic key (get one at console.anthropic.com). The key is saved encrypted on this computer and sent only to Anthropic.");
    await type("Paste your Anthropic key", "sk-x");
    await tap("Save key and allow for this robot");
    expect(api.setAssistantSettings).toHaveBeenCalledWith({ key: "sk-x", enableRobot: "/r.yaml" });
    expect(container.querySelector("details:not([open]) input")).not.toBeNull(); // Model: behind Advanced, on its default
    await ask("hi");
    expect(api.sendAssistant).toHaveBeenCalledWith({ contract: "/r.yaml", revision: "r", mode: "Check", text: "hi" });
    expect(container.textContent).not.toContain("Turn on for this robot");
  });

  it("a second robot, key already saved: one tap allows it, no key field", async () => {
    const api = bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, enabledRobots: [], files: true }) });
    await mount(panel());
    expect(container.textContent).toContain("The Assistant can use this robot's check results and files in this robot's folder.");
    expect(container.querySelector('input[type="password"]')).toBeNull();
    await tap("Allow for this robot");
    expect(api.setAssistantSettings).toHaveBeenCalledWith({ enableRobot: "/r.yaml" });
  });

  it("apply uses the saved name: one press, the app's own name; with none saved the first Apply asks once, saves it where the app keeps it, then applies", async () => {
    const api = bridge({ applyAssistant: vi.fn().mockResolvedValue({ ok: false, error: "stale revision" }) });
    await mount(panel("Ada (default)"));
    await ask("fix");
    expect(text(".assistant-as")).toEqual(["as Ada"]);
    await tap("Apply");
    expect(api.applyAssistant).toHaveBeenCalledWith({ id: "p1", witness: "Ada (default)" }); // the name as the app holds it
    expect(container.textContent).toContain("The robot file changed. Ask again."); // a refusal is one line
    act(() => root.unmount());
    chat = EMPTY_CHAT;
    const onName = vi.fn();
    const none = bridge();
    await mount(panel("", { onName }));
    await ask("fix");
    await tap("Apply");
    expect(none.applyAssistant).not.toHaveBeenCalled(); // no name yet: nothing is written
    await type("Your name, recorded with every change", "Bo");
    await tap("Save");
    expect(none.writeContract).toHaveBeenCalledWith({ command: "set-witness", args: { name: "Bo" } }); // the app's one store
    expect(onName).toHaveBeenCalledWith({ rows: [], witness: { prefill: "Bo (default)" } });
    expect(none.applyAssistant).toHaveBeenCalledWith({ id: "p1", witness: "Bo (default)" });
    expect(container.textContent).toContain("Applied");
  });

  it("the folded exact change shows the saved name where the dry run had its stand-in, or leaves the witness out with none saved; Apply still sends the saved name", async () => {
    const diff = '+    note: "Stall draw.\\n2026-11-16: draw declared, witness your name"\n+    witness: your name\n+    draw: 2\n';
    const api = bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [{ ...turn.proposals[0], diff }] }) });
    await mount(panel("Ada (default)"));
    await ask("fix");
    expect(text(".assistant-diff span")).toEqual(['+    note: "Stall draw.\\n2026-11-16: draw declared, witness Ada"', "+    witness: Ada", "+    draw: 2"]);
    await tap("Apply");
    expect(api.applyAssistant).toHaveBeenCalledWith({ id: "p1", witness: "Ada (default)" });
    act(() => root.unmount());
    chat = EMPTY_CHAT;
    bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [{ ...turn.proposals[0], diff }] }) });
    await mount(panel(""));
    await ask("fix");
    expect(text(".assistant-diff span")).toEqual(['+    note: "Stall draw.\\n2026-11-16: draw declared"', "+    draw: 2"]);
  });

  it("the saved name can be changed: 'Applying as' under Advanced, Change opens the same field, saves it where the app keeps it, and applies nothing; no name saved, no line", async () => {
    const onName = vi.fn();
    const api = bridge();
    await mount(panel("Ada (default)", { onName }));
    expect(container.querySelector("details")!.textContent).toContain("Applying as Ada");
    await tap("Change");
    await type("Your name, recorded with every change", "Bo");
    await tap("Save");
    expect(api.writeContract).toHaveBeenCalledWith({ command: "set-witness", args: { name: "Bo" } });
    expect(onName).toHaveBeenCalledWith({ rows: [], witness: { prefill: "Bo (default)" } });
    expect(api.applyAssistant).not.toHaveBeenCalled();
    expect(container.querySelector(".assistant-name")).toBeNull(); // the field closes once saved
    act(() => root.unmount());
    bridge();
    await mount(panel(""));
    expect(container.textContent).not.toContain("Applying as");
  });

  it("a card with main's own note shows it as one plain line above before and after; without one nothing extra shows", async () => {
    const comparison = { before: "NO-GO", after: "GO", changes: [] };
    bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [{ ...turn.proposals[0], comparison, note: "Numbers are as the assistant read them." }, { ...turn.proposals[0], id: "p2", comparison }] }) });
    await mount(panel("Ada (default)"));
    await ask("fix");
    const [noted, plain] = [...container.querySelectorAll(".assistant-card")];
    const line = [...noted!.children].find((c) => c.textContent === "Numbers are as the assistant read them.");
    expect(line?.className).toBe("assistant-note");
    expect([...noted!.children].indexOf(line!)).toBeLessThan([...noted!.children].indexOf(noted!.querySelector(".assistant-sides")!));
    expect(plain!.children.length).toBe(noted!.children.length - 1);
  });

  it("no name field on a card: plain title, one Apply, a quiet Dismiss, the exact change behind one tap, and no word about a name on a swap", async () => {
    const swap = { id: "p2", command: "swap", args: { joint: "wrist", part_id: "dynamixel.xl330-m288" }, revision: "r", diff: "-wrist: a\n+wrist: b\n", proved: true };
    bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [turn.proposals[0], swap] }) });
    await mount(panel("Ada (default)", { rows: [{ path: "mass", short: "Model mass", group: "Hand", unit: "kg", value: 1 }] as never }));
    await ask("fix");
    const [declare, swapped] = [...container.querySelectorAll(".assistant-card")];
    expect(container.querySelectorAll(".assistant-card input").length).toBe(0);
    expect(text(".assistant-card .kit-btn-primary")).toEqual(["Apply", "Apply"]);
    expect(text(".assistant-card-head strong")).toEqual(["Change model mass on Hand", "Swap the wrist servo"]); // never the command's name
    expect(declare!.textContent).toContain("1 kg → 2 kg");
    expect(declare!.textContent).not.toContain("Proved by the engine");
    expect(swapped!.textContent).toContain("Proved by the engine");
    expect(swapped!.textContent).not.toMatch(/as Ada|name/i); // the engine records no name for a swap
    expect(container.textContent).not.toMatch(/\bsign/i);
    expect(declare!.querySelector("details:not([open]) .assistant-diff [data-line='add']")!.textContent).toBe("+mass: 2");
    await tap("Dismiss");
    expect(container.querySelectorAll(".assistant-card").length).toBe(1);
  });

  it("an applied card keeps its change line as it read when it came, not the file's new value (it read 7 A → 7 A)", async () => {
    const fuse = { path: "topology/rails/rail.battery/current_limit", short: "Current limit", group: "Battery", unit: "A" };
    bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [{ ...turn.proposals[0], args: { path: fuse.path, value: "7 A" } }] }) });
    const more = { rows: [{ ...fuse, value: 9 }] as never };
    await mount(panel("Ada (default)", more));
    await ask("and the fuse?");
    await tap("Apply");
    more.rows = [{ ...fuse, value: 7 }] as never; // the page's declare rows after the write
    await act(async () => root.render(panel("Ada (default)", more)));
    const card = container.querySelector(".assistant-card")!;
    expect(card.textContent).toContain("9 A → 7 A");
    expect(card.textContent).toContain("Change current limit on Battery");
  });

  it("every card says its change by the page's names, old to new with the unit; a part card shows its citations; no id and no full path on screen", async () => {
    const C = "/Users/someone/robots/arm/lockwire.yaml";
    const wire = { id: "w", command: "declare", args: { path: "topology/wires/w.servo-feed/awg", value: 22 }, revision: "r", diff: `--- ${C}\n+++ ${C}\n-    awg: 28\n+    awg: 22\n`,
      comparison: { before: "NO-GO", after: "NO-GO", changes: [{ kind: "changed", severity: "violation", quantity: "topology/rails/rail.5v/sag", observed: "rail.5v holds 4.91 V", expected: ">= 4.75 V for dev.jetson", was: "rail.5v sags to 4.65 V" }] } };
    const swap = { id: "s", command: "swap", args: { joint: "wrist", part_id: "dynamixel.xl330-m288" }, revision: "r", diff: "-  wrist: feetech.scs0009\n", swap: { from: { id: "feetech.scs0009", name: "Feetech SC-0090-C001 smart servo (SCS0009)" }, to: { id: "dynamixel.xl330-m288", name: "ROBOTIS Dynamixel XL330-M288-T smart servo" } },
      comparison: { before: "NO-GO", after: "NO-GO", changes: [{ kind: "cleared", severity: "violation", quantity: "model/joints/wrist/limits/effort", observed: "0.40 N*m", expected: "<= 0.226 N*m, the feetech.scs0009 stall torque" }] } };
    const part = { id: "p", command: "add", args: { kind: "device", id: "dev.lw_t1", fields: { model: "LW-T1" } }, revision: "r", diff: "+x\n", note: "Not checked against the file.",
      cites: [{ name: "stall_torque", value: 0.52, unit: "N*m", page: 1, quote: "Stall torque 0.52 N*m at 6 V" }] };
    bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, enabledRobots: [C] }), sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [wire, swap, part] }), applyAssistant: vi.fn().mockResolvedValue({ ok: false, error: `no file at ${C}` }) });
    await mount(panel("Ada (default)", { contract: C, labels: { "w.servo-feed": "Servo Feed", "rail.5v": "5 V", "dev.jetson": "Jetson" } }));
    await ask("fix");
    const [w, s, p] = [...container.querySelectorAll(".assistant-card")].map((e) => e.textContent!);
    expect(w).toContain("Change wire gauge on Servo Feed28 AWG → 22 AWG");
    expect(w).toContain("A violation: 5 V holds 4.91 V, needs >= 4.75 V for Jetson"); // no page card: the engine's sentence, its ids as the page names them
    expect(s).toContain("Swap the wrist servoFeetech SC-0090-C001 smart servo (SCS0009) → ROBOTIS Dynamixel XL330-M288-T smart servo");
    expect(s).toContain("needs <= 0.226 N*m, the Feetech SC-0090-C001 smart servo (SCS0009) stall torque"); // the catalog id in the engine's sentence, by its catalog name
    expect(p).toContain("Add a partLW-T1stall torque: 0.52 N*m, p.1, “Stall torque 0.52 N*m at 6 V”Not checked against the file.");
    await tap("Apply");
    expect(container.textContent).toContain("no file at lockwire.yaml");
    expect(text(".assistant-diff")[0]).toBe("--- lockwire.yaml+++ lockwire.yaml-    awg: 28+    awg: 22");
    expect(container.textContent).not.toMatch(/someone|rail\.5v|dev\.jetson|w\.servo-feed/); // the folded exact change may name the catalog id: it is the file
  });

  it("a part with no graph name reads by its declare rows' group, the id only when neither exists; a path in either macOS spelling of the folder, or outside it, shows no folders (review minors)", async () => {
    const C = "/private/tmp/lw/arm/lockwire.yaml";
    const card = (id: string, wire: string) => ({ id, command: "declare", args: { path: `topology/wires/${wire}/awg`, value: 22 }, revision: "r", diff: "-    awg: 28\n+    awg: 22\n" });
    bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, enabledRobots: [C] }), sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [card("a", "w.spare"), card("b", "w.nameless")] }),
      applyAssistant: vi.fn().mockResolvedValue({ ok: false, error: "no file at /tmp/lw/arm/lockwire.yaml; see /opt/other/notes.txt and https://example.com/a/b" }) });
    await mount(panel("Ada (default)", { contract: C, rows: [{ path: "topology/wires/w.spare/length", group: "Spare Lead", short: "Length", unit: "m", value: 1 }] as never }));
    await ask("fix");
    expect(text(".assistant-card-head strong")).toEqual(["Change wire gauge on Spare Lead", "Change wire gauge on w.nameless"]);
    await tap("Apply");
    expect(container.textContent).toContain("no file at lockwire.yaml; see /…/notes.txt and https://example.com/a/b");
  });

  it("re-check at signing: Apply that wrote nothing because the robot changed shows the new Before and After and main's sentence in the same card, one Apply, still open; the next press applies", async () => {
    const shown = { before: "NO-GO", after: "GO", changes: [{ kind: "cleared" as const, severity: "violation", quantity: FUSE, observed: "9 A", expected: "<= 7 A" }] };
    const fresh = { before: "NO-GO", after: "NO-GO", changes: [{ kind: "changed" as const, severity: "violation", quantity: FUSE, observed: "9 A", expected: "<= 5 A", was: "9 A" }] };
    const said = "The robot changed since this card was made. Look at its Before and After again, then press Apply once more.";
    const api = bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [{ ...turn.proposals[0]!, comparison: shown, proved: true }] }),
      applyAssistant: vi.fn().mockResolvedValueOnce({ ok: false, error: said, comparison: fresh }).mockResolvedValueOnce({ ok: true }) });
    await mount(panel("Ada (default)", { check }));
    await ask("fix");
    const sides = () => [...container.querySelectorAll(".assistant-side")].map((e) => e.textContent);
    expect(sides()).toEqual(["BeforeNO-GOFuse too big for its wire", "AfterGOFuse too big for its wire: cleared"]);
    await tap("Apply");
    expect(sides()).toEqual(["BeforeNO-GOFuse too big for its wire", "AfterNO-GOFuse too big for its wire: 9 A, needs <= 5 A"]);
    expect(container.querySelector(".assistant-card [role=status]")?.textContent).toBe(said);
    expect(container.textContent).not.toContain("Proved by the engine"); // its proof was for the robot as it was
    expect([...container.querySelectorAll(".assistant-card button")].map((b) => b.textContent)).toEqual(["Apply", "Dismiss"]); // the approved card: one Apply
    await tap("Apply");
    expect(api.applyAssistant).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("Applied");
    expect(container.textContent).not.toContain(said);
  });

  it("a card's Apply is off while its own apply is in flight: two quick presses make one apply call, and the card ends showing the re-shown comparison, not 'gone'; Dismiss stays usable", async () => {
    const shown = { before: "NO-GO", after: "GO", changes: [] };
    const fresh = { before: "NO-GO", after: "NO-GO", changes: [] };
    let answer!: (r: unknown) => void;
    const api = bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [{ ...turn.proposals[0]!, comparison: shown }] }),
      applyAssistant: vi.fn(() => new Promise((r) => (answer = r)) as never) });
    await mount(panel("Ada (default)", { check }));
    await ask("fix");
    const button = (t: string) => [...container.querySelectorAll(".assistant-card button")].find((b) => b.textContent === t) as HTMLButtonElement;
    await act(async () => { button("Apply").click(); button("Apply").click(); }); // the same frame
    await tap("Apply");
    expect(api.applyAssistant).toHaveBeenCalledTimes(1);
    expect([button("Apply").disabled, button("Dismiss").disabled]).toEqual([true, false]);
    await act(async () => answer({ ok: false, error: "The robot changed since this card was made. Look at its Before and After again, then press Apply once more.", comparison: fresh }));
    expect(container.querySelector(".assistant-card [role=status]")?.textContent).toMatch(/^The robot changed/);
    expect(container.textContent).not.toContain("gone");
    expect(button("Apply").disabled).toBe(false);
  });

  it("the model's answer shows a path relative to the robot's folder, a path outside it as its file name; the person's own words stay as typed", async () => {
    const C = "/private/tmp/lw/arm/lockwire.yaml";
    bridge({ getAssistantSettings: vi.fn().mockResolvedValue({ ...settings, enabledRobots: [C] }), sendAssistant: vi.fn().mockResolvedValue({ ...turn, proposals: [], text: "Run lockwire swap /tmp/lw/arm/lockwire.yaml wrist, as /Users/someone/notes.txt says." }) });
    await mount(panel("Ada (default)", { contract: C }));
    await ask("what about /Users/someone/arm.urdf?");
    expect(text(".assistant-turn")).toEqual(["what about /Users/someone/arm.urdf?", "Run lockwire swap lockwire.yaml wrist, as /…/notes.txt says."]);
  });

  it("card shows before and after side by side: the engine's two verdicts, findings by their plain title, six then a count, and a failed comparison can still be applied", async () => {
    const rail = (i: number) => ({ kind: "new" as const, severity: "violation", quantity: `rail/${i}`, observed: `${i} W`, expected: "<= 0 W" });
    const moved = { kind: "changed" as const, severity: "violation", quantity: RAIL, observed: "4.70 V", expected: ">= 4.75 V", was: "4.65 V" };
    const gone = { kind: "cleared" as const, severity: "violation", quantity: FUSE, observed: "9 A", expected: "<= 7 A" };
    const comparisons = [{ before: "NO-GO", after: "GO_WITH_NOTES", changes: [moved, gone, ...[1, 2, 3, 4, 5, 6].map(rail)] }, { before: "GO", after: "GO", changes: [] }, { error: "Lockwire could not compare before and after. You can still apply this change." }];
    bridge({ sendAssistant: vi.fn().mockResolvedValue({ ...turn, text: "", proposals: comparisons.map((comparison, i) => ({ ...turn.proposals[0]!, id: `p${i}`, comparison })) }) });
    await mount(panel("Ada (default)", { check }));
    await ask("fix");
    const [many, none, failed] = [...container.querySelectorAll(".assistant-card")];
    const [before, after] = [...many!.querySelectorAll(".assistant-side")].map((e) => e.textContent!);
    expect(before).toBe("BeforeNO-GO5 V rail sags under loadFuse too big for its wire"); // the Check screen's own titles
    expect(after).toContain("AfterGO with notes5 V rail sags under load: 4.70 V, needs >= 4.75 VFuse too big for its wire: clearedNew: A violation: 1 W, needs <= 0 W");
    expect(many!.textContent).toContain("and 2 more");
    expect(many!.textContent).not.toMatch(/rail\/|topology/); // never a path
    expect(none!.textContent).toContain("Nothing changes in the check.");
    expect(failed!.textContent).toContain("Lockwire could not compare before and after.");
    expect([...container.querySelectorAll("button")].filter((b) => b.textContent === "Apply").map((b) => b.disabled)).toEqual([false, false, false]);
  });

  it("the verdict pill is the engine's: the turn's verdict field and its faults, never the model's words; before a question and after an Apply, the page's own check", async () => {
    const api = bridge();
    await mount(panel("Ada (default)", { check }));
    expect(text(".assistant-verdict")).toEqual(["NO-GO · 2 violationsfrom the check"]); // the page's check, before any question
    says(api, { text: "This robot is GO.", verdict: { word: "GO_WITH_NOTES", at: now() }, findings: [{ quantity: "a", severity: "warning" }] });
    await ask("is it ok?");
    expect(text(".assistant-verdict")).toEqual(["GO with notes · 1 warningfrom the check, just now"]);
    says(api, { text: "Late.", verdict: { word: "GO", at: "2026-01-01T00:00:00.000Z" } });
    await ask("an older turn lands late");
    expect(text(".assistant-verdict")).toEqual(["GO with notes · 1 warningfrom the check, just now"]); // the verdict taken last stays
    says(api, { text: "It is GO.", verdict: { word: "unknown", at: now(), reason: "Lockwire's engine did not answer in time, so this robot was not checked." }, proposals: turn.proposals });
    await ask("and now?");
    expect(text(".assistant-verdict")).toEqual(["Not checkedLockwire's engine did not answer in time, so this robot was not checked."]);
    await tap("Apply");
    expect(text(".assistant-verdict")).toEqual(["NO-GO · 2 violationsfrom the check"]); // a write makes the turn's verdict old
  });

  it("a verdict from before an Apply never shows: Apply waits while a question is in flight, and a turn whose check is older than the last Apply is ignored", async () => {
    const api = bridge();
    await mount(panel("Ada (default)", { check }));
    says(api, { text: "A card.", verdict: { word: "NO-GO", at: now() }, proposals: turn.proposals });
    await ask("fix?");
    let land: (t: unknown) => void = () => {};
    api.sendAssistant.mockReturnValueOnce(new Promise((r) => (land = r)));
    await ask("still pending");
    const apply = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Apply")!;
    expect(apply().disabled).toBe(true); // like Send while a question is in flight
    await act(async () => land({ ...turn, proposals: [], text: "Late.", verdict: { word: "GO", at: now() } }));
    expect(apply().disabled).toBe(false);
    await tap("Apply");
    expect(text(".assistant-verdict")).toEqual(["NO-GO · 2 violationsfrom the check"]); // the page's own check after a write
    says(api, { text: "Old.", verdict: { word: "GO", at: "2026-01-01T00:00:00.000Z" } }); // a check taken before the Apply
    await ask("and now?");
    expect(text(".assistant-verdict")).toEqual(["NO-GO · 2 violationsfrom the check"]);
  });

  it("unverified marks: each item main could not ground is underlined and labelled, whole words only, and the answer's words are not changed", async () => {
    const answer = "The rail holds 4.9 V at 3-5 A, so it is GO with notes, not NO-GO.";
    says(bridge(), { text: answer, ungrounded: ["4.9 V", "5 A", "GO_WITH_NOTES"] });
    await mount(panel());
    await ask("why?");
    const marks = [...container.querySelectorAll<HTMLElement>(".assistant-unverified")];
    expect(marks.map((m) => [m.firstChild!.textContent, m.title, m.querySelector(".kit-sr")!.textContent])).toEqual([["4.9 V", "unverified", " (unverified)"], ["5 A", "unverified", " (unverified)"], ["GO with notes", "unverified", " (unverified)"]]);
    marks.forEach((m) => m.querySelector(".kit-sr")!.remove());
    expect(container.querySelector('.assistant-turn[data-who="assistant"]')!.textContent).toBe(answer);
    says(bridge(), { text: "NO-GO: not a GO.", ungrounded: ["GO"] });
    await ask("again?");
    expect(text('.assistant-turn[data-who="assistant"]:last-of-type .assistant-unverified')).toEqual(["GO (unverified)"]); // the GO inside NO-GO is not marked
  });

  it("a verdict only a proposal supports has the same mark, saying it is the robot after the change, not now (review F1)", async () => {
    const answer = "It is NO-GO now. After that change the check would be GO with notes, about 9 kg.";
    says(bridge(), { text: answer, ungrounded: ["9 kg"], proposedOnly: ["GO_WITH_NOTES"] });
    await mount(panel());
    await ask("and after?");
    const marks = [...container.querySelectorAll<HTMLElement>(".assistant-unverified")];
    expect(marks.map((m) => [m.firstChild!.textContent, m.title, m.querySelector(".kit-sr")!.textContent])).toEqual([
      ["GO with notes", "after the proposed change, not the robot now", " (after the proposed change, not the robot now)"], ["9 kg", "unverified", " (unverified)"]]);
    marks.forEach((m) => m.querySelector(".kit-sr")!.remove());
    expect(container.querySelector('.assistant-turn[data-who="assistant"]')!.textContent).toBe(answer);
  });

  it("findings rows use plain titles: the Check screen's words and two numbers, never a path; the eye asks main to show that finding; the same findings are not listed twice", async () => {
    const api = bridge();
    says(api, { text: "Two faults.", findings: found });
    await mount(panel("Ada (default)", { check }));
    await ask("Why is this NO-GO?");
    expect(text(".assistant-findings strong")).toEqual(["Violation: 5 V rail sags under load", "Violation: Fuse too big for its wire"]);
    expect(text(".assistant-findings small")).toEqual(["It sags to 4.65 V. It needs 4.75 V.", "The fuse is 9 A and the wire is good for 7 A."]);
    expect(container.textContent).not.toContain("topology/");
    expect(text(".assistant-verdict")).toEqual(["NO-GO · 2 violationsfrom the check, just now"]);
    await tap("Show Fuse too big for its wire on the robot");
    expect(api.showAssistantFinding).toHaveBeenCalledWith({ contract: "/r.yaml", finding: FUSE });
    await ask("and the fix?");
    expect(container.querySelectorAll(".assistant-findings").length).toBe(1);
  });

  it("with the Assistant open in Check mode the findings stay in the DOM", async () => {
    await openApp();
    expect(container.textContent).toContain("Verdict");
    await tap("Assistant");
    expect(container.querySelector(`[aria-label="${ASK}"]`)).not.toBeNull();
    expect(container.textContent).toContain("Verdict");
    expect(container.textContent).toContain("PM02 5 V rail overloaded");
  });

  it("selection tags (data path): a question carries the selected part, nothing is sent with nothing selected, and the panel says what the Assistant showed", async () => {
    const api = await openApp();
    api.sendAssistant.mockResolvedValue({ ...turn, text: "It is the wrist.", proposals: [], shown: ["Selected Wrist."] });
    await tap("Assistant");
    await ask("hi");
    expect(api.sendAssistant).toHaveBeenLastCalledWith({ contract: "/r.yaml", revision: "revision", mode: "Check", text: "hi" });
    expect(container.textContent).toContain("It is the wrist.Showed: Selected Wrist.");
    act(() => setSelectedPartId("frame"));
    await ask("and this?");
    expect(api.sendAssistant).toHaveBeenLastCalledWith({ contract: "/r.yaml", revision: "revision", mode: "Check", text: "and this?", context: { selection: { id: "frame", name: "frame" } } });
    act(() => setSelectedPartId(null));
  });

  it("the selection tag can be removed: the cross stops it being sent for that question, and it is back for the next", async () => {
    const api = bridge();
    await mount(panel("Ada (default)", { context: { selection: { id: "joint:wrist", name: "Wrist" }, view: "graph" } }));
    expect(text(".assistant-tag")).toEqual(["Wrist×", "Graph×"]);
    await tap("Do not send Wrist with this question");
    expect(text(".assistant-tag")).toEqual(["Graph×"]);
    await ask("hi");
    expect(api.sendAssistant).toHaveBeenLastCalledWith({ contract: "/r.yaml", revision: "r", mode: "Check", text: "hi", context: { view: "graph" } });
    expect(text(".assistant-tag")).toEqual(["Wrist×", "Graph×"]);
  });
});
