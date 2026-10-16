import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

// jsdom has no WebGL context, so the renderer alone is stubbed — everything else (Scene,
// Mesh, BufferGeometry, PerspectiveCamera, OrbitControls) is real three and needs no GL to
// construct. This is the "STUBBED renderer" the accept line asks for: it lets the scene-graph
// assertions below run without a real GPU.
// The class lives inside the factory (not top-level) because vi.mock is hoisted above imports.
vi.mock("three", async () => {
  const actual = await vi.importActual<typeof import("three")>("three");
  class FakeWebGLRenderer {
    domElement: HTMLCanvasElement;
    private width = 0;
    private height = 0;
    private ratio = 1;
    constructor(params: { canvas: HTMLCanvasElement }) {
      this.domElement = params.canvas;
    }
    setSize(width: number, height: number) {
      this.width = width;
      this.height = height;
    }
    setPixelRatio(ratio: number) {
      this.ratio = ratio;
    }
    getPixelRatio() {
      return this.ratio;
    }
    getSize(target: THREE.Vector2) {
      return target.set(this.width, this.height);
    }
    render() {}
    dispose() {}
  }
  return { ...actual, WebGLRenderer: FakeWebGLRenderer };
});

const { FRAME_INSET, applyViewInset, refit, buildScene, disposeScene, handleResize, updatePixelRatio, attachWasd } = await import("./buildScene");

const here = path.dirname(fileURLToPath(import.meta.url));
const fixturePath = path.resolve(here, "../../../../tests/fixtures/scene_roundtrip.json");
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));

function makeParts() {
  return [
    { positions: fixture.positions.packed, normals: fixture.normals.packed, indices: fixture.indices_u16.packed },
    { positions: fixture.positions.packed, indices: fixture.indices_u32.packed },
  ];
}

function projectedX(ctx: { meshes: THREE.Mesh[]; camera: THREE.PerspectiveCamera }, width: number): [number, number] {
  const box = new THREE.Box3().setFromObject(ctx.meshes[0]!);
  const xs = [box.min.x, box.max.x].flatMap((x) => [box.min.y, box.max.y].flatMap((y) => [box.min.z, box.max.z].map((z) => (new THREE.Vector3(x, y, z).project(ctx.camera).x + 1) * (width / 2))));
  return [Math.min(...xs), Math.max(...xs)];
}

