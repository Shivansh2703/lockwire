import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";

import { PRODUCT_NAME } from "../shared/branding";
import ways from "../shared/ways.json";
import { WaysFace } from "./WaysFace";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("renders every way and feature, with a copy button per command and Pro greyed", () => {
  const box = document.createElement("div");
  document.body.appendChild(box);
  act(() => createRoot(box).render(<WaysFace />));
  const cards = box.querySelectorAll(".ways-card");
  expect(cards).toHaveLength(ways.ways.length + ways.features.length);
  const commands = ways.ways.flatMap((w) => ("code" in w ? (w.code as string[]) : [])).length + ways.features.length;
  expect(box.querySelectorAll('button[title="Copy"]')).toHaveLength(commands);
  expect(box.querySelector('[data-pro="true"]')?.textContent).toContain(`${PRODUCT_NAME} Pro`);
  expect(box.querySelectorAll('[data-pro="true"] button')).toHaveLength(0);
});
