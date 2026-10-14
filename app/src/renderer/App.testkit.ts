import type { PagePayload, PlanPayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { vi } from "vitest";

// Fixtures shared by the free App tests and the pro/ App tests.

// CheckFace renders into Frame, which reads window.matchMedia for the OS theme (jsdom has none).
export function mockMatchMedia(): void {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

export const picked = (opened: unknown) => ({ started: false, opened });

export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

// The optional health extras are not part of the free fixture.
export const PAGE = {
  revision: "revision",
  robot: "Holybro X500 V2 PX4 reference quadrotor",
  contract_id: "x500",
  state: "go",
  headline: X500_CHECK_PAYLOAD.headline,
  qualifier: "",
  scored: X500_CHECK_PAYLOAD.scored,
  violations: 0,
  warnings: 0,
  unchecked: 0,
  adds_up: [],
  cards: X500_CHECK_PAYLOAD.cards,
  joints: [],
  links: [],
  graph: {
    off: { nodes: [], edges: [], label: "Off", caption: "" },
    mechanical: { nodes: [], edges: [], label: "Mechanical", caption: "" },
    power: { nodes: [], edges: [], label: "Power", caption: "" },
    data: { nodes: [], edges: [], label: "Data", caption: "" },
    health: { nodes: [], edges: [], label: "Health", caption: "" },
  },
  budgets: [],
  constraints: [],
  setup: [],
  run: null,
  scene: null,
  modes: [],
  mode_labels: {},
  command_modes: {},
  health: null,
  health_devices: null,
  cli_line: "lockwire gui /tmp/x500/lockwire.yaml --out lockwire-gui.html",
  document_identity: "x500",
  faces: {
    design: { title: "Design", parts: [], cli: "lockwire check /tmp/x500/lockwire.yaml" },
    declare: { title: "Declare", lines: [] },
    plan: { title: "Check a plan before it runs", cli: "lockwire plan /tmp/x500/lockwire.yaml plan.yaml", outcomes: null },
    reference: { title: "Reference", drawers: [], devices: [], power_tree: null },
  },
} as unknown as PagePayload;

export const DECLARE_FORM = {
  revision: "revision",
  rows: [{ path: "mass.total", label: "Total mass", group: "Total", short: "mass", unit: "kg", kind: "declared" as const, value: 2,
    provenance: { source: "weigh-in.csv", witness: "Maya", locked_at: "2026-09-09" } }],
  witness: { prefill: "Maya" },
};
export const PLAN: PlanPayload = {
  format_version: "2", schema_version: "1", robot: "X500", verdict: "GO" as const, exit_code: 0,
  counts: { violation: 0, warning: 0, not_scored: 0 }, scored: 1, findings: [],
  artifact_type: "lockwire-plan-report" as const, steps: [],
};
export const HISTORY = [{ sha: "abc123", author: "Maya", date: "2026-09-09", message: "Re-lock mount", verdict: "GO" as const, fact_diff: null }];
export const DIFF = { changed: false, changes: [], added: [], removed: [], findings_new: [], findings_gone: [] };
export const PAGE_WITH_SCENE = {
  ...PAGE,
  scene: {
    source: "fixture.glb",
    hash: "hash",
    parts: [{
      id: "frame",
      node: "Frame",
      name: "Frame",
      state: "ok",
      positions: { type: "i16", data: "" },
      normals: null,
      indices: { type: "u16", data: "" },
      color: [],
      translation: [],
      group: { id: "other", label: "Other", kind: "other" },
      lenses: ["off"],
    }],
    labels: [],
    scene_only: [],
    contract_only: [],
    bounds: [],
    triangles: 0,
    vertices: 0,
    display_triangles: 0,
    honesty: "fixture",
  },
} as unknown as PagePayload;
