import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as THREE from "three";

import type { CheckPayload, ScenePayload } from "../shared/api.gen";
import type { SceneContext } from "./scene/buildScene";
import { getSelectedGroup, getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";
import { resetDesignView, setDesignView, getDesignView } from "./scene/designView";

let canvasPartIds: (string | null | undefined)[] = [];
const mountedScene = vi.hoisted(() => ({ ctx: null as SceneContext | null }));

vi.mock("./scene/Canvas", async () => {
  const THREE = await import("three");
  const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
  camera.position.set(0, 0, 5);
  camera.updateMatrixWorld();
  camera.updateProjectionMatrix();
  const meshes = ["hub", "frame"].map((id, index) => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));
    mesh.userData.partId = id;
    mesh.position.x = index * 2;
    mesh.updateMatrixWorld();
    return mesh;
  });
  mountedScene.ctx = { camera, meshes, resolution: new THREE.Vector2(800, 600) } as unknown as SceneContext;
  return {
    Canvas: ({ parts, children }: { parts: { id?: string | null }[]; children?: ReactNode }) => {
      canvasPartIds = parts.map((part) => part.id);
      return <div data-testid="scene-canvas">{children}</div>;
    },
    useScene: () => mountedScene.ctx!,
  };
});
const { DesignPanel, DesignParts, DesignLens, DesignTools, DesignSceneExtras } = await import("./DesignPanel");

// The Shell mounts these around its own RobotView; here they share one tree.
type Props = Parameters<typeof DesignPanel>[0];
function Page(props: Props) {
  return (
    <>
      <DesignParts payload={props.payload} />
      <DesignLens payload={props.payload} />
      <DesignTools payload={props.payload} canAnalyse={!!props.contract && !!props.expectedRevision} />
      <DesignPanel {...props} />
      <DesignSceneExtras payload={props.payload} />
    </>
  );
}
const expandAll = () => act(() => container.querySelectorAll<HTMLButtonElement>('.kit-tree-chev[aria-expanded="false"]').forEach((c) => c.click()));
const row = (name: string) => {
  expandAll();
  return [...container.querySelectorAll<HTMLButtonElement>(".kit-tree-row:not(.kit-tree-head)")].find((r) => r.textContent?.includes(name))!;
};

const payload = {
  scene: {
    source: "fixture.glb",
    asset_path: "/models/fixture.glb",
    asset_sha256: "actual-digest",
    expected_asset_sha256: "declared-digest",
    hash: "hash",
    parts: [
      { id: "hub", node: "Front hub", name: "Front hub", state: "fault", positions: {}, normals: null, indices: {}, color: [], translation: [], lenses: [], group: { id: "hub", label: "Hub", kind: "device" } },
      { id: "frame", node: "Frame", name: "Frame", state: "ok", positions: {}, normals: null, indices: {}, color: [], translation: [], lenses: [], group: { id: "frame", label: "Chassis", kind: "link" } },
      { id: "hub2", node: "Hub cap", name: "Hub cap", state: "ok", positions: {}, normals: null, indices: {}, color: [], translation: [], lenses: [], group: { id: "hub", label: "Hub", kind: "device" } },
      { id: "frame2", node: "Rail", name: "Rail", state: "ok", positions: {}, normals: null, indices: {}, color: [], translation: [], lenses: [], group: { id: "frame", label: "Chassis", kind: "link" } },
    ],
    labels: [
      { part: "hub", severity: "fault", title: "first", observed: null, comparator: "", expected: null },
      { part: "hub", severity: "fault", title: "second", observed: null, comparator: "", expected: null },
    ],
  },
  // Findings come from Check's cards: two failing ones light the hub (the labels above are only callouts).
  cards: ["first", "second"].map((title) => ({ severity: "violation", title, targets: [{ kind: "device", id: "hub", label: "Hub", parts: ["hub"] }] })),
  graph: {
    off: { nodes: [], edges: [], label: "off", caption: "Drag to orbit" },
    power: { nodes: [], edges: [], label: "Power", caption: "What feeds what" },
    data: { nodes: [], edges: [], label: "Data", caption: "" },
    mechanical: { nodes: [], edges: [], label: "Mechanical", caption: "" },
    health: { nodes: [], edges: [], label: "Health", caption: "" },
  },
} as unknown as CheckPayload;

