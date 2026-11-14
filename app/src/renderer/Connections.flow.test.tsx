import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import * as THREE from "three";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { CheckPayload } from "../shared/api.gen";
import SCENE_DOC from "./scene/fixtures/x500-check-payload.json";
import { swept } from "./scene/sweptFixture";

const scene = new THREE.Scene();
const meshes = Array.from({ length: 12 }, (_, i) => {
  const m = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));
  m.position.set(i * 3, 0, 0);
  m.updateMatrixWorld();
  return m;
});
const ctx = { scene, meshes, camera: null, needsRender: false };
vi.mock("./scene/Canvas", () => ({ useScene: () => ctx }));
const { ConnectionLines, ConnectionsPanel } = await import("./Connections");

const ids = SCENE_DOC.graph.power.nodes.map((n) => n.id);
ids.forEach((id, i) => { meshes[i]!.userData.partId = id; });
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
// The PM02 rail is overloaded in this fixture (red); lift its limit so its wires show the plain accent.
const power = (volts: number) => ({ ...swept(volts), nodes: swept(volts).nodes.map((n) => (n.id === "rail.pm02-5v" ? { ...n, limit: 100 } : n)) });
const payload = (volts = 9) => ({ ...SCENE_DOC, graph: { ...SCENE_DOC.graph, power: power(volts) } }) as unknown as CheckPayload;

const TOKENS = { "canvas-a": "#101010", go: "#00ff00", "glow-warn": "#ffff00", "glow-bad": "#ff0000", accent: "#0000ff", grid: "#333", part: "#888", outline: "#fff" };
let container: HTMLDivElement;
let root: Root;
let frames: FrameRequestCallback[];
beforeEach(() => {
  for (const [k, v] of Object.entries(TOKENS)) document.documentElement.style.setProperty(`--${k}`, v);
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => frames.push(cb));
  vi.stubGlobal("cancelAnimationFrame", () => {});
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); scene.clear(); });

const reduced = (on: boolean) => vi.stubGlobal("matchMedia", () => ({ matches: on, addEventListener() {}, removeEventListener() {} }));
const alphaMaps = () => ((scene.children[0] as THREE.Group | undefined)?.children as THREE.Mesh[] | undefined ?? []).flatMap((m) => ((m.material as THREE.MeshBasicMaterial).alphaMap ? [(m.material as THREE.MeshBasicMaterial).alphaMap!] : []));
const colours = (p: CheckPayload) => {
  act(() => root.render(<ConnectionLines payload={p} lens="power" />));
  const group = scene.children[0] as THREE.Group;
  return (group.children as THREE.Mesh[]).map((m) => (m.material as THREE.MeshBasicMaterial).color.getHexString());
};

it("a swept rail's wires take the sag scale from the payload: green at nominal, red a quarter low, accent without data", () => {
  const full = colours(payload(12));
  expect(full).toContain("00ff00");
  const low = colours(payload(9));
  expect(low).toContain("ff0000");
  expect(low).not.toContain("00ff00");
  expect(low).toContain("0000ff"); // the unswept camera rail keeps the accent
});

it("dashes move with time, unless the person asked for less motion", () => {
  reduced(false);
  colours(payload());
  expect(alphaMaps().length).toBeGreaterThan(0);
  expect(frames.length).toBe(1);
  act(() => { frames.shift()!(1000); frames.shift()!(2000); });
  expect(alphaMaps().some((t) => t.offset.x < 0)).toBe(true);
  expect(ctx.needsRender).toBe(true);
});

it("with reduced motion the dashes stand still and no frame is ever asked for", () => {
  reduced(true);
  colours(payload());
  expect(frames).toHaveLength(0);
  expect(alphaMaps().length).toBeGreaterThan(0);
});

it("the legend says what colour and motion mean, with the numbers on each rail and nothing for unswept ones", () => {
  act(() => root.render(<ConnectionsPanel payload={payload(9)} lens="power" drawn={new Set()} />));
  const legend = container.querySelector(".conn-legend")!;
  expect(legend.textContent).toContain("Wire colour");
  expect(legend.textContent).toContain("Moving dashes: more current, faster.");
  const rails = [...legend.querySelectorAll<HTMLElement>("li")];
  expect(rails.map((li) => li.tabIndex)).toEqual([0, 0]);
  expect(rails.find((li) => li.title.includes("9 V of 12 V (75%)"))).toBeTruthy();
  expect(legend.textContent).not.toContain("pm02");
  act(() => root.render(<ConnectionsPanel payload={SCENE_DOC as unknown as CheckPayload} lens="power" drawn={new Set()} />));
  expect(container.querySelector(".conn-legend")).toBeNull();
});
