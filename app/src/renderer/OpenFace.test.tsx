import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OpenFace } from "./OpenFace";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function renderFace(props: Parameters<typeof OpenFace>[0] = {}) {
  root = createRoot(container);
  act(() => {
    root.render(<OpenFace {...props} />);
  });
}

describe("OpenFace", () => {
  it("test_the_recent_list_is_empty_and_says_so_on_first_launch", () => {
    renderFace({ recent: [] });
    expect(container.textContent).toContain("No recent robots");
  });

  it("a non-empty recent list renders real rows, not the empty message", () => {
    renderFace({ recent: [{ path: "/robots/arm/lockwire.yaml", name: "arm" }] });
    expect(container.querySelectorAll(".open-face-recents li").length).toBe(1);
    expect(container.querySelector(".open-face-recents li")!.textContent).toBe("arm");
  });

  it("repeated robot names show their folder so rows differ", () => {
    renderFace({ recent: [{ path: "/a/qarm1/lockwire.yaml", name: "QARM" }, { path: "/a/qarm2/lockwire.yaml", name: "QARM" }, { path: "/a/x/l.yaml", name: "arm" }] });
    expect([...container.querySelectorAll(".open-face-recents li")].map((l) => l.textContent)).toEqual(["QARMqarm1", "QARMqarm2", "arm"]);
  });

  it("test_the_rest_of_the_app_still_works_with_no_geometry", () => {
    renderFace({ emptyGeometry: { reason: "package:// not resolved" } });
    // The actions and recents still render alongside the empty-geometry panel.
    expect(container.querySelector(".open-face-actions")).not.toBeNull();
    expect(container.querySelector(".empty-geometry-panel")).not.toBeNull();
  });

  it("one Open robot button calls its handler", () => {
    const onOpenRobot = vi.fn();
    renderFace({ onOpenRobot });
    const buttons = Array.from(container.querySelectorAll(".open-face-actions button"));
    expect(buttons.map((b) => b.textContent)).toEqual(["Open robot"]);
    act(() => buttons[0]!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onOpenRobot).toHaveBeenCalledTimes(1);
  });
});