let container: HTMLDivElement;
let root: Root;
let queuedFrame: FrameRequestCallback | null;
let rafSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  setSelectedPartId(null);
  resetDesignView();
  canvasPartIds = [];
  queuedFrame = null;
  rafSpy = vi.spyOn(globalThis, "requestAnimationFrame").mockImplementation((callback) => {
    queuedFrame = callback;
    return 1;
  });
  vi.spyOn(globalThis, "cancelAnimationFrame").mockImplementation(() => {});
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  rafSpy.mockRestore();
});

function flushOneFrame() {
  const callback = queuedFrame;
  queuedFrame = null;
  act(() => callback?.(0));
}

describe("Parts drop-downs", () => {
  const head = (id: string) => container.querySelector<HTMLButtonElement>(`[data-group-id="${id}"]`)!;
  const shown = () => [...container.querySelectorAll(".kit-tree-group ul .kit-tree-row")].map((r) => r.textContent);

  it("starts collapsed except the group holding a finding", () => {
    act(() => root.render(<Page payload={payload} />));
    expect(shown()).toEqual(["Front hub", "Hub cap"]);
    expect(head("frame").closest("li")?.getAttribute("data-open")).toBe("false");
    expect(container.querySelector(".kit-tree-count")?.textContent).toBe("2");
  });

  it("folds same-named parts into one row and says No findings when a group has none", () => {
    const part = (id: string) => ({ ...(payload.scene as ScenePayload).parts[1]!, id, node: id, name: "Fan blade", group: { id: "fan", label: "Fan", kind: "device" } });
    const fans = { ...payload, scene: { ...(payload.scene as ScenePayload), parts: [part("fan.001"), part("fan.002"), { ...part("fan.hub"), name: "Fan hub" }], labels: [] } } as unknown as CheckPayload;
    act(() => root.render(<Page payload={fans} />));
    expandAll();
    expect(shown()).toEqual(["Fan blade ×2", "Fan hub"]);
    act(() => head("fan").click());
    expect(container.querySelector(".design-face")?.textContent).toContain("No findings");
    expect(container.querySelector(".design-face .kit-kv")?.textContent).toContain(`Parts${head("fan").querySelector(".kit-tree-count")!.textContent}`); // folded rows: 2, not 3
  });

  it("a folded row shows any member's selection and opens its group", () => {
    const part = (id: string) => ({ ...(payload.scene as ScenePayload).parts[1]!, id, node: id, name: "Fan blade", group: { id: "fan", label: "Fan blade", kind: "device" } });
    const fans = { ...payload, scene: { ...(payload.scene as ScenePayload), parts: [part("fan.001"), part("fan.002"), { ...part("fan.hub"), name: "Fan hub" }], labels: [] } } as unknown as CheckPayload;
    act(() => root.render(<Page payload={fans} />));
    act(() => setSelectedPartId("fan.002"));
    expect(container.querySelector(".kit-tree-group ul .kit-tree-row.is-selected")?.textContent).toBe("Fan blade ×2");
    expect(container.querySelector(".design-face .kit-kv")?.textContent).not.toContain("Group"); // a group named like its part is not a row
  });

  it("selecting a part expands its group", () => {
    act(() => root.render(<Page payload={payload} />));
    act(() => setSelectedPartId("frame"));
    expect(shown()).toContain("Frame");
  });

  it("a header selects every member and the Inspector summarises the group", () => {
    act(() => root.render(<Page payload={payload} />));
    act(() => head("hub").click());
    expect(getSelectedGroup()).toEqual({ id: "hub", members: ["hub", "hub2"] });
    expect(container.querySelector(".design-face")?.textContent).toContain("Device");
    expect(container.textContent).toContain("Findings");
    act(() => setSelectedPartId("frame"));
    expect(getSelectedGroup()).toBeNull();
  });

  it("search opens matching groups and hides the rest; arrows toggle", () => {
    act(() => root.render(<Page payload={payload} />));
    const input = container.querySelector<HTMLInputElement>("input[type=search]")!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "fram");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(shown()).toEqual(["Frame"]);
    expect(head("hub")).toBeNull();
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => head("hub").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })));
    expect(shown()).toEqual([]);
  });
});

