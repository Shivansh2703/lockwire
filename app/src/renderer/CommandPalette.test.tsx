import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, test, vi } from "vitest";

import { CommandPalette, matchCommands, type PaletteCommand } from "./CommandPalette";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const cmds = (run = vi.fn()): PaletteCommand[] => [
  { id: "mode-Check", group: "Go to", title: "Check", run },
  { id: "fit", group: "View", title: "Fit the robot in view", run: vi.fn() },
];

test("every typed word must match the group or the title", () => {
  expect(matchCommands(cmds(), "go che").map((c) => c.id)).toEqual(["mode-Check"]);
  expect(matchCommands(cmds(), "zzz")).toEqual([]);
});

test("hidden until Cmd+K; Enter runs the selected command and closes it", () => {
  const run = vi.fn();
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  act(() => root.render(<CommandPalette commands={cmds(run)} />));
  expect(box.querySelector('[role="dialog"]')).toBeNull();
  act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true })); });
  const input = box.querySelector<HTMLInputElement>('[role="combobox"]')!;
  expect(box.querySelectorAll('[role="option"]')).toHaveLength(2);
  act(() => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
  expect(run).toHaveBeenCalledTimes(1);
  expect(box.querySelector('[role="dialog"]')).toBeNull();
  act(() => root.unmount());
  box.remove();
});