describe("buildScene", () => {
  let canvas: HTMLCanvasElement;

  beforeEach(() => {
    canvas = document.createElement("canvas");
  });

  it("builds exactly one mesh per decoded part", () => {
    const parts = makeParts();
    const ctx = buildScene(canvas, parts, 800, 600);
    expect(ctx.meshes.length).toBe(parts.length);
    expect(ctx.scene.children.filter((c) => c instanceof THREE.Mesh).length).toBe(parts.length);
  });

  it("gives each mesh a position attribute the same length as the decoded array, and honours per-part normals", () => {
    const parts = makeParts();
    const ctx = buildScene(canvas, parts, 800, 600);

    const expectedLength = fixture.positions.raw.length;
    for (const mesh of ctx.meshes) {
      expect(mesh.geometry.getAttribute("position").array.length).toBe(expectedLength);
    }
    // part 0 declared normals -> decoded, not computed
    expect(ctx.meshes[0]!.geometry.getAttribute("normal").array.length).toBe(fixture.normals.raw.length);
    // part 1 declared none -> computeVertexNormals still leaves one normal per vertex
    expect(ctx.meshes[1]!.geometry.getAttribute("normal").array.length).toBe(expectedLength);
  });

  it("an empty scene (Open) still has a Z-up camera, so its floor lies flat", () => {
    const ctx = buildScene(canvas, [], 800, 600);
    expect(ctx.camera.up.toArray()).toEqual([0, 0, 1]);
  });

  it("constructs a PerspectiveCamera and OrbitControls", () => {
    const ctx = buildScene(canvas, makeParts(), 800, 600);
    expect(ctx.camera).toBeInstanceOf(THREE.PerspectiveCamera);
    expect(ctx.camera.aspect).toBeCloseTo(800 / 600);
    expect(ctx.controls).toBeInstanceOf(OrbitControls);
    ctx.controls.dispose();
  });

  it("never applies a part's translation to its decoded geometry", () => {
    const parts = [
      { positions: fixture.positions.packed, indices: fixture.indices_u16.packed, translation: fixture.translation },
    ];
    const ctx = buildScene(canvas, parts as never, 800, 600);
    const decoded = Array.from(ctx.meshes[0]!.geometry.getAttribute("position").array as Float32Array);
    const translated = fixture.positions.raw.map((v: number, i: number) => v + fixture.translation[i % 3]);
    let anyClose = false;
    for (let i = 0; i < decoded.length; i++) {
      if (Math.abs(decoded[i]! - translated[i]!) < 1) anyClose = true;
    }
    expect(anyClose).toBe(false);
  });

  it("fits the model at its own scale and leaves the fitted orbit pose alone on resize", () => {
    const small = buildScene(canvas, makeParts(), 800, 600);
    const enlarged = buildScene(
      canvas,
      makeParts().map((part) => ({
        ...part,
        positions: {
          ...part.positions,
          scale: part.positions.scale * 10,
          offset: part.positions.offset.map((value: number) => value * 10),
        },
      })),
      800,
      600,
    );
    expect(enlarged.camera.position.distanceTo(enlarged.controls.target)).toBeCloseTo(
      small.camera.position.distanceTo(small.controls.target) * 10,
    );

    const position = enlarged.camera.position.clone();
    const target = enlarged.controls.target.clone();
    handleResize(enlarged, 1024, 768);
    expect(enlarged.camera.position).toEqual(position);
    expect(enlarged.controls.target).toEqual(target);
  });

  it.each([[1, 1, 0.2], [1, 1, 0.05], [0.8, 0.8, 0.8]] as [number, number, number][])("frames a %j box about 50-55%% of the open width, centred, clear of the edges", (...size) => {
    const ctx = buildScene(canvas, makeParts(), 1440, 900, [], FRAME_INSET);
    ctx.meshes = [new THREE.Mesh(new THREE.BoxGeometry(...size))];
    ctx.meshes[0]!.updateMatrixWorld();
    applyViewInset(ctx, FRAME_INSET, 1440, 900);
    refit(ctx);
    ctx.camera.updateMatrixWorld();
    const [lo, hi] = projectedX(ctx, 1440);
    const open = [FRAME_INSET.left, 1440 - FRAME_INSET.right];
    expect((lo + hi) / 2).toBeCloseTo((open[0]! + open[1]!) / 2, -1);
    const fraction = (hi - lo) / (open[1]! - open[0]!);
    expect(fraction).toBeGreaterThanOrEqual(0.5);
    expect(fraction).toBeLessThanOrEqual(0.555);
    expect(lo).toBeGreaterThan(open[0]! + 80);
    expect(hi).toBeLessThan(open[1]! - 80);
  });

  it("a tall robot stops at 80% of the open height, narrower than the width rule", () => {
    const ctx = buildScene(canvas, makeParts(), 1440, 900, [], FRAME_INSET);
    ctx.meshes = [new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.3, 1.7))];
    ctx.meshes[0]!.updateMatrixWorld();
    applyViewInset(ctx, FRAME_INSET, 1440, 900);
    refit(ctx);
    ctx.camera.updateMatrixWorld();
    const [lo, hi] = projectedX(ctx, 1440);
    expect((hi - lo) / (1440 - FRAME_INSET.left - FRAME_INSET.right)).toBeLessThan(0.5);
    const box = new THREE.Box3().setFromObject(ctx.meshes[0]!);
    const ys = [box.min.x, box.max.x].flatMap((x) => [box.min.y, box.max.y].flatMap((y) => [box.min.z, box.max.z].map((z) => (1 - new THREE.Vector3(x, y, z).project(ctx.camera).y) * 450)));
    expect((Math.max(...ys) - Math.min(...ys)) / (900 - FRAME_INSET.top - FRAME_INSET.bottom)).toBeLessThanOrEqual(0.85);
  });

  it("with no right panel the robot centres in [left panel + gap, width - 24] at 55% of that", () => {
    const inset = { top: 105, right: 24, bottom: 60, left: 271 };
    const ctx = buildScene(canvas, makeParts(), 1440, 900, [], inset);
    ctx.meshes = [new THREE.Mesh(new THREE.BoxGeometry(1, 1, 0.05))];
    ctx.meshes[0]!.updateMatrixWorld();
    applyViewInset(ctx, inset, 1440, 900);
    refit(ctx);
    ctx.camera.updateMatrixWorld();
    const [lo, hi] = projectedX(ctx, 1440);
    expect((lo + hi) / 2).toBeCloseTo((271 + 1416) / 2, -1);
    expect((hi - lo) / (1416 - 271)).toBeGreaterThanOrEqual(0.5);
    expect((hi - lo) / (1416 - 271)).toBeLessThanOrEqual(0.555);
  });

  it("at 1100x750 with a 560px right panel the robot stays inside the open area", () => {
    const inset = { ...FRAME_INSET, right: Math.max(FRAME_INSET.right, 560 + 12) }; // what Shell measures
    const ctx = buildScene(canvas, makeParts(), 1100, 750, [], inset);
    ctx.meshes = [new THREE.Mesh(new THREE.BoxGeometry(1, 1, 0.2))];
    ctx.meshes[0]!.updateMatrixWorld();
    applyViewInset(ctx, inset, 1100, 750);
    refit(ctx);
    ctx.camera.updateMatrixWorld();
    const box = new THREE.Box3().setFromObject(ctx.meshes[0]!);
    const xs = [box.min.x, box.max.x].flatMap((x) => [box.min.y, box.max.y].flatMap((y) => [box.min.z, box.max.z].map((z) => (new THREE.Vector3(x, y, z).project(ctx.camera).x + 1) * 550)));
    expect(Math.max(...xs)).toBeLessThan(1100 - 572);
    expect(Math.min(...xs)).toBeGreaterThan(inset.left);
  });

  it("handleResize owns the resolution uniform, the camera aspect, and the renderer size", () => {
    const ctx = buildScene(canvas, makeParts(), 800, 600);
    handleResize(ctx, 1024, 768);
    expect(ctx.resolution.x).toBe(1024);
    expect(ctx.resolution.y).toBe(768);
    expect(ctx.camera.aspect).toBeCloseTo(1024 / 768);
  });

  it("caps the renderer's pixel ratio at mount, and re-applies the cap on a later ratio change", () => {
    const original = window.devicePixelRatio;
    try {
      Object.defineProperty(window, "devicePixelRatio", { value: 4, configurable: true });
      const ctx = buildScene(canvas, makeParts(), 800, 600);
      // devicePixelRatio was 4 (e.g. a 4x display) — capped at 2, never left at 1x nor uncapped.
      expect((ctx.renderer as unknown as { getPixelRatio(): number }).getPixelRatio()).toBe(2);

      updatePixelRatio(ctx, 3);
      expect((ctx.renderer as unknown as { getPixelRatio(): number }).getPixelRatio()).toBe(2);
      // CSS size is untouched by a ratio change — only the drawing-buffer target moves.
      expect(ctx.renderer.getSize(new THREE.Vector2())).toEqual(new THREE.Vector2(800, 600));
    } finally {
      Object.defineProperty(window, "devicePixelRatio", { value: original, configurable: true });
    }
  });

  it("disposeScene disposes controls, every mesh's geometry/material, and the renderer without throwing", () => {
    const ctx = buildScene(canvas, makeParts(), 800, 600);
    expect(() => disposeScene(ctx)).not.toThrow();
  });
});