describe("DesignPanel", () => {
  it("with nothing picked there is no inspector card at all", () => {
    act(() => root.render(<Page payload={payload} />));
    expect(container.querySelector(".design-face")).toBeNull();
  });

  it("a picked part's inspector has no Name row", () => {
    act(() => root.render(<Page payload={payload} />));
    act(() => setSelectedPartId("frame"));
    expect(container.querySelector(".design-face")?.textContent).not.toMatch(/Name/);
  });

  it("a link grouped under a device does not read 'Part of <device>'", () => {
    const torso = { ...(payload.scene as ScenePayload).parts[1]!, id: "dev.jetson", node: "torso", mount_frame: "torso", name: "Torso", group: { id: "dev.jetson", label: "Jetson Orin", kind: "device" } };
    act(() => root.render(<Page payload={{ ...payload, scene: { ...(payload.scene as ScenePayload), parts: [torso], labels: [] } } as unknown as CheckPayload} />));
    act(() => setSelectedPartId("dev.jetson"));
    expect(container.querySelector(".design-face .kit-kv")?.textContent).not.toContain("Part of");
  });

  it("a part with no findings still shows group, state and findings", () => {
    act(() => root.render(<Page payload={payload} />));
    act(() => setSelectedPartId("frame"));
    expect(container.querySelector(".design-face .kit-kv")?.textContent).toMatch(/Part ofChassisStateOKFindingsNo findings/);
  });

  it("the tool pill keeps Callouts (disabled) when nothing applies", () => {
    const bare = { ...payload, scene: { ...(payload.scene as ScenePayload), labels: [] } } as unknown as CheckPayload;
    act(() => root.render(<Page payload={bare} />));
    const callouts = container.querySelector<HTMLButtonElement>('button[title="No callouts on this robot"]')!;
    expect(callouts.disabled).toBe(true);
    expect(callouts.getAttribute("aria-label")).toBe("No callouts on this robot");
  });

  it("discloses whether the mounted root matches the declared model asset, as a tooltip", () => {
    act(() => root.render(<Page payload={payload} />));
    act(() => setSelectedPartId("frame"));
    expect(container.querySelector(".scene-asset-identity")?.getAttribute("title")).toContain(
      "Selected asset /models/fixture.glb matches the declared model identity declared-digest.",
    );

    const unpinned = {
      ...payload,
      scene: { ...payload.scene, expected_asset_sha256: null },
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={unpinned} />));
    act(() => setSelectedPartId("frame"));
    expect(container.querySelector(".scene-asset-identity")?.getAttribute("title")).toContain(
      "Selected scene /models/fixture.glb; declared model identity is unavailable.",
    );
  });

  it("lists the scene parts in the tree with state dots and no canvas of its own", () => {
    act(() => root.render(<Page payload={payload} />));
    expandAll();
    const rows = [...container.querySelectorAll<HTMLButtonElement>(".kit-tree-group ul .kit-tree-row")];
    expect(rows.map((r) => r.textContent)).toEqual(["Front hub", "Hub cap", "Frame", "Rail"]);
    expect(rows[0]!.querySelector(".kit-s-bad")).not.toBeNull();
    expect(rows[2]!.querySelector(".kit-s-go")).toBeNull();
    expect(container.querySelector("[data-testid=scene-canvas]")).toBeNull();
  });

  it("shows the selected part in the inspector, with its finding count, both ways", () => {
    act(() => root.render(<Page payload={payload} />));
    act(() => row("Front hub").click());
    expect(getSelectedPartId()).toBe("hub");
    expect([...container.querySelectorAll(".kit-panel-title")].map((t) => t.textContent)).toContain("Front hub");
    expect(container.querySelector(".design-face")?.textContent).toContain("Findings2 · first");
    act(() => setSelectedPartId("frame"));
    expect(row("Frame").getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".design-face")?.textContent).toContain("No findings");
  });

  it("the inspector says Fails with the finding title, and a fixed joint reads Fixed to World", () => {
    act(() => root.render(<Page payload={payload} />));
    act(() => row("Front hub").click());
    expect(container.querySelector(".design-face .kit-kv")?.textContent).toContain("StateFails");
    expect(container.querySelector(".design-face")?.textContent).toContain("2 · first");
    expect(container.querySelector(".design-face .kit-kv")?.textContent).not.toContain("Lens");
    const skeleton = { scene: null, graph: null, joints: [{ name: "mount", type: "fixed", parent: "world", child: "base", parent_name: "World", child_name: "Base", position: [0, 0, 0], axis: [0, 0, 1], lower: null, upper: null, effort: null }], links: [] } as unknown as CheckPayload;
    act(() => root.render(<Page payload={skeleton} />));
    act(() => row("Base").click());
    expect(container.querySelector(".design-face")?.textContent).toContain("Fixed to World");
  });

  it("a solo device bucketed into Cameras stays draggable", () => {
    const mk = (id: string, name: string, kind: string) => ({ id, node: name, name, state: "ok", positions: {}, normals: null, indices: {}, color: [], translation: [], lenses: [], group: { id, label: name, kind } });
    const many = { ...payload, scene: { ...payload.scene, parts: [mk("cam", "Camera", "device"), ...Array.from({ length: 8 }, (_, i) => mk(`l${i}`, `Hip ${i}`, "link"))] } } as unknown as CheckPayload;
    act(() => root.render(<Page payload={many} />));
    expect(row("Camera").getAttribute("draggable")).toBe("true");
  });

  it("groups many one-part sided links under their limb", () => {
    const mk = (id: string, name: string) => ({ id, node: name, name, state: "ok", positions: {}, normals: null, indices: {}, color: [], translation: [], lenses: [], group: { id, label: `${name} Link`, kind: "link" } });
    const names = ["Left Hip Pitch", "Left Knee", "Right Hip Pitch", "Right Knee", "Left Elbow", "Left Wrist", "Right Elbow", "Right Wrist", "Head"];
    const many = { ...payload, scene: { ...payload.scene, parts: names.map((n, i) => mk(`p${i}`, n)) } } as unknown as CheckPayload;
    act(() => root.render(<Page payload={many} />));
    const heads = [...container.querySelectorAll(".kit-tree-label")].map((e) => e.textContent);
    expect(heads).toEqual(["Left leg2", "Right leg2", "Left arm2", "Right arm2"]);
    act(() => (container.querySelector(".kit-tree-label") as HTMLElement).click()); // open "Left leg"
    act(() => (container.querySelector('[data-part-id="p1"]') as HTMLElement).click());
    expect([...container.querySelectorAll(".kit-panel-title")].pop()?.textContent).toBe("Left leg · Knee"); // the side survives in the inspector title
  });

  it("switches the lens from the payload's own words, caption as tooltip", () => {
    act(() => root.render(<Page payload={payload} />));
    const tabs = [...container.querySelectorAll<HTMLElement>('[role="tab"]')];
    expect(tabs.map((t) => t.textContent).slice(0, 4)).toEqual(["All", "Power", "Data", "Mechanical"]); // Health: a tab in Pro, a greyed label in free
    act(() => tabs[1]!.click());
    expect(getDesignView().lens).toBe("power");
    expect(tabs[1]!.parentElement!.parentElement!.getAttribute("title")).toBe("What feeds what");
  });

  it("lists joints when there is no mesh, and inspects a picked joint", () => {
    const skeleton = {
      scene: null,
      graph: null,
      joints: [{ name: "shoulder", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Arm", position: [0, 0, 0], axis: [0, 0, 1], lower: -1.5, upper: 1.5, effort: null }],
      links: [],
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={skeleton} />));
    expect(container.querySelector(".design-face-empty")).toBeNull();
    expect(row("Arm").getAttribute("data-part-id")).toBe("shoulder"); // the link row picks the joint that carries it
    expect(row("Arm").querySelector(".kit-s-go")).toBeNull(); // dots on a skeleton too
    act(() => row("Arm").click());
    expect(container.querySelector(".design-face")?.textContent).toContain("revolute");
    expect(container.querySelector(".design-face")?.textContent).toContain("-1.5 raddeclared");
    expect([...container.querySelectorAll(".kit-panel-title")].pop()?.textContent).toBe("Arm"); // the link, not its joint "shoulder"
  });

  it("draws the links as a tree from the root and shows a part drawn as two meshes under one group", () => {
    const chain = {
      scene: null,
      joints: ["a", "b", "c"].map((child, i) => ({ name: `j${child}`, type: "fixed", parent: ["base", "a", "b"][i], child, parent_name: ["Base", "A", "B"][i], child_name: child.toUpperCase(), position: [0, 0, 0], axis: [0, 0, 1], lower: null, upper: null, effort: null })),
      links: [],
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={chain} />));
    expect([...container.querySelectorAll<HTMLElement>(".kit-tree-row")].map((r) => [r.textContent, r.style.paddingLeft])).toEqual([
      ["A", ""], ["B", "calc(var(--sp-2) + 1 * var(--sp-3))"], ["C", "calc(var(--sp-2) + 2 * var(--sp-3))"],
    ]);
    const part = (state: string) => ({ ...(payload.scene as ScenePayload).parts[0]!, id: "lidar", node: "lidar", name: "lidar", state });
    const twin = { ...payload, scene: { ...(payload.scene as ScenePayload), parts: [part("ok"), part("warn"), { ...part("ok"), id: "lidar-base", node: "lidar-base", name: "Lidar base" }] } } as unknown as CheckPayload;
    act(() => root.render(<Page payload={twin} />));
    expandAll();
    const rows = [...container.querySelectorAll<HTMLElement>(".kit-tree-group ul .kit-tree-row")];
    expect(rows.map((r) => r.textContent)).toEqual(["lidar ×2", "Lidar base"]); // same-named meshes fold into one row, under the one group
    expect(container.querySelector(".kit-tree-head .kit-s-warn")).not.toBeNull(); // the group dot is the worst of its parts
    expect(container.querySelector(".design-legend")?.textContent).toBe("warnok");
  });

  it("keeps row selection on the shared store used by the canvas", () => {
    act(() => root.render(<Page payload={payload} />));
    const frame = row("Frame");
    act(() => frame.click());
    expect(getSelectedPartId()).toBe("frame");
    expect(frame.getAttribute("aria-pressed")).toBe("true");
  });

  it("keeps selection through a health poll, but clears it when the scene identity is lost", () => {
    act(() => root.render(<Page payload={payload} contract="first.yaml" />));
    act(() => setSelectedPartId("frame"));

    const payloadScene = payload.scene as ScenePayload;
    const refreshed = {
      ...payload,
      scene: { ...payloadScene, parts: [...payloadScene.parts] },
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={refreshed} contract="first.yaml" />));
    expect(getSelectedPartId()).toBe("frame");

    act(() => root.render(<Page payload={null} contract="first.yaml" />));
    expect(getSelectedPartId()).toBeNull();

    const replacement = {
      ...refreshed,
      scene: { ...payloadScene, hash: "replacement-hash", parts: [...payloadScene.parts] },
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={refreshed} contract="first.yaml" />));
    act(() => setSelectedPartId("frame"));
    act(() => root.render(<Page payload={replacement} contract="first.yaml" />));
    expect(getSelectedPartId()).toBeNull();

    act(() => setSelectedPartId("frame"));
    act(() => root.render(<Page payload={replacement} contract="second.yaml" />));
    expect(getSelectedPartId()).toBeNull();

    act(() => setSelectedPartId("frame"));
    act(() => root.render(<></>));
    expect(getSelectedPartId()).toBe("frame"); // leaving Design keeps the part chosen
  });

  it("renders an active label on its mounted payload-id mesh and selects that same id", () => {
    const frameLabelPayload = {
      ...payload,
      scene: {
        ...payload.scene,
        labels: [{ part: "frame", severity: "fault", title: "Frame finding", observed: null, comparator: "", expected: null }],
      },
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={frameLabelPayload} />));
    const toggle = () => container.querySelector<HTMLButtonElement>('button[title="Callouts"]')!;
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
    act(() => toggle().click());
    flushOneFrame();

    const callout = container.querySelector<HTMLDivElement>('[data-callout-part-id="frame"]')!;
    expect(callout.hidden).toBe(false);
    expect(callout.textContent).toBe("Frame finding");
    const frameMesh = mountedScene.ctx!.meshes[1]!;
    const expected = new THREE.Box3().setFromObject(frameMesh).getCenter(new THREE.Vector3()).project(
      mountedScene.ctx!.camera,
    );
    expect(callout.style.transform).toContain(`${(expected.x * 0.5 + 0.5) * 800}px`);

    act(() => callout.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(getSelectedPartId()).toBe("frame");
    expect(row("Frame").getAttribute("aria-pressed")).toBe("true");
  });

  it("discloses mounted label overflow, clears it, and obeys the Callouts toggle", () => {
    const overflowLabels = Array.from({ length: 5 }, (_, index) => ({
      part: "hub",
      severity: "fault",
      title: `Finding ${index + 1}`,
      observed: null,
      comparator: "",
      expected: null,
    }));
    const overflowPayload = {
      ...payload,
      scene: {
        ...payload.scene,
        labels: overflowLabels,
      },
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={overflowPayload} />));
    const toggle = () => container.querySelector<HTMLButtonElement>('button[title="Callouts"]')!;
    expect(container.querySelectorAll(".callout")).toHaveLength(0);

    act(() => toggle().click());
    flushOneFrame();

    const disclosure = container.querySelector<HTMLElement>("[role=status]")!;
    expect(disclosure.textContent).toBe("1 additional callout exceeds the scene label limit.");
    expect(container.querySelectorAll(".callout")).toHaveLength(4);
    const callout = container.querySelector<HTMLDivElement>('[data-callout-part-id="hub"]')!;
    act(() => callout.click());
    expect(getSelectedPartId()).toBe("hub");

    const noOverflowPayload = {
      ...payload,
      scene: { ...payload.scene, labels: overflowLabels.slice(0, 2) },
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={noOverflowPayload} />));
    flushOneFrame();
    expect(container.querySelector("[role=status]")).toBeNull();

    act(() => toggle().click());
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelectorAll(".callout")).toHaveLength(0);
  });

  it("forwards the selected payload id and edit callbacks without owning IPC", async () => {
    const onEdit = vi.fn(async () => ({ written: true, refusal: null }));
    const onReopen = vi.fn(async () => payload);
    const onPayload = vi.fn();
    act(() => root.render(
      <Page
        payload={payload}
        contract="robot.yaml"
        expectedRevision="revision"
        onEdit={onEdit}
        onReopen={onReopen}
        onPayload={onPayload}
      />,
    ));

    const frameRow = row("Frame");
    act(() => frameRow.click());
    for (const [label, value] of [["Source", "caliper"], ["Witness", "Shivansh"]]) {
      const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      act(() => input.dispatchEvent(new Event("change", { bubbles: true })));
    }
    await act(async () => {
      container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(onEdit).toHaveBeenCalledWith({
      command: "move",
      args: { contract: "robot.yaml", target: "frame", offset: [0, 0, 0], unit: "mm", source: "caliper", witness: "Shivansh", expected_revision: "revision" },
    });
    expect(onReopen).toHaveBeenCalledWith("robot.yaml");
    expect(onPayload).toHaveBeenCalledWith(payload);
  });

  it("does not expose a design write control without the loaded revision", () => {
    const onEdit = vi.fn();
    act(() => root.render(
      <Page payload={payload} contract="robot.yaml" onEdit={onEdit} onReopen={vi.fn()} onPayload={vi.fn()} />,
    ));

    expect(container.querySelector("form")).toBeNull();
    expect(onEdit).not.toHaveBeenCalled();
  });
});
