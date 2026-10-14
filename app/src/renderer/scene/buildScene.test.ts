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

const { buildScene, disposeScene, handleResize, updatePixelRatio } = await import("./buildScene");

const here = path.dirname(fileURLToPath(import.meta.url));
const fixturePath = path.resolve(here, "../../../../tests/fixtures/scene_roundtrip.json");
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));

function makeParts() {
  return [
    { positions: fixture.positions.packed, normals: fixture.normals.packed, indices: fixture.indices_u16.packed },
    { positions: fixture.positions.packed, indices: fixture.indices_u32.packed },
  ];
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