describe("WASD walk", () => {
  const press = (key: string, type = "keydown") => window.dispatchEvent(new KeyboardEvent(type, { key }));
  function setup() {
    const ctx = buildScene(document.createElement("canvas"), makeParts(), 400, 300);
    return { ctx, wasd: attachWasd(ctx) };
  }
  it("W moves camera and target together and asks for a frame", () => {
    const { ctx, wasd } = setup();
    const cam = ctx.camera.position.clone(), tgt = ctx.controls.target.clone();
    ctx.needsRender = false;
    press("w");
    wasd.step(0.1);
    press("w", "keyup");
    expect(ctx.camera.position.equals(cam)).toBe(false);
    expect(ctx.controls.target.equals(tgt)).toBe(false);
    expect(ctx.camera.position.z).toBeCloseTo(cam.z); // ground plane only
    expect(ctx.needsRender).toBe(true);
    wasd.dispose();
  });
  it("W while an input is focused changes nothing", () => {
    const { ctx, wasd } = setup();
    const input = document.body.appendChild(document.createElement("input"));
    input.focus();
    const cam = ctx.camera.position.clone();
    press("w");
    wasd.step(0.1);
    expect(ctx.camera.position.equals(cam)).toBe(true);
    input.remove();
    wasd.dispose();
  });
});
