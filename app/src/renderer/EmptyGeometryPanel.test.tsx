import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { EmptyGeometryPanel } from "./EmptyGeometryPanel";

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

describe("EmptyGeometryPanel", () => {
  it("test_the_empty_geometry_panel_names_the_unresolved_prefix", () => {
    const reason = "package://arm_description/meshes/base.stl not resolved -- pass --package-root";
    root = createRoot(container);
    act(() => {
      root.render(<EmptyGeometryPanel reason={reason} />);
    });
    // Rendered verbatim -- this file does not compose its own sentence.
    expect(container.querySelector(".empty-geometry-reason")!.textContent).toBe(reason);
  });

  it("test_the_resolved_mesh_count_comes_from_the_command", () => {
    root = createRoot(container);
    act(() => {
      root.render(<EmptyGeometryPanel reason="unresolved" resolved={{ count: 7, total: 9 }} />);
    });
    expect(container.querySelector(".empty-geometry-resolved")!.textContent).toBe("resolved 7 of 9 meshes");
  });

  it("no resolved count is shown before a resolution attempt has run", () => {
    root = createRoot(container);
    act(() => {
      root.render(<EmptyGeometryPanel reason="unresolved" />);
    });
    expect(container.querySelector(".empty-geometry-resolved")).toBeNull();
  });

  it("offers the package-root control inline", () => {
    root = createRoot(container);
    act(() => {
      root.render(<EmptyGeometryPanel reason="unresolved" />);
    });
    expect(container.querySelector(".package-root-rows")).not.toBeNull();
  });
});
