import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PackageRootRows } from "./PackageRootRows";

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

describe("PackageRootRows", () => {
  it("test_the_package_root_control_adds_a_row_per_click", () => {
    root = createRoot(container);
    act(() => {
      root.render(<PackageRootRows />);
    });
    expect(container.querySelectorAll('input[aria-label="package root"]').length).toBe(1);

    const addButton = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Add package root",
    )!;
    act(() => addButton.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(container.querySelectorAll('input[aria-label="package root"]').length).toBe(2);

    act(() => addButton.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(container.querySelectorAll('input[aria-label="package root"]').length).toBe(3);
  });

  it("test_onChange_reports_only_non_empty_roots", () => {
    const onChange = vi.fn();
    root = createRoot(container);
    act(() => {
      root.render(<PackageRootRows onChange={onChange} />);
    });
    const input = container.querySelector('input[aria-label="package root"]') as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      setter.call(input, "/pkgs/arm_description");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(onChange).toHaveBeenLastCalledWith(["/pkgs/arm_description"]);
  });
});
