import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CompareWithDatasheet, MeasureCompareDeclare, type MeasureCompareObservation } from "./MeasureCompareDeclare";

let container: HTMLDivElement;
let root: Root;

const OBSERVATION: MeasureCompareObservation = {
  path: "devices/dev.x500/physical/mount_offset",
  value: [0.1, 0.2, 0.3],
  source: "check_scene_placement",
  witness: "Shivansh",
};
const DOC = { contractPath: "/tmp/robot/lockwire.yaml", expectedRevision: "a".repeat(64) };

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const compareButton = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Compare") as HTMLButtonElement;

describe("MeasureCompareDeclare", () => {
  it("sends the proposal to the read-only what-if bridge and shows before and after verdicts", async () => {
    const compareContractWhatIf = vi.fn(async () => ({
      current: { verdict: "NO-GO", scored: 1, findings: [{}] }, proposed: { verdict: "GO", scored: 2, findings: [] }, diff: "capacity changes",
    }));
    window.lockwire = { compareContractWhatIf } as unknown as typeof window.lockwire;
    act(() => root.render(<MeasureCompareDeclare observation={OBSERVATION} {...DOC} />));
    await act(async () => compareButton().click());

    expect(compareContractWhatIf).toHaveBeenCalledWith({
      contract: "/tmp/robot/lockwire.yaml",
      expected_revision: "a".repeat(64),
      change: { path: OBSERVATION.path, value: "0.1,0.2,0.3", source: "check_scene_placement", witness: "Shivansh" },
    });
    expect([...container.querySelectorAll(".kit-pill")].map((p) => p.textContent)).toEqual(["NO-GO", "GO"]);
    expect(container.querySelector(".kit-disclosure")?.textContent).toContain("capacity changes");
  });

  it("will not compare a proposal without source and witness", () => {
    act(() => root.render(<MeasureCompareDeclare observation={{ ...OBSERVATION, source: null }} {...DOC} />));
    expect(compareButton().disabled).toBe(true);
  });

  it("shows a refusal as an error notice", async () => {
    window.lockwire = { compareContractWhatIf: vi.fn().mockRejectedValue(new Error("stale revision")) } as unknown as typeof window.lockwire;
    act(() => root.render(<MeasureCompareDeclare observation={OBSERVATION} {...DOC} />));
    await act(async () => compareButton().click());
    expect(container.querySelector(".kit-notice-error")?.textContent).toBe("stale revision");
  });

  describe("live preview", () => {
    const answer = (verdict: string) => ({ current: { verdict: "GO", scored: 1, findings: [] }, proposed: { verdict, scored: 1, findings: [] }, diff: "" });
    let others: string[] = [];
    const bridge = (verdict = "NO-GO") => {
      const compareContractWhatIf = vi.fn(async () => answer(verdict));
      // Every other bridge call (declare, write) is recorded: the preview must never make one.
      window.lockwire = new Proxy({ compareContractWhatIf }, { get: (t, k) => { if (k in t) return (t as never)[k]; return () => { others.push(String(k)); }; } }) as unknown as typeof window.lockwire;
      return compareContractWhatIf;
    };
    const live = (value: string, onValue?: (v: string) => void) => act(() => root.render(<MeasureCompareDeclare observation={{ ...OBSERVATION, value }} {...DOC} onValue={onValue} />));
    beforeEach(() => { vi.useFakeTimers(); others = []; });
    afterEach(() => vi.useRealTimers());
    const pause = () => act(async () => { await vi.advanceTimersByTimeAsync(700); });

    it("asks once after typing pauses and says it is a preview", async () => {
      const call = bridge();
      live("1.5");
      for (const v of ["1.5", "1.52", "1.52 kg", "9", "98"]) { live(v); await act(async () => { await vi.advanceTimersByTimeAsync(200); }); }
      expect(call).not.toHaveBeenCalled();
      await pause();
      expect(call).toHaveBeenCalledTimes(1);
      expect(call).toHaveBeenCalledWith(expect.objectContaining({ change: expect.objectContaining({ value: "98" }) }));
      expect(container.querySelector(".measure-preview-note")?.textContent).toBe("Preview, not saved");
      expect([...container.querySelectorAll(".kit-pill")].map((p) => p.textContent)).toEqual(["GO", "NO-GO"]);
    });

    it("previews before a source or checker is typed, with a stand-in", async () => {
      const call = bridge();
      act(() => root.render(<MeasureCompareDeclare observation={{ ...OBSERVATION, value: "1", source: null, witness: null }} {...DOC} />));
      act(() => root.render(<MeasureCompareDeclare observation={{ ...OBSERVATION, value: "2", source: null, witness: null }} {...DOC} />));
      await pause();
      expect(call).toHaveBeenCalledWith(expect.objectContaining({ change: expect.objectContaining({ source: "preview", witness: "preview" }) }));
    });

    it("keeps the newest answer when an older one arrives late", async () => {
      const resolvers: Array<(v: unknown) => void> = [];
      const compareContractWhatIf = vi.fn(() => new Promise((r) => { resolvers.push(r); }));
      window.lockwire = { compareContractWhatIf } as unknown as typeof window.lockwire;
      live("1.5");
      live("2");
      await pause();
      live("3");
      await pause();
      expect(resolvers).toHaveLength(2);
      await act(async () => resolvers[1]!(answer("NO-GO")));
      await act(async () => resolvers[0]!(answer("GO")));
      expect([...container.querySelectorAll(".kit-pill")].map((p) => p.textContent)).toEqual(["GO", "NO-GO"]);
    });

    it("does not ask for the saved value itself", async () => {
      const call = bridge();
      live("1.5");
      await pause();
      expect(call).not.toHaveBeenCalled();
    });

    it("drags a slider through the same pause and only ever calls the preview route", async () => {
      const call = bridge();
      const onValue = vi.fn();
      live("2", onValue);
      const slider = container.querySelector("input[type=range]") as HTMLInputElement;
      expect(slider.max).toBe("4");
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      act(() => { set.call(slider, "3"); slider.dispatchEvent(new Event("input", { bubbles: true })); });
      expect(onValue).toHaveBeenCalledWith("3");
      live("3");
      await pause();
      expect(call).toHaveBeenCalledTimes(1);
      expect(others).toEqual([]);
    });

    it("the slider has a visible label", () => {
      bridge();
      live("2", vi.fn());
      expect(container.querySelector("label.measure-slider-label")?.textContent).toContain("Slide to see where the verdict flips");
    });

    it("has no slider for a vector or without a way to set the value", () => {
      bridge();
      live("0.1,0.2,0.3", vi.fn());
      expect(container.querySelector("input[type=range]")).toBeNull();
      live("2");
      expect(container.querySelector("input[type=range]")).toBeNull();
    });
  });

  it("a datasheet weight typed with a decimal comma enables Compare and sends a point", async () => {
    const onCompare = vi.fn().mockResolvedValue(null);
    act(() => root.render(<CompareWithDatasheet result={null} onCompare={onCompare} />));
    const input = container.querySelector("input[aria-label='datasheet weight']") as HTMLInputElement;
    act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "1,5"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    const btn = [...container.querySelectorAll("button")].find((b) => b.textContent === "Compare")!;
    expect(btn.disabled).toBe(false);
    await act(async () => { btn.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(onCompare).toHaveBeenCalledWith("1.5", "datasheet");
  });
});
