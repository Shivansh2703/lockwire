import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { RunPanel } from "./RunPanel";

const FIELDS: Record<string, string> = {
  "SSH host": "bot.local", "SSH user": "root", "SSH private key file": "/k", "SSH known hosts file": "/kh",
  "SSH declared target": "t", "SSH declared image": "img:1",
};

describe("RunPanel SSH Start gate", () => {
  it("stays disabled while any single required field (image, host) is empty", () => {
    window.matchMedia ??= vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }) as never;
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    act(() => root.render(<RunPanel active contract="/c.yaml" joints={[]} onHighlight={() => {}} />));
    act(() => { [...el.querySelectorAll<HTMLElement>(".kit-seg-opt")].find((b) => b.textContent === "SSH")!.click(); });
    const set = (label: string, value: string) => act(() => {
      const input = el.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const start = () => el.querySelector<HTMLButtonElement>(".run-start")!.disabled;
    for (const [label, v] of Object.entries(FIELDS)) set(label, v);
    expect(start()).toBe(false);
    set("SSH declared image", "");
    expect(start()).toBe(true);
    set("SSH declared image", "img:1");
    set("SSH host", "");
    expect(start()).toBe(true);
    act(() => root.unmount());
    el.remove();
  });

  it("shows Host/User/Key file up front, the rest (still required) under More, and drops the hint once a source is picked", () => {
    window.matchMedia ??= vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }) as never;
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    act(() => root.render(<RunPanel active contract="/c.yaml" joints={[]} onHighlight={() => {}} />));
    expect(el.textContent).toContain("Pick a source, then Start.");
    act(() => { [...el.querySelectorAll<HTMLElement>(".kit-seg-opt")].find((b) => b.textContent === "SSH")!.click(); });
    expect(el.textContent).not.toContain("Pick a source, then Start.");
    const direct = [...el.querySelectorAll(".run-controls > .kit-field .kit-field-label")].map((n) => n.textContent);
    expect(direct).toEqual(["Host", "User", "Key file"]);
    const more = [...el.querySelectorAll(".run-controls details .kit-field-label")].map((n) => n.textContent);
    expect(more).toEqual(["Port", "Known hosts", "Target", "Image", "Keep capture"]);
    act(() => root.unmount());
    el.remove();
  });

  it("says what Start needs: Target/Image hint when only Host/User/Key are filled, none when all filled; Docker and Recording too", () => {
    window.matchMedia ??= vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }) as never;
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    act(() => root.render(<RunPanel active contract="/c.yaml" joints={[]} onHighlight={() => {}} />));
    const pick = (name: string) => act(() => { [...el.querySelectorAll<HTMLElement>(".kit-seg-opt")].find((b) => b.textContent === name)!.click(); });
    const set = (label: string, value: string) => act(() => {
      const input = el.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const btn = () => el.querySelector<HTMLButtonElement>(".run-start")!;
    const hint = () => el.querySelector(".run-hint")?.textContent ?? null;
    expect(hint()).toContain("Needs: Recording");
    pick("Docker");
    expect(hint()).toContain("Needs: Container");
    pick("SSH");
    for (const l of ["SSH host", "SSH user", "SSH private key file", "SSH known hosts file"]) set(l, FIELDS[l]!);
    expect(btn().disabled).toBe(true);
    expect(hint()).toContain("Needs: Target, Image");
    expect(btn().title).toBe("Needs: Target, Image");
    expect(el.querySelector("details")!.open).toBe(false);
    act(() => { el.querySelector<HTMLButtonElement>(".run-hint-show")!.click(); });
    expect(el.querySelector("details")!.open).toBe(true);
    set("SSH declared target", "t");
    set("SSH declared image", "img:1");
    expect(btn().disabled).toBe(false);
    expect(hint()).toBeNull();
    act(() => root.unmount());
    el.remove();
  });

  it("keeps another tab looking idle (its own hint, Start waiting) while a job runs elsewhere", async () => {
    window.matchMedia ??= vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }) as never;
    Object.assign(window, { lockwire: { chooseRecording: vi.fn().mockResolvedValue("/r"),
      startRun: vi.fn().mockResolvedValue({ status: "running", health: { verdict: "NOT_SCORED", devices: [], nothing_watched: false, next_step: null } }) } });
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    act(() => root.render(<RunPanel active contract="/c.yaml" joints={[]} onHighlight={() => {}} />));
    expect(el.querySelector(".run-choose-recording")!.textContent).toContain("ROS 2 bag (.mcap or bag folder)");
    await act(async () => el.querySelector<HTMLButtonElement>(".run-choose-recording button")!.click());
    await act(async () => el.querySelector<HTMLButtonElement>(".run-start")!.click());
    act(() => { [...el.querySelectorAll<HTMLElement>(".kit-seg-opt")].find((b) => b.textContent === "Docker")!.click(); });
    expect(el.textContent).toContain("Needs: Container");
    expect(el.querySelector(".kit-jobbar")).toBeNull();
    act(() => root.unmount());
    el.remove();
  });

  it("drops a finished run's verdict when the source changes", async () => {
    window.matchMedia ??= vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }) as never;
    Object.assign(window, { lockwire: { chooseRecording: vi.fn().mockResolvedValue("/r"),
      startRun: vi.fn().mockResolvedValue({ status: "completed", health: { verdict: "GO", devices: [], nothing_watched: false, next_step: null } }) } });
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    act(() => root.render(<RunPanel active contract="/c.yaml" joints={[]} onHighlight={() => {}} />));
    await act(async () => el.querySelector<HTMLButtonElement>(".run-choose-recording button")!.click());
    await act(async () => el.querySelector<HTMLButtonElement>(".run-start")!.click());
    expect(el.textContent).toContain("GO");
    act(() => { [...el.querySelectorAll<HTMLElement>(".kit-seg-opt")].find((b) => b.textContent === "SSH")!.click(); });
    expect(el.textContent).not.toContain("GO");
    expect(el.textContent).toContain("Needs:");
    act(() => root.unmount());
    el.remove();
  });

  it("lights nothing on a tab whose source has not run", async () => {
    window.matchMedia ??= vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }) as never;
    Object.assign(window, { lockwire: { chooseRecording: vi.fn().mockResolvedValue("/r"),
      startRun: vi.fn().mockResolvedValue({ status: "completed", health: { verdict: "NO-GO", nothing_watched: false, next_step: null,
        devices: [{ id: "dev.imu", kind: "device", state: "faulty", word: "faulty", reason: "r", run: null, runs: 1 }] } }) } });
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    const onHighlight = vi.fn();
    act(() => root.render(<RunPanel active contract="/c.yaml" joints={[]} onHighlight={onHighlight} />));
    await act(async () => el.querySelector<HTMLButtonElement>(".run-choose-recording button")!.click());
    await act(async () => el.querySelector<HTMLButtonElement>(".run-start")!.click());
    expect(onHighlight).toHaveBeenLastCalledWith([{ id: "dev.imu", level: "bad" }]);
    act(() => { [...el.querySelectorAll<HTMLElement>(".kit-seg-opt")].find((b) => b.textContent === "SSH")!.click(); });
    expect(onHighlight).toHaveBeenLastCalledWith([]);
    act(() => root.unmount());
    el.remove();
  });
});
