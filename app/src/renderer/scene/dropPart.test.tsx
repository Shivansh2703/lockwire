/** Drag a listed device onto the body: the drop raycasts and sends the `place` edit; empty space sends nothing. */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("three", async () => {
  const actual = await vi.importActual<typeof import("three")>("three");
  class FakeWebGLRenderer {
    domElement: HTMLCanvasElement;
    constructor(params: { canvas: HTMLCanvasElement }) { this.domElement = params.canvas; }
    setSize() {}
    setPixelRatio() {}
    render() {}
    dispose() {}
  }
  return { ...actual, WebGLRenderer: FakeWebGLRenderer };
});
vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) as never;

const { Canvas } = await import("./Canvas");
const { dropEdit, PART_MIME } = await import("./placeOnBody");

const i16 = (v: number[]) => { const b = Buffer.alloc(v.length * 2); v.forEach((x, i) => b.writeInt16LE(x, i * 2)); return { type: "i16" as const, data: b.toString("base64"), scale: 0.0001, offset: [0, 0, 0] as [number, number, number] }; };
const triangle = { id: "link.arm", positions: i16([-500, -500, 0, 500, -500, 0, 0, 500, 0]), indices: { type: "u16" as const, data: Buffer.from(Uint16Array.of(0, 1, 2).buffer).toString("base64") }, group: { id: "g", label: "g", kind: "link" }, lenses: ["off"] };
const scene = { joints: [{ name: "j", type: "fixed", parent: "base", child: "link.arm", position: [0, 0, 0.1], axis: [], lower: null, upper: null, effort: null }] as never, links: [] };
const prov = { contract: "c.yaml", source: "placed in the app", witness: "me", expected_revision: "r1" };

let root: ReturnType<typeof createRoot>;
afterEach(() => act(() => root.unmount()));

function dropAt(x: number, y: number) {
  const send = vi.fn();
  const div = document.createElement("div");
  document.body.appendChild(div);
  root = createRoot(div);
  act(() => root.render(<Canvas parts={[triangle] as never} sceneKey="k" lens="off" onDropPart={(d, hit) => { const r = dropEdit(d, hit, scene, prov); if (r) send(r); }} />));
  const canvas = div.querySelector("canvas")!;
  canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;
  const event = Object.assign(new Event("drop", { bubbles: true, cancelable: true }), { clientX: x, clientY: y, dataTransfer: { getData: (t: string) => (t === PART_MIME ? "dev.servo5" : ""), types: [PART_MIME] } });
  act(() => { canvas.dispatchEvent(event); });
  return send;
}

it("a drop on the body sends place with mount_frame and the hit point in that frame", () => {
  const send = dropAt(400, 300);
  expect(send).toHaveBeenCalledOnce();
  const { command, args } = send.mock.calls[0]![0];
  expect([command, args.target, args.on, args.unit]).toEqual(["place", "dev.servo5", "link.arm", "m"]);
  expect(args.offset[2]).toBeCloseTo(-0.1, 3); // world z 0 minus the link origin at z 0.1
});

it("a drop on empty space sends nothing", () => {
  expect(dropAt(2, 2)).not.toHaveBeenCalled();
});

it("a drop on a device with a mount_frame places on that frame", () => {
  const hit = { id: "dev.cam", point: [0, 0, 0.3] };
  const links = [{ name: "frame", position: [0, 0, 0.1] }];
  const r = dropEdit("dev.servo5", hit, { joints: [], links, mounts: { "dev.cam": "frame" } }, prov);
  expect(r).toMatchObject({ args: { target: "dev.servo5", on: "frame" } });
  expect((r as { args: { offset: number[] } }).args.offset[2]).toBeCloseTo(0.2, 3);
  expect(dropEdit("dev.servo5", hit, { joints: [], links }, prov)).toBeNull(); // no mount_frame, no frame: nothing to write
});
