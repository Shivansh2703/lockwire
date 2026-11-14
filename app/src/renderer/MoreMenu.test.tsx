import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";

import { MoreMenu } from "./MoreMenu";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function openMenu() {
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  act(() => root.render(<><MoreMenu items={[{ id: "Declare", label: "Declare" }]} mode="Check" onPick={() => {}} /><input /></>));
  const more = box.querySelector<HTMLButtonElement>(".shell-more")!;
  more.focus();
  act(() => more.click());
  const close = () => act(() => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
  return { box, more, close, input: box.querySelector("input")! };
}

it("leaves focus on a control outside the menu when it closes", () => {
  const { input, close } = openMenu();
  input.focus();
  close();
  expect(document.activeElement).toBe(input);
});

it("returns focus to the opener when focus was inside the menu", () => {
  const { box, more, close } = openMenu();
  box.querySelector<HTMLElement>('[role="menuitem"]')!.focus();
  close();
  expect(document.activeElement).toBe(more);
});
