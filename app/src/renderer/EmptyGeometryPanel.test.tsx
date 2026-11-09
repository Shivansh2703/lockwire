import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";

import { EmptyGeometryPanel } from "./EmptyGeometryPanel";

it("is one row: the missing count, one sentence, Choose folder and Skip", () => {
  const box = document.createElement("div");
  const root = createRoot(box);
  act(() => root.render(<EmptyGeometryPanel gap={{ missing: 3, total: 4, example: "package://arm_pkg/meshes/base.stl" }} onChooseFolder={() => {}} onSkip={() => {}} />));
  expect(box.querySelector("strong")!.textContent).toBe("Meshes not found (3)");
  expect([...box.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Choose folder…", "Skip"]);
  act(() => root.unmount());
});
