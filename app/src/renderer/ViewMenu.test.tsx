import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { ViewMenu } from "./ViewMenu";

const saved = vi.fn().mockResolvedValue(undefined);
vi.mock("./scene/savePicture", () => ({ savePicture: (n: string) => saved(n) }));

describe("View menu: Save picture", () => {
  it("saves the picture under the robot's name and closes", () => {
    const box = document.createElement("div");
    document.body.append(box);
    const root = createRoot(box);
    act(() => root.render(<ViewMenu design={false} name="Arm" />));
    act(() => box.querySelector<HTMLButtonElement>("button[aria-haspopup]")!.click());
    const row = [...box.querySelectorAll<HTMLButtonElement>("[role=menuitem]")].find((b) => b.textContent === "Save picture")!;
    act(() => row.click());
    expect(saved).toHaveBeenCalledWith("Arm");
    expect(box.querySelector("[role=menu]")).toBeNull();
    act(() => root.unmount());
    box.remove();
  });
});
