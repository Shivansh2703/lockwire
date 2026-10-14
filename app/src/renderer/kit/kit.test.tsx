import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as kit from "./index";
import { Panel, PanelFooter, StatusPill, shortCommand, CommandLine, Disclosure, FilePick, Figure, IconButton, PillTabs, Segmented, FindingRow, PartTree } from "./index";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;
let box: HTMLElement;
function mount(el: ReactElement) {
  box = document.createElement("div");
  document.body.appendChild(box);
  root = createRoot(box);
  act(() => root!.render(el));
  return box;
}
afterEach(() => {
  act(() => root?.unmount());
  box?.remove();
});

describe("kit", () => {
  it("PillTabs: roles, selection, arrow/Home/End keys", () => {
    const onChange = vi.fn();
    const tabs = ["a", "b", "c"].map((id) => ({ id, label: id }));
    mount(<PillTabs tabs={tabs} value="b" onChange={onChange} />);
    expect(box.querySelector('[role="tablist"]')).not.toBeNull();
    const t = [...box.querySelectorAll('[role="tab"]')];
    expect(t.map((x) => x.getAttribute("aria-selected"))).toEqual(["false", "true", "false"]);
    const press = (key: string) => act(() => void t[1]!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true })));
    for (const [key, id] of [["ArrowRight", "c"], ["ArrowLeft", "a"], ["Home", "a"], ["End", "c"]]) {
      press(key!);
      expect(onChange).toHaveBeenLastCalledWith(id);
    }
  });

  it("Segmented: radiogroup of real radios, click calls onChange", () => {
    const onChange = vi.fn();
    mount(<Segmented options={[{ value: "x", label: "X" }, { value: "y", label: "Y" }]} value="x" onChange={onChange} />);
    expect(box.querySelector('[role="radiogroup"]')).not.toBeNull();
    const radios = box.querySelectorAll<HTMLInputElement>('input[type="radio"]');
    expect(radios).toHaveLength(2);
    act(() => radios[1]!.click());
    expect(onChange).toHaveBeenCalledWith("y");
  });

  it("IconButton: text is its label; aria-pressed for toggles", () => {
    mount(<IconButton icon="copy" label="Copy" pressed />);
    const b = box.querySelector("button")!;
    expect(b.textContent).toBe("Copy");
    expect(b.getAttribute("aria-pressed")).toBe("true");
  });

  it("Figure: kind word always shown; only catalog/projected are marked", () => {
    for (const [kind, marked] of [["measured", false], ["catalog", true], ["projected", true]] as const) {
      mount(<Figure value={1} kind={kind} />);
      expect(box.textContent).toContain(kind);
      expect(box.querySelector(".is-unverified") !== null).toBe(marked);
      expect(box.textContent?.includes("▲")).toBe(marked);
      act(() => root!.unmount());
      box.remove();
    }
  });

  it("Figure: a float is rounded, an integer stays exact", () => {
    mount(<Figure value={57.02786999999998} unit="kg" kind="derived" />);
    expect(box.querySelector(".kit-num")!.textContent).toBe("57.03 kg");
    act(() => root!.unmount());
    box.remove();
    mount(<Figure value={1200} kind="declared" />);
    expect(box.querySelector(".kit-num")!.textContent).toBe("1200");
  });

  it("Disclosure closed; FilePick remove calls back; CommandLine copy survives no clipboard", async () => {
    mount(<Disclosure summary="s">body</Disclosure>);
    expect(box.querySelector("details")!.open).toBe(false);
    act(() => root!.unmount());
    const onRemove = vi.fn();
    mount(<FilePick label="Pick" path="/x" onPick={() => {}} onRemove={onRemove} />);
    act(() => box.querySelector<HTMLElement>('[title="Remove file"]')!.click());
    expect(onRemove).toHaveBeenCalled();
    act(() => root!.unmount());
    vi.stubGlobal("navigator", {});
    mount(<CommandLine command="lockwire x" />);
    await act(async () => box.querySelector<HTMLElement>("button")!.click());
    vi.unstubAllGlobals();
  });

  it("index exports every kit part", () => {
    expect(Object.keys(kit)).toEqual(expect.arrayContaining([
      "Workspace", "Panel", "PillTabs", "IconButton", "ToolPill", "Button", "Field", "Segmented", "StatusPill",
      "FindingRow", "Figure", "FindingDetail", "Inspector", "PartTree", "Disclosure", "FilePick", "JobBar",
      "Notice", "CommandLine", "EmptyState", "Icon", "ICON_NAMES",
    ]));
  });

  it("CommandLine shortens an absolute path in the middle and keeps the full command in the tip", () => {
    expect(shortCommand("lockwire check /Users/a/b/qarm/lockwire.yaml --x")).toBe("lockwire check …/qarm/lockwire.yaml --x");
    const el = mount(<CommandLine command="lockwire check /Users/a/b/qarm/lockwire.yaml" />);
    expect(el.querySelector(".kit-cli-text")!.textContent).toBe("lockwire check …/qarm/lockwire.yaml");
    expect(el.querySelector(".kit-cli")!.getAttribute("title")).toBe("lockwire check /Users/a/b/qarm/lockwire.yaml");
  });

  it("PillTabs can be disabled (no robot open)", () => {
    const el = mount(<PillTabs tabs={[{ id: "a", label: "A" }]} value="a" onChange={() => {}} disabled />);
    expect(el.querySelector<HTMLButtonElement>('[role="tab"]')!.disabled).toBe(true);
  });

  it("Panel: footer is a sibling of the scroll body (never inside it), also via PanelFooter", () => {
    mount(<Panel title="T" footer={<button>Go</button>}><ul><li>row</li></ul><PanelFooter><button>Deep</button></PanelFooter></Panel>);
    const body = box.querySelector(".kit-panel-body")!;
    const foot = box.querySelector(".kit-panel-footer")!;
    expect(foot.parentElement).toBe(body.parentElement);
    expect(body.contains(foot)).toBe(false);
    expect([...foot.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Go", "Deep"]);
    expect(body.querySelector("button")).toBeNull();
  });

  it("StatusPill always has a word", () => {
    mount(<StatusPill status="bad">{""}</StatusPill>);
    expect(box.textContent).toBe("Problem");
  });

  it("status is never colour alone: a failing row says Fails, an OK tree row has no dot", () => {
    mount(<FindingRow severity="bad" title="Motor" />);
    expect([...box.querySelectorAll(".kit-finding-word")].map((e) => e.textContent)).toEqual(["Fails"]);
    mount(<PartTree parts={[{ id: "a", name: "A", state: "go" }, { id: "b", name: "B", state: "bad" }]} />);
    expect(box.querySelectorAll(".kit-dot")).toHaveLength(1);
  });
});
