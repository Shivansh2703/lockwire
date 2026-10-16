import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { CheckPayload } from "../shared/api.gen";
import ANYMAL from "./anymal.click.fixture.json";
import SCENE_DOC from "./scene/fixtures/x500-check-payload.json";
import { getDesignView, resetDesignView, setDesignView } from "./scene/designView";
import { getSelectedGroup, setSelectedPartId } from "./scene/selectionStore";

vi.mock("./proRegistry", () => ({ pro: null }));
vi.mock("./scene/Canvas", () => ({ Canvas: ({ children }: { children?: ReactNode }) => <>{children}</>, useScene: () => ({}) }));
const { DesignParts, DesignLens } = await import("./DesignPanel");

let container: HTMLDivElement;
let root: Root;
beforeEach(() => { resetDesignView(); setSelectedPartId(null); container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); });

const show = (payload: unknown) => act(() => root.render(<><DesignParts payload={payload as CheckPayload} /><DesignLens payload={payload as CheckPayload} /></>));
const row = (id: string) => container.querySelector<HTMLElement>(`[data-conn-id="${id}"]`)!;

it("X500 Power: the tree replaces Parts, the overloaded rail reads red, hover lights and click selects the rail's loads", () => {
  show(SCENE_DOC);
  act(() => [...container.querySelectorAll<HTMLElement>('[role="tab"]')].find((t) => t.textContent === "Power")!.click());
  expect(container.textContent).toContain("Connections");
  expect(row("rail.pm02-5v").querySelector("small")!.className).toBe("kit-s-bad");
  expect(row("rail.pm02-5v").textContent).toContain("35.2 / 15.6 W");
  act(() => row("dev.companion.jetson-orin-nano").dispatchEvent(new MouseEvent("mouseover", { bubbles: true })));
  expect(getDesignView().hover).toEqual(["dev.companion.jetson-orin-nano"]);
  act(() => row("rail.pm02-5v").click());
  expect(getSelectedGroup()!.members).toEqual(expect.arrayContaining(["rail.pm02-5v", "dev.companion.jetson-orin-nano", "dev.camera.oak-d-lite"]));
});

it("Health is a greyed Pro label, not a tab, in the free app", () => {
  show(SCENE_DOC);
  expect([...container.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).not.toContain("Health");
  expect(container.querySelector(".shell-pro-tab")!.textContent).toBe("HealthPro");
});

it("ANYmal Power: one empty-state line, not a broken panel", () => {
  show(ANYMAL.page);
  act(() => setDesignView({ lens: "power" }));
  expect(container.querySelector(".conn-empty")!.textContent).toBe("No power rails declared for this robot.");
});
