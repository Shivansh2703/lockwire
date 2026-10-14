import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DesignControls, type DesignEditReply, type DesignEditRequest } from "./DesignControls";
import type { CheckPayload } from "../shared/api.gen";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
const payload = { robot: "after-edit", scene: { parts: [] } } as unknown as CheckPayload;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

function render(reply: DesignEditReply = { written: true, refusal: null }) {
  const onEdit = vi.fn(async (_request: DesignEditRequest) => reply);
  const onReopen = vi.fn(async () => payload);
  const onPayload = vi.fn();
  act(() => { root.render(<DesignControls contract="robot.yaml" expectedRevision="revision" selectedPartId="dev.gripper" onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />); });
  return { onEdit, onReopen, onPayload };
}

function set(label: string, value: string) {
  const input = container.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;
  const prototype = input instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLSelectElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, value);
  act(() => { input.dispatchEvent(new Event("input", { bubbles: true })); input.dispatchEvent(new Event("change", { bubbles: true })); });
}
const OP: Record<string, string> = { "Mount shape": "Mount" };
function pick(label: string) {
  const group = label === "Place" || label === "Reparent" ? '[aria-label="More operations"]' : '[aria-label="Operation"]';
  if (group.includes("More") && !container.querySelector(group)) pick("More");
  const radio = [...container.querySelectorAll<HTMLLabelElement>(`${group} label`)].find((l) => l.textContent === (OP[label] ?? label))!.querySelector("input")!;
  act(() => radio.click());
}
async function submit() { await act(async () => { container.querySelector<HTMLFormElement>("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); }); }

describe("DesignControls", () => {
  it("distinguishes a saved write from refresh failure and retries only the read", async () => {
    const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
    const onReopen = vi.fn().mockRejectedValueOnce(new Error("sidecar stopped")).mockResolvedValue(payload);
    const onPayload = vi.fn();
    act(() => root.render(<DesignControls contract="robot.yaml" expectedRevision="revision" selectedPartId="dev.gripper" onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />));
    expect(container.querySelector("button[type=submit]")).toBeNull(); // nothing changed: no Apply
    set("Source", "caliper");
    await submit();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Write succeeded, refresh failed");
    expect(container.querySelector<HTMLButtonElement>("button[type=submit]")?.disabled).toBe(true);
    await submit();
    expect(onEdit).toHaveBeenCalledTimes(1);
    await act(async () => [...container.querySelectorAll("button")].find(b => b.textContent === "Retry refresh")!.click());
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onReopen).toHaveBeenCalledTimes(2);
    expect(onPayload).toHaveBeenCalledWith(payload);
    expect(container.querySelector("[role=alert]")).toBeNull();
  });
  it("sends the selected part and real move arguments, then replaces the scene with the reopened payload", async () => {
    const { onEdit, onReopen, onPayload } = render();
    set("X", "10"); set("Y", "20"); set("Z", "30"); set("Source", "caliper"); set("Witness", "Shivansh");
    await submit();
    expect(onEdit).toHaveBeenCalledWith({ command: "move", args: { contract: "robot.yaml", expected_revision: "revision", target: "dev.gripper", offset: [10, 20, 30], unit: "mm", source: "caliper", witness: "Shivansh" } });
    expect(onReopen).toHaveBeenCalledWith("robot.yaml");
    expect(onPayload).toHaveBeenCalledWith(payload);
  });

  it("leaves cycle and orphan decisions to the backend and shows its refusal", async () => {
    const { onEdit, onReopen } = render({ written: false, refusal: "refused: elbow would create a cycle" });
    pick("Reparent"); set("New parent", "elbow"); set("Source", "layout.md"); set("Witness", "Shivansh");
    await submit();
    expect(onEdit).toHaveBeenCalledWith({ command: "reparent", args: { contract: "robot.yaml", expected_revision: "revision", target: "dev.gripper", to: "elbow", source: "layout.md", witness: "Shivansh" } });
    expect(container.querySelector("[role=alert]")!.textContent).toContain("cycle");
    expect((container.querySelector('[aria-label="New parent"]') as HTMLInputElement).value).toBe("elbow");
    expect((container.querySelector('[aria-label="Source"]') as HTMLInputElement).value).toBe("layout.md");
    expect(onReopen).not.toHaveBeenCalled();
  });

  it("shows the backend refusal for an absolute custom mesh path", async () => {
    const { onEdit, onReopen } = render({ written: false, refusal: "custom-mesh mesh_path must be non-absolute" });
    pick("Mount shape"); set("Primitive", "custom-mesh"); set("Mesh path", "/tmp/bracket.stl"); set("Source", "cad"); set("Witness", "Shivansh");
    await submit();
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ command: "mount-shape" }));
    expect(container.querySelector("[role=alert]")!.textContent).toContain("non-absolute");
    expect(onReopen).not.toHaveBeenCalled();
  });

  it("reopens a successful write while showing a post-write readback diagnostic", async () => {
    const { onReopen, onPayload } = render({
      written: true,
      refusal: null,
      diagnostic: "written, but the contract hash could not be read back",
    });
    set("Source", "cad"); set("Witness", "Shivansh");
    await submit();
    expect(onReopen).toHaveBeenCalledWith("robot.yaml");
    expect(onPayload).toHaveBeenCalledWith(payload);
    expect(container.querySelector("[role=status]")?.textContent).toContain("hash could not be read back");
    expect(container.querySelector("[role=alert]")).toBeNull();
  });
});
