import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import type { SwapPreviewPayload } from "../shared/api.gen";
import { SwapPanel } from "./SwapPanel";
import FIXTURE from "./swap.fixture.json";

it("groups fit / tight / too weak, warns only on a status drop, and Use this writes with the revision", async () => {
  const swapPreview = vi.fn().mockResolvedValue(FIXTURE as SwapPreviewPayload);
  const writeContract = vi.fn().mockResolvedValue({ refusal: null, payload: { lines: ["swapped wrist to feetech.sts3250"] } });
  Object.assign(window, { lockwire: { swapPreview, writeContract } });
  const onApplied = vi.fn();
  const el = document.body.appendChild(document.createElement("div"));
  const root = createRoot(el);
  await act(async () => root.render(<SwapPanel contract="/r.yaml" revision="rev1" joint="wrist" onClose={() => {}} onApplied={onApplied} />));
  expect([...el.querySelectorAll(".swap-group h3")].map((h) => h.textContent)).toEqual(["Fit", "Tight", "Too weak"]);
  expect(el.querySelector(".swap-row")!.textContent).toBe("Feetech STS3250cited4.90 N·m stall · 32.7x · 74.5 gdifferent bus");
  expect(el.querySelector(".swap-note")!.textContent).toBe("supply 5 V, as declared for its power rail");
  expect(el.textContent).toContain("needs a different cable or adapter");
  expect([...el.querySelectorAll('.swap-knock[aria-label="Joints above"] li')].map((l) => [l.textContent, l.className])).toEqual([
    ["+61.3 g on the wrist link", ""],
    ["elbow 0.40 N·m → 0.52 N·m, still fit (3.7x)", ""],
    ["warning: shoulder 0.81 N·m → 1.05 N·m, now tight (1.8x), was fit", "swap-drop"], // warning only where the status dropped
  ]);
  await act(async () => [...el.querySelectorAll("button")].find((b) => b.textContent === "Use this")!.click());
  expect(writeContract).toHaveBeenCalledWith({ command: "swap-apply", args: { contract: "/r.yaml", joint: "wrist", part_id: "feetech.sts3250", expected_revision: "rev1" } });
  expect(onApplied).toHaveBeenCalledWith(["swapped wrist to feetech.sts3250"]);

  swapPreview.mockResolvedValue({ ...FIXTURE, candidates: [], chosen: null, knock_on: [] });
  await act(async () => root.render(<SwapPanel contract="/r.yaml" revision="rev1" joint="elbow" onClose={() => {}} onApplied={onApplied} />));
  expect(el.textContent).toContain("No alternatives with a source yet");
  act(() => root.unmount());
  el.remove();
});

it("shows a loading line, warns on a too-weak pick, and clears an old error when applying", async () => {
  Object.assign(window, { lockwire: { swapPreview: vi.fn().mockResolvedValue({ ...FIXTURE, chosen: "test.weak" }), writeContract: vi.fn().mockRejectedValueOnce(new Error("boom")).mockResolvedValue({ refusal: null }) } });
  const el = document.body.appendChild(document.createElement("div"));
  act(() => createRoot(el).render(<SwapPanel contract="/r.yaml" revision="rev1" joint="elbow" onClose={() => {}} onApplied={() => {}} />));
  expect(el.textContent).toContain("Ranking servos");
  await act(async () => {});
  expect(el.textContent).toContain("Warning: elbow would be too weak");
  for (const boom of [true, false]) {
    await act(async () => [...el.querySelectorAll("button")].find((b) => b.textContent === "Use this")!.click());
    expect(el.textContent!.includes("boom")).toBe(boom); // the second apply clears the first error
  }
});
