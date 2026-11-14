import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PagePayload } from "../shared/api.gen";
import { FindPart, findItems } from "./FindPart";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const page = {
  links: [{ name: "base_link" }, { name: "upper_arm" }],
  joints: [{ name: "shoulder_pan" }, { name: "upper_arm_roll" }],
  wiring_devices: [{ id: "dev.imu", name: "Gyro board", connectors: [] }],
  wiring_wires: [{ id: "w.batt", name: "Battery lead" }],
} as unknown as PagePayload;

let box: HTMLDivElement, root: Root, pick: ReturnType<typeof vi.fn<(id: string) => void>>, outside: HTMLButtonElement;
const key = (k: string, init: KeyboardEventInit = {}, target: EventTarget = window) => act(() => { target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init })); });
const box$ = () => document.querySelector<HTMLInputElement>(".find-part input");
const type = (v: string) => act(() => {
  const el = box$()!;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, v);
  el.dispatchEvent(new Event("input", { bubbles: true }));
});
const open = () => key("f", { ctrlKey: true });

beforeEach(() => {
  box = document.createElement("div"); document.body.append(box);
  outside = document.createElement("button"); document.body.append(outside); outside.focus();
  pick = vi.fn<(id: string) => void>(); root = createRoot(box);
  act(() => root.render(<FindPart page={page} onPick={pick} />));
});
afterEach(() => { act(() => root.unmount()); box.remove(); outside.remove(); });

describe("FindPart", () => {
  it("is closed until Cmd/Ctrl+F, then opens focused", () => {
    expect(box$()).toBeNull();
    open();
    expect(document.activeElement).toBe(box$());
  });

  it("a plain F does not open it", () => {
    key("f");
    expect(box$()).toBeNull();
  });

  it("typing a joint name and Enter picks that joint and closes", () => {
    open(); type("shoulder");
    key("Enter", {}, box$()!);
    expect(pick).toHaveBeenCalledWith("shoulder_pan");
    expect(box$()).toBeNull();
  });

  it("arrow keys move between matches", () => {
    open(); type("upper_arm");
    key("ArrowDown", {}, box$()!); key("Enter", {}, box$()!);
    expect(pick).toHaveBeenCalledWith("upper_arm_roll");
  });

  it("Escape closes without picking and returns focus", () => {
    open(); type("base");
    key("Escape", {}, box$()!);
    expect([box$(), pick.mock.calls.length, document.activeElement]).toEqual([null, 0, outside]);
  });

  it("no match says so in plain words", () => {
    open(); type("zzz");
    expect(document.querySelector(".find-part-none")!.textContent).toBe("Nothing named zzz");
  });

  it("devices and wires are searchable by name", () => {
    open(); type("gyro");
    key("Enter", {}, box$()!);
    open(); type("battery");
    key("Enter", {}, box$()!);
    expect(pick.mock.calls.map((c) => c[0])).toEqual(["dev.imu", "w.batt"]);
  });

  it("findItems lists links, joints, devices and wires", () => {
    expect(findItems(page).map((i) => i.kind)).toEqual(["link", "link", "joint", "joint", "device", "wire"]);
  });
});
