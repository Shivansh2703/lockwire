import { afterEach, describe, expect, it, vi } from "vitest";

import { canvasPng, savePicture } from "./savePicture";

const SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const b64 = (bytes: number[]) => btoa(String.fromCharCode(...bytes));
const u32 = (n: number) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
/** A stand-in for a WebGL canvas: PNG bytes with its own width/height in the header, JPEG or nothing for any other type. */
function fakeCanvas(width: number, height: number) {
  const png = [...SIG, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52, ...u32(width), ...u32(height)];
  return { width, height, toDataURL: (type: string) => (type === "image/png" ? `data:image/png;base64,${b64(png)}` : "data:,") } as unknown as HTMLCanvasElement;
}
afterEach(() => { document.body.innerHTML = ""; vi.restoreAllMocks(); });

describe("Save picture", () => {
  it("hands over a real PNG whose size is the canvas size", () => {
    const bytes = Uint8Array.from(atob(canvasPng(fakeCanvas(1234, 567))), (c) => c.charCodeAt(0));
    const dv = new DataView(bytes.buffer);
    expect([...bytes.slice(0, 8)]).toEqual(SIG);
    expect([dv.getUint32(16), dv.getUint32(20)]).toEqual([1234, 567]);
  });

  it("sends the scene canvas's picture under the robot's name", async () => {
    const send = vi.fn().mockResolvedValue(true);
    (window as unknown as { lockwire: object }).lockwire = { savePicture: send };
    const scene = document.createElement("div");
    scene.className = "shell-scene";
    document.body.append(scene);
    const c = document.createElement("canvas");
    vi.spyOn(c, "toDataURL").mockReturnValue("data:image/png;base64,QUJD");
    scene.append(c);
    await savePicture("Arm");
    expect(send).toHaveBeenCalledWith("Arm", "QUJD");
  });
});
