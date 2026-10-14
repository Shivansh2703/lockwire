import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";

import { createThickLine, updateThickLineResolution } from "./ThickLine";

describe("thick line", () => {
  it("is drawn with LineMaterial, not a one-pixel line", () => {
    const line = createThickLine({
      points: [0, 0, 0, 1, 0, 0],
      color: 0xff3e45,
      linewidth: 3,
      resolution: new THREE.Vector2(800, 600),
    });
    expect(line).toBeInstanceOf(Line2);
    expect(line.material).toBeInstanceOf(LineMaterial);
    expect((line.material as LineMaterial).linewidth).toBe(3);
  });

  it("updates the resolution uniform on every resize", () => {
    const line = createThickLine({
      points: [0, 0, 0, 1, 0, 0],
      color: 0xffffff,
      linewidth: 2,
      resolution: new THREE.Vector2(800, 600),
    });
    const material = line.material as LineMaterial;
    expect(material.resolution.x).toBe(800);
    expect(material.resolution.y).toBe(600);

    updateThickLineResolution(line, 1024, 768);
    expect(material.resolution.x).toBe(1024);
    expect(material.resolution.y).toBe(768);
  });
});
