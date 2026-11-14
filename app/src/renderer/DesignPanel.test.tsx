import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as THREE from "three";

import type { CheckPayload, ScenePayload } from "../shared/api.gen";
import type { SceneContext } from "./scene/buildScene";
import { getSelectedGroup, getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";
import { getViewState, zeroPose } from "./scene/viewState";
import { pro } from "./proRegistry";
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
const { DesignPanel, DesignParts, DesignLens, DesignTools, DesignSceneExtras, placementWords } = await import("./DesignPanel");

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
    expect(container.querySelector(".design-face")?.textContent).toContain("Part");
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

  describe("GAP-delkey: Delete removes the selected part through the engine", () => {
    const pressKey = (key: string) => act(async () => { document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })); });
    const withJetson = () => {
      const torso = { ...(payload.scene as ScenePayload).parts[1]!, id: "dev.jetson", node: "torso", mount_frame: "torso", name: "Torso", group: { id: "dev.jetson", label: "Jetson Orin", kind: "device" } };
      return { ...payload, scene: { ...(payload.scene as ScenePayload), parts: [torso], labels: [] } } as unknown as CheckPayload;
    };
    const mountWith = (p: CheckPayload, onEdit: NonNullable<Props["onEdit"]>) => act(() => root.render(<Page payload={p} contract="robot.yaml" expectedRevision="r7" onEdit={onEdit} onReopen={vi.fn().mockResolvedValue(p)} onPayload={vi.fn()} />));

    it("a selected device posts kind device with its name and the revision; nothing selected posts nothing", async () => {
      const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
      mountWith(withJetson(), onEdit);
      const tree = container.querySelector<HTMLElement>(".kit-tree-row")!;
      tree.focus();
      await pressKey("Delete");
      expect(onEdit).not.toHaveBeenCalled(); // nothing selected
      act(() => setSelectedPartId("dev.jetson"));
      tree.focus();
      await pressKey("Delete");
      expect(onEdit).toHaveBeenCalledTimes(1);
      expect(onEdit.mock.calls[0]![0]).toMatchObject({ command: "delete", args: { contract: "robot.yaml", kind: "device", name: "dev.jetson", expected_revision: "r7" } });
    });

    it("Ctrl+Z still undoes with nothing selected (the state right after a delete)", async () => {
      const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
      mountWith(withJetson(), onEdit);
      await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true })); });
      expect(onEdit.mock.calls[0]![0]).toEqual({ command: "undo-edit", args: { contract: "robot.yaml" } });
    });

    const wheeled = { scene: null, graph: null, links: [], joints: [{ name: "wheel_fl_joint", type: "continuous", parent: "chassis", child: "wheel_fl", parent_name: "Chassis", child_name: "Front left wheel", position: [0, 0, 0], axis: [0, 1, 0], lower: null, upper: null, effort: null }] } as unknown as CheckPayload;
    it("the root part is listed, can be picked, and Delete and the edit form stay away from it", async () => {
      const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
      mountWith(wheeled, onEdit);
      act(() => row("Chassis").click());
      expect(document.body.textContent).toContain("Root part");
      expect(container.textContent).not.toContain("Edit placement");
      row("Chassis").focus();
      await pressKey("Delete");
      expect(onEdit).not.toHaveBeenCalled();
    });

    it("Escape closes the Add part and Add shape forms and writes nothing", () => {
      const add = { witness: "Maya", catalog: { rails: [], parts: [], stock: [], kit: [] }, onAdd: vi.fn(), shape: { links: [], params: [], onAdd: vi.fn(), onSize: vi.fn() } };
      act(() => root.render(<DesignParts payload={wheeled as never} add={add} />));
      const esc = () => act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
      const click = (label: string) => act(() => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === label)!.click());
      const has = (label: string) => Array.from(container.querySelectorAll("button")).some((b) => b.textContent === label);
      click("Add part"); expect(has("Add shape")).toBe(false);
      esc(); expect(has("Add shape")).toBe(true);
      click("Add shape"); expect(container.querySelector('[aria-label="Add shape"]')).not.toBeNull();
      esc(); expect(container.querySelector('[aria-label="Add shape"]')).toBeNull();
      expect(add.onAdd).not.toHaveBeenCalled(); expect(add.shape.onAdd).not.toHaveBeenCalled();
    });

    it("deleting a wheel's row deletes the part: kind link with the child's name, and Backspace works too", async () => {
      const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
      mountWith(wheeled, onEdit);
      act(() => row("Front left wheel").click());
      row("Front left wheel").focus();
      await pressKey("Backspace");
      expect(onEdit.mock.calls[0]![0]).toMatchObject({ command: "delete", args: { kind: "link", name: "wheel_fl", expected_revision: "r7" } });
      expect(document.body.textContent).toContain("Deleted Front left wheel.");
    });

    it("the engine's refusal of a delete shows as it came", async () => {
      const refusal = "wheel_fl carries dev.motor_fl; re-mount or delete it first."; // edit.py delete_entry wording
      mountWith(wheeled, vi.fn().mockResolvedValue({ written: false, refusal }));
      act(() => row("Front left wheel").click());
      row("Front left wheel").focus();
      await pressKey("Delete");
      expect(document.querySelector(".typed-move")?.textContent).toBe(refusal);
    });
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
    expect(tabs.map((t) => t.textContent).slice(0, 4)).toEqual(["All", "Power", "Data", "Mechanical"]); // Health: a tab when the pro folder is loaded, absent otherwise
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

  describe("PRINT-BTN Print this part", () => {
    const solid = { kind: "box", size: [1, 1, 1], position: [0, 0, 0], rotation: [0, 0, 0, 1] };
    const mk = (solids: object[]) => ({ scene: null, graph: null, links: [{ name: "arm", position: [0, 0, 0], solids }], joints: [{ name: "shoulder", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Arm", position: [0, 0, 0], axis: [0, 0, 1], lower: -1, upper: 1, effort: null }] }) as unknown as CheckPayload;
    const btn = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Print this part")!;
    const open = (p: CheckPayload, printPart = vi.fn().mockResolvedValue("Opened arm.stl in millimetres.")) => {
      Object.assign(window.lockwire ?? (window.lockwire = {} as never), { printPart });
      act(() => root.render(<Page payload={p} contract="/r.yaml" />));
      act(() => row("Arm").click());
      return printPart;
    };

    it("one click sends the contract and the selected link, then shows the engine's line", async () => {
      const printPart = open(mk([solid]));
      expect(btn().disabled).toBe(false);
      await act(async () => btn().click());
      expect(printPart).toHaveBeenCalledWith("/r.yaml", "arm");
      expect(container.querySelector(".print-part [role=status]")?.textContent).toBe("Opened arm.stl in millimetres.");
    });

    it("picking a joint row sends the joint's child link, never the joint's own name", async () => {
      const printPart = open(mk([solid]));
      expect(container.querySelector<HTMLElement>(".kit-tree-row[data-part-id='shoulder']")).not.toBeNull();
      await act(async () => btn().click());
      expect(printPart.mock.calls[0]![1]).toBe("arm");
    });

    it("is disabled with the reason, and sends nothing, when the link has no shape", async () => {
      const printPart = open(mk([]));
      expect(btn().disabled).toBe(true);
      expect(container.querySelector(".print-part")?.textContent).toContain("This part has no shape to print.");
      await act(async () => btn().click());
      expect(printPart).not.toHaveBeenCalled();
    });
  });

  describe("POSE-1 Pose sliders", () => {
    const arm = { scene: null, graph: null, links: [], joints: [{ name: "shoulder", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Arm", position: [0, 0, 0], axis: [0, 0, 1], lower: -1, upper: 1, effort: null }] } as unknown as CheckPayload;
    const slider = () => container.querySelector<HTMLInputElement>('input[aria-label="Pose Arm"]')!;
    const drag = (to: number) => act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(slider(), String(to)); slider().dispatchEvent(new Event("input", { bubbles: true })); });
    afterEach(() => act(() => zeroPose()));

    it("says so on a slider whose joint declares no limits, and not on one that does", () => {
      const free = { ...arm, joints: [{ ...(arm as unknown as { joints: object[] }).joints[0], lower: null, upper: null }] } as unknown as CheckPayload;
      act(() => root.render(<Page payload={arm} />));
      act(() => row("Arm").click());
      expect(container.textContent).not.toContain("No limits declared");
      act(() => root.render(<Page payload={free} />));
      expect(container.textContent).toContain("No limits declared; showing a half turn each way");
    });

    it("says Not saved while posed, and Back to zero clears it with nothing written", () => {
      const onEdit = vi.fn(), onReopen = vi.fn(), onPayload = vi.fn(), writeContract = vi.fn(), declare = vi.fn();
      Object.assign(window.lockwire ?? (window.lockwire = {} as never), { writeContract, declare });
      act(() => root.render(<Page payload={arm} contract="/r.yaml" expectedRevision="r1" onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />));
      act(() => row("Arm").click());
      expect(container.textContent).not.toContain("Not saved");
      drag(30);
      expect(container.textContent).toContain("Posed for viewing only. Not saved.");
      expect(getViewState().pose.shoulder).toBeCloseTo((30 * Math.PI) / 180);
      act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Back to zero")!.click());
      expect(container.textContent).not.toContain("Not saved");
      expect([onEdit, onReopen, onPayload, writeContract, declare].map((f) => f.mock.calls.length)).toEqual([0, 0, 0, 0, 0]);
    });
  });

  describe("POSE-1-fix zero-pose figures", () => {
    const armF = { scene: null, graph: null, links: [], forces: withForces(true).forces, joints: [{ name: "shoulder", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Arm", position: [0, 0, 0], axis: [0, 0, 1], lower: -1, upper: 1, effort: null }] } as unknown as CheckPayload;
    const com = () => mountedScene.ctx!.scene.getObjectByName("centre-of-mass");
    const sentence = "Mass, torque and tip-over figures are for the zero pose.";
    const drag = (to: number) => act(() => { const s = container.querySelector<HTMLInputElement>('input[aria-label="Pose Arm"]')!; Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(s, String(to)); s.dispatchEvent(new Event("input", { bubbles: true })); });
    afterEach(() => act(() => zeroPose()));

    it("hides the centre-of-mass marker while posed and Back to zero restores it", () => {
      mountedScene.ctx!.scene = new THREE.Scene();
      act(() => root.render(<Page payload={armF} />));
      act(() => row("Arm").click());
      expect(com()).toBeTruthy();
      drag(30);
      expect(com()).toBeUndefined();
      act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Back to zero")!.click());
      expect(com()).toBeTruthy();
    });

    it("hides the torque callouts while posed and Back to zero restores them", () => {
      const withHead = { ...armF, joints: [{ ...(armF as unknown as { joints: object[] }).joints[0], headroom: { ratio: 1.9 } }] } as unknown as CheckPayload;
      act(() => root.render(<Page payload={withHead} />));
      act(() => row("Arm").click());
      expect(container.querySelector(".callout-layer")).toBeTruthy();
      drag(30);
      expect(container.querySelector(".callout-layer")).toBeNull();
      act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Back to zero")!.click());
      expect(container.querySelector(".callout-layer")).toBeTruthy();
    });

    it("hides the support polygon while posed and Back to zero restores it", () => {
      mountedScene.ctx!.scene = new THREE.Scene();
      const withPoly = { ...armF, support_polygon: { z: 0, points: [[0, 0], [1, 0], [1, 1]] } } as unknown as CheckPayload;
      const poly = () => mountedScene.ctx!.scene.getObjectByName("support-polygon");
      act(() => root.render(<Page payload={withPoly} />));
      act(() => row("Arm").click());
      expect(poly()).toBeTruthy();
      drag(30);
      expect(poly()).toBeUndefined();
      act(() => [...container.querySelectorAll("button")].find((b) => b.textContent === "Back to zero")!.click());
      expect(poly()).toBeTruthy();
    });

    it("says the figures are for the zero pose while posed, and not otherwise", () => {
      act(() => root.render(<Page payload={armF} />));
      act(() => row("Arm").click());
      expect(container.textContent).not.toContain(sentence);
      drag(30);
      expect(container.textContent).toContain(sentence);
    });
  });

  it("a typed move on a joint row moves its child link from the joint's origin", async () => {
    const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
    const skeleton = {
      scene: null, graph: null, links: [],
      joints: [{ name: "shoulder", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Arm", position: [0, 0, 0], axis: [0, 0, 1], origin_mm: [10, 20, 30], lower: null, upper: null, effort: null }],
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={skeleton} contract="robot.yaml" expectedRevision="r1" onEdit={onEdit} onReopen={vi.fn().mockResolvedValue(skeleton)} onPayload={vi.fn()} />));
    act(() => row("Arm").click());
    for (const key of ["g", "x", "5", "Enter"]) await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })); });
    expect(onEdit.mock.calls[0]![0]).toMatchObject({ command: "move", args: { target: "arm", offset: [15, 20, 30], unit: "mm" } }); // the child link, not "shoulder"; origin plus 5
  });

  it("a rotary joint's inspector offers Swap servo and opens the Part Swap panel for it", async () => {
    const swapPreview = vi.fn().mockResolvedValue({ joint: "shoulder", current: null, load_nm: 0.8, chosen: null, delta_g: 0, candidates: [], knock_on: [], notes: [] });
    Object.assign(window, { lockwire: { swapPreview } });
    const j = { type: "revolute", parent: "base", parent_name: "Base", position: [0, 0, 0], axis: [0, 0, 1], lower: null, upper: null, effort: null };
    const skeleton = { scene: null, graph: null, links: [], joints: [{ ...j, name: "shoulder", child: "arm", child_name: "Arm" }, { ...j, name: "mount", type: "fixed", parent: "arm", parent_name: "Arm", child: "cam", child_name: "Cam" }] } as unknown as CheckPayload;
    act(() => root.render(<Page payload={skeleton} contract="/r.yaml" expectedRevision="rev1" onSwapped={() => {}} />));
    const swap = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Swap servo");
    act(() => setSelectedPartId("mount"));
    expect(swap()).toBeUndefined(); // a fixed joint has no servo
    act(() => row("Arm").click());
    await act(async () => swap()!.click());
    expect(swapPreview).toHaveBeenCalledWith({ contract: "/r.yaml", joint: "shoulder" });
    expect(container.textContent).toContain("Swap the shoulder servo");
  });

  it("draws the links as a tree from the root and shows a part drawn as two meshes under one group", () => {
    const chain = {
      scene: null,
      joints: ["a", "b", "c"].map((child, i) => ({ name: `j${child}`, type: "fixed", parent: ["base", "a", "b"][i], child, parent_name: ["Base", "A", "B"][i], child_name: child.toUpperCase(), position: [0, 0, 0], axis: [0, 0, 1], lower: null, upper: null, effort: null })),
      links: [],
    } as unknown as CheckPayload;
    act(() => root.render(<Page payload={chain} />));
    expect([...container.querySelectorAll<HTMLElement>(".kit-tree-row")].map((r) => [r.textContent, r.style.paddingLeft])).toEqual([
      ["Base", ""], ["A", ""], ["B", "calc(var(--sp-2) + 1 * var(--sp-3))"], ["C", "calc(var(--sp-2) + 2 * var(--sp-3))"],
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
    act(() => [...container.querySelectorAll<HTMLLabelElement>('[aria-label="Operation"] label')].find((l) => l.textContent === "Mount")!.querySelector("input")!.click()); // a frame has no known position or turn, so Move and Rotate would refuse; Mount still forwards the write
    await act(async () => {
      container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(onEdit).toHaveBeenCalledWith({
      command: "mount-shape",
      args: { contract: "robot.yaml", target: "frame", shape: { kind: "plate", dimensions_mm: [0, 0, 0] }, source: "placed in the app", witness: "", expected_revision: "revision" },
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

const fig = (value: unknown, unit: string, extra = {}) => ({ value, unit, kind: "derived-by-x", ...extra });
const withForces = (cgComplete: boolean) => ({
  ...payload,
  forces: {
    mass_total: fig(8, "kg"),
    cg: fig([0.75, 0, 0.1], "m", { frame: "base", complete: cgComplete, note: "all joints at zero; the real centre of mass moves as the joints move" }),
    inertia: fig([0, 1.5, 1.5, 0, 0, 0], "kg*m^2"),
    masses: [fig(2, "kg", { id: "devices/dev.fore/physical/mass" }), { value: null, unit: "kg", kind: "not_scored", id: "model/links/arm_2/mass", not_scored_because: "x" }],
  },
}) as unknown as CheckPayload;

describe("Mass and balance", () => {
  it("shows total mass, centre of mass and inertia, and names a part with no mass instead of counting it as 0 kg", () => {
    act(() => root.render(<Page payload={withForces(true)} />));
    const text = container.textContent ?? "";
    expect(text).toContain("Robot mass: 8 kg");
    expect(text).toContain("x 750, y 0, z 100 mm in base");
    expect(text).toContain("Iyy 1.50");
    expect(text).toContain("Centre of mass: all joints at zero; the real centre of mass moves as the joints move.");
    expect(text).toContain("No mass declared for arm_2. The total leaves it out, it does not count it as 0 kg.");
  });

  const kinds = () => ({
    ...withForces(true),
    forces: { ...withForces(true).forces!, masses: [
      fig(0.3, "kg", { id: "model/links/arm_1/mass", mass_kind: "estimated", material: "PLA (Polymaker PolyLite)" }),
      fig(0.5, "kg", { id: "model/links/arm_2/mass", mass_kind: "measured" }),
      fig(0.2, "kg", { id: "model/links/arm_3/mass", mass_kind: "calculated" }),
    ] },
  }) as unknown as CheckPayload;
  const partLine = (name: string) => [...container.querySelectorAll(".mass-part")].find((p) => p.textContent?.startsWith(name))!;

  it("says how mature each mass is and shows no band, since no band has a source", () => {
    act(() => root.render(<Page payload={kinds()} />));
    expect(partLine("arm_1").textContent).toContain("estimated");
    expect(partLine("arm_3").textContent).toContain("from the slicer");
    expect(partLine("arm_2").textContent).toContain("weighed");
    expect(container.textContent).not.toMatch(/band/i);
  });

  const MATS = [
    { name: "Aluminium 6061-T6", density_kg_m3: 2700, source: "https://a.example/al.pdf (Aalco datasheet: \"2.70\"), read 2026-11-13", source_label: "Aalco datasheet", source_url: "https://a.example/al.pdf", caveat: null },
    { name: "PLA (Polymaker PolyLite)", density_kg_m3: 1170, source: "https://p.example/pla.pdf (Polymaker PLA TDS V5.4: \"1.17\"), read 2026-11-13; solid filament density; a printed part's density depends on infill", source_label: "Polymaker PLA TDS V5.4", source_url: "https://p.example/pla.pdf", caveat: "Solid filament density; a printed part's density depends on infill." },
  ];
  const withMats = (extra = {}) => ({ ...kinds(), materials: MATS, ...extra }) as unknown as CheckPayload;

  it("a derived mass row says so from its kind and shows its density source; the wording is the engine's", () => {
    const base = withMats();
    const masses = [fig(0.3, "kg", { id: "model/links/arm_1/mass", kind: "derived", mass_kind: "estimated", mass_note: "worked out from shapes (density source: https://p.example/pla.pdf (Polymaker PLA TDS V5.4: \"1.17\"), read 2026-11-13)" }), fig(0.5, "kg", { id: "model/links/arm_2/mass", mass_kind: "measured" })];
    act(() => root.render(<Page payload={{ ...base, forces: { ...base.forces!, masses } } as unknown as CheckPayload} />));
    expect(partLine("arm_1").textContent).toContain("worked out from shapes (density source: Polymaker PLA TDS V5.4)");
    expect(partLine("arm_1").textContent).not.toContain("estimated");
    expect(partLine("arm_2").textContent).toContain("weighed");
    expect(partLine("arm_2").textContent).not.toContain("derived");
  });

  it("the picker lists exactly the catalog materials with their sources, and no handbook wording", () => {
    act(() => root.render(<Page payload={withMats()} contract="robot.yaml" expectedRevision="r1" onEdit={vi.fn()} />));
    const options = [...partLine("arm_1").querySelectorAll("option")].map((o) => o.textContent);
    expect(options).toEqual(["Pick a material", "Aluminium 6061-T6 (2700 kg/m³)", "PLA (Polymaker PolyLite) (1170 kg/m³)", "My own material..."]);
    expect(container.textContent).toContain("Aluminium 6061-T6, 2700 kg/m³. Source: Aalco datasheet");
    expect(container.querySelector('a[href="https://p.example/pla.pdf"]')).not.toBeNull();
    expect(container.textContent).not.toMatch(/typical|handbook/i);
  });

  it("the filament caveat shows for a plastic and not for a metal", () => {
    const pla = withMats();
    const aluminium = withMats({ forces: { ...pla.forces!, masses: [fig(0.3, "kg", { id: "model/links/arm_1/mass", mass_kind: "estimated", material: "Aluminium 6061-T6" })] } });
    act(() => root.render(<Page payload={aluminium} contract="robot.yaml" expectedRevision="r1" onEdit={vi.fn()} />));
    expect(partLine("arm_1").textContent).not.toContain("infill");
    const plastic = withMats({ forces: { ...pla.forces!, masses: [fig(0.3, "kg", { id: "model/links/arm_1/mass", mass_kind: "estimated", material: "PLA (Polymaker PolyLite)" })] } });
    act(() => root.render(<Page payload={plastic} contract="robot.yaml" expectedRevision="r1" onEdit={vi.fn()} />));
    expect(partLine("arm_1").textContent).toContain("a printed part's density depends on infill");
  });

  const NAMES = ["Aluminium 6061-T6", "Mild steel S235JR", "Stainless steel 304", "Titanium Ti-6Al-4V", "PLA", "PETG", "ABS"];
  const crow = (material: string, link: number, total: number, margin: number | null, extra = {}) => ({ material, delta_mass_kg: 0, delta_margin_nm: 0, density_kg_m3: 1, link_mass_kg: link, total_mass_kg: total, worst_margin_nm: margin, worst_joint: "shoulder", current: false, printed: false, ...extra });
  const COMPARE = [{ link: "arm_1", note: "Printed materials are shown as solid 100 percent infill; real prints weigh less.", rows: NAMES.map((n, i) => crow(n, 0.5 + i, 8 + i, 5 - i, { current: i === 0, printed: i >= 4, delta_mass_kg: i * 1.5, delta_margin_nm: -i * 2.5 })) }];
  const withCompare = (compare: unknown = COMPARE) => withMats({ material_compare: compare, materials: [...MATS, { name: "Mild steel S235JR", density_kg_m3: 7850, source: "https://s.example (EN 10025)", source_label: "EN 10025", source_url: null, caveat: null }], forces: { ...kinds().forces!, masses: [fig(0.5, "kg", { id: "model/links/arm_1/mass", kind: "derived", mass_kind: "estimated", material: "Aluminium 6061-T6" })] } });
  const compareRows = () => [...container.querySelectorAll(".material-compare tbody tr")] as HTMLElement[];
  const mount = (payload: CheckPayload, extra = {}) => act(() => root.render(<Page payload={payload} contract="robot.yaml" expectedRevision="r1" onEdit={vi.fn().mockResolvedValue({ written: true, refusal: null })} {...extra} />));

  it("Compare materials lists one row per catalog material, the engine's figures as changes from the current one", () => {
    mount(withCompare());
    expect(compareRows().map((r) => r.querySelector("button")!.textContent)).toEqual(NAMES);
    expect(compareRows()[1]!.textContent).toContain("+1.5 kg");
    expect(compareRows()[1]!.textContent).toContain("-2.5 Nm");
  });

  const srow = (material: string, verdict: string, extra = {}) => ({ material, figure: null, strength_mpa: 40, limit_mpa: 20, stress_mpa: 18.4, margin: 1.09, verdict, caveat: null, source: null, ...extra });
  const STRENGTH = { warning: "A simple beam estimate, not FEA.", links: [{ link: "arm_1", moment_nm: 3, rows: [srow("PLA", "flagged", { margin: 0.8, caveat: "Prints are weaker across layers." }), srow("Aluminium 6061-T6", "ok"), srow("PETG", "no data", { limit_mpa: null, margin: null })] }] };
  const strengthRows = () => [...container.querySelectorAll(".material-strength tbody tr")] as HTMLElement[];

  it.skipIf(!pro)("Pro: a flagged strength row shows flagged, its margin and its caveat; the warning shows once", () => {
    mount({ ...withCompare(), material_strength: STRENGTH } as unknown as CheckPayload);
    expect(strengthRows()[0]!.textContent).toContain("flagged");
    expect(strengthRows()[0]!.textContent).toContain("0.8x");
    expect(strengthRows()[0]!.textContent).toContain("18.4 / 20 MPa");
    expect(strengthRows()[0]!.textContent).toContain("Prints are weaker across layers.");
    expect(container.textContent!.split("A simple beam estimate, not FEA.")).toHaveLength(2);
  });

  it.skipIf(!pro)("Pro: a no data row says so and shows no numbers; no column when the link is missing", () => {
    mount({ ...withCompare(), material_strength: STRENGTH } as unknown as CheckPayload);
    expect(strengthRows()[2]!.textContent).toBe("PETGno data");
    mount({ ...withCompare(), material_strength: { ...STRENGTH, links: [] } } as unknown as CheckPayload);
    expect(container.querySelector(".material-strength")).toBeNull();
    mount({ ...withCompare(), material_strength: null } as unknown as CheckPayload);
    expect(container.querySelector(".material-strength")).toBeNull();
  });

  it("the free build (no Pro slot) renders no strength column even when the payload carries it", () => {
    const slot = pro?.MaterialCompareMore; // pro is null in the free tree: the slot is already absent
    if (pro) pro.MaterialCompareMore = undefined;
    try { mount({ ...withCompare(), material_strength: STRENGTH } as unknown as CheckPayload); } finally { if (pro) pro.MaterialCompareMore = slot; }
    expect(container.querySelector(".material-strength")).toBeNull();
    expect(container.textContent).not.toContain("flagged");
    expect(compareRows()).toHaveLength(7);
  });

  it("a current material outside the list says the figures are totals, not changes", () => {
    mount(withCompare([{ link: "arm_1", note: null, rows: COMPARE[0]!.rows.map((r) => ({ ...r, current: false, delta_mass_kg: null, delta_margin_nm: null })) }]));
    expect(container.textContent).toContain("not in the material list, so these are totals, not changes");
    expect(compareRows()[1]!.textContent).toContain("9 kg");
  });

  it("the current material's row is marked and cannot be picked again", () => {
    mount(withCompare());
    expect(compareRows().filter((r) => r.getAttribute("aria-current") === "true").map((r) => r.querySelector("button")!.textContent)).toEqual(["Aluminium 6061-T6"]);
    expect(compareRows()[0]!.querySelector("button")!.disabled).toBe(true);
  });

  it("one tap on the steel row writes steel through declare", async () => {
    const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
    mount(withCompare(), { onEdit });
    await act(async () => { compareRows()[1]!.querySelector("button")!.click(); });
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ path: "model/links/arm_1/material", value: { name: "Mild steel S235JR", density_kg_m3: 7850 } });
    expect(onEdit.mock.calls[0]![0].args).not.toHaveProperty("formal"); // a quick edit, not the Declare form
    expect(onEdit.mock.calls[0]![0].args.witness).toBe("");
  });

  it("printed rows say solid infill and metals do not; a declared mass gets the honest line and no table", () => {
    mount(withCompare());
    expect(compareRows()[4]!.textContent).toContain("solid infill");
    expect(compareRows()[1]!.textContent).not.toContain("infill");
    mount(withCompare([{ link: "arm_1", rows: [], note: "x" }]));
    expect(container.textContent).toContain("Declared, no volume to compare");
    expect(compareRows()).toHaveLength(0);
  });

  it("the material picker writes the catalog material and its source through declare and reloads the page", async () => {
    const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
    const onPayload = vi.fn();
    const onReopen = vi.fn().mockResolvedValue({ reopened: true });
    act(() => root.render(<Page payload={withMats()} contract="robot.yaml" expectedRevision="r1" onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />));
    const select = partLine("arm_1").querySelector("select")!;
    expect(select.value).toBe("PLA (Polymaker PolyLite)");
    await act(async () => { select.value = "Aluminium 6061-T6"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    const sent = onEdit.mock.calls[0]![0];
    expect(sent.command).toBe("declare");
    expect(sent.args).toMatchObject({ contract: "robot.yaml", path: "model/links/arm_1/material", expected_revision: "r1", value: { name: "Aluminium 6061-T6", density_kg_m3: 2700, source: MATS[0]!.source } });
    expect(sent.args).not.toHaveProperty("formal"); // a quick edit, not the Declare form
    expect(sent.args.witness).toBe("");
    expect(onPayload).toHaveBeenCalledWith({ reopened: true });
    expect(partLine("arm_1").textContent).toContain("Saved. The mass stays as declared until you weigh it or import a slicer mass.");
  });

  it("your own material needs a name, a density and a source, and is written through declare", async () => {
    const onEdit = vi.fn().mockResolvedValue({ written: true, refusal: null });
    act(() => root.render(<Page payload={withMats()} contract="robot.yaml" expectedRevision="r1" onEdit={onEdit} onReopen={vi.fn()} onPayload={vi.fn()} />));
    const select = partLine("arm_1").querySelector("select")!;
    await act(async () => { select.value = "__own__"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(onEdit).not.toHaveBeenCalled();
    const type = (label: string, v: string) => { const el = container.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!; act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, v); el.dispatchEvent(new Event("input", { bubbles: true })); }); };
    const save = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Save material")!;
    type("own material name", "Birch ply"); type("own material density", "680");
    expect(save().disabled).toBe(true);
    type("own material source", "maker data sheet");
    expect(save().disabled).toBe(false);
    await act(async () => { save().click(); });
    expect(onEdit.mock.calls[0]![0].args).toMatchObject({ path: "model/links/arm_1/material", value: { name: "Birch ply", density_kg_m3: 680, source: "maker data sheet" } });
  });

  it("shows the refusal and does not reload when the write is refused", async () => {
    const onEdit = vi.fn().mockResolvedValue({ written: false, refusal: "Reload first." });
    const onPayload = vi.fn();
    act(() => root.render(<Page payload={withMats()} contract="robot.yaml" expectedRevision="r1" onEdit={onEdit} onReopen={vi.fn()} onPayload={onPayload} />));
    const select = partLine("arm_1").querySelector("select")!;
    await act(async () => { select.value = "Aluminium 6061-T6"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(container.textContent).toContain("Reload first.");
    expect(container.textContent).not.toContain("Saved.");
    expect(onPayload).not.toHaveBeenCalled();
  });

  it("draws one centre-of-mass marker only for a complete centre of mass, and removes it", () => {
    const scene = new THREE.Scene();
    (mountedScene.ctx as unknown as { scene: THREE.Scene }).scene = scene;
    act(() => root.render(<Page payload={withForces(true)} />));
    const marker = scene.getObjectByName("centre-of-mass");
    expect(marker?.position.toArray()).toEqual([0.75, 0, 0.1]);
    act(() => root.render(<Page payload={withForces(false)} />));
    expect(scene.getObjectByName("centre-of-mass")).toBeUndefined();
    act(() => root.unmount());
    root = createRoot(container);
    delete (mountedScene.ctx as unknown as { scene?: THREE.Scene }).scene;
  });

  it("reads a joint's placement as roll, pitch, yaw in degrees beside its quaternion", () => {
    expect(placementWords([Math.PI / 2, 0, Math.PI / 2])).toEqual({ rpy: "roll 90°, pitch 0°, yaw 90°", quaternion: "x 0.5, y 0.5, z 0.5, w 0.5" });
    const skeleton = { scene: null, graph: null, links: [], joints: [{ name: "j", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Arm", position: [0, 0, 0], axis: [0, 0, 1], rpy: [0, 0, Math.PI / 2], lower: null, upper: null, effort: null }] } as unknown as CheckPayload;
    act(() => root.render(<Page payload={skeleton} />));
    act(() => row("Arm").click());
    expect(container.textContent).toContain("roll 0°, pitch 0°, yaw 90°");
  });
});

describe("Add part form beside the Parts tree (LIB-4)", () => {
  it("leaves one search box on screen while the form is open", () => {
    const add = { witness: "Maya", catalog: { rails: [], parts: [], stock: [{ id: "stock.plate", name: "Plate", dimensions: ["length"], mass_from: "material" as const, outline_only: false }], kit: [] }, onAdd: vi.fn(), onAddStructure: vi.fn() };
    act(() => root.render(<DesignParts payload={payload as never} add={add} />));
    const searches = () => container.querySelectorAll('input[type="search"]').length;
    expect(searches()).toBe(1); // the tree's own
    act(() => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Add part")!.click());
    expect(searches()).toBe(1); // the form's, the tree's is hidden
    expect(container.querySelector('[aria-label="search parts"]')).not.toBeNull();
  });
});
