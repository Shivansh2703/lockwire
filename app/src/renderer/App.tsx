import { useEffect, useMemo, useRef, useState } from "react";

import { explainOpenError } from "./openError";
import { pushRecent, readRecents, removeRecent } from "./recents";
import type { CheckFacePayload } from "./checkFacePayload";
import { DesignLens, DesignPanel, jointsOf, DesignParts, DesignSceneExtras, DesignTools, unplacedOf } from "./DesignPanel";
import { PLACED_SOURCE, type DesignEditReply, type DesignEditRequest } from "./DesignControls";
import type { AddPartSubmission, StructureSubmission } from "./AddPartForm";
import { AddPartForm, attachAfterDevice, structureFields } from "./AddPartForm";
import type { ShapeSubmission } from "./AddShapeForm";
import { sizeEditRequest } from "./NamedSizes";
import { DeclareFace, DeclareValues, type DeclareSubmitResult, type ReviewSubmission } from "./DeclareFace";
import { FailureBanner } from "./FailureBanner";
import { CheckPanel, CheckPending } from "./CheckPanel";
import { CompareWithDatasheet, MASS_NOMINAL, massLine } from "./MeasureCompareDeclare";
import { Button, EmptyState, Notice, Panel } from "./kit";
import { useDesignView } from "./scene/designView";
import { GraphView } from "./GraphView";
import { isolateIds } from "./graphLayout";
import { setSelectedGroup, setSelectedPartId, setSelectionSceneKey, useSelectedGroup, useSelectedPartId } from "./scene/selectionStore";
import { declarePathOf, type Jump } from "./jump";
import { RobotView } from "./scene/RobotView";
import { ViewBoundary } from "./ErrorBoundary";
import { dropEdit, placeRequest, type Prov } from "./scene/placeOnBody";
import type { SceneHit } from "./scene/buildScene";
import type { ContractWriteRequest } from "../shared/ipc-contract";
import { flagSummary } from "./FindingList";
import { headroomHighlights } from "./scene/buildScene";
import { declarePart, highlightsFromCards, highlightsFromFindings, partsOfPath, targetIds, checkHighlight, wholeHighlight, type Highlight } from "./scene/robotScene";
import { Shell } from "./Shell";
import { FindPart } from "./FindPart";
import { toScenePart } from "./DesignPanel";
import type { Mode } from "./Frame";
import "./PowerPanel"; // registers the Power side panel
import "./ControlsPanel"; // registers the Controls side panel
import "./WiringPanel"; // registers the Wiring side panel
import "./BomPanel"; // registers the Parts list side panel
import "./InterferencePanel"; // registers the Overlaps and clearance side panel
import { visiblePanels } from "./panelRegistry";
import { SideDrawer } from "./SideDrawer";
import { AssistantPanel, EMPTY_CHAT, type Chat } from "./AssistantPanel";
import { DocsPanel } from "./DocsPanel";
import { OpenFace } from "./OpenFace";
import { ShortcutsSheet } from "./ShortcutsSheet";
import { isHelpKey, onMac, screenKey, typing } from "./shortcuts";
import { EmptyGeometryPanel } from "./EmptyGeometryPanel";
import type { FileOpenResult, StartUrdfResult } from "../main/ipc";
import { PlanInput, planName } from "./PlanInput";
import { pro, type ProResult } from "./proRegistry";
import { useSidecarState } from "./useSidecarState";
import type { CheckPayload, DeclareFormPayload, DiffPayload, Finding, LogPayload, MeshGap, PagePayload, PlanPayload, ScenePayload, Starter } from "../shared/api.gen";

// The composition root (E2-j2, rebased onto e2-app-r2). Owns the mode Frame renders AND the
// one payload BOTH the Check and Design tabs read (one source, never two).
//
// The OpenFace -> CheckPanel IPC flow: same `View` state shape, same `onOpenLockfile`, same
// error branch, same `FailureBanner`.
//
// `window.__lockwireLoadCheckPayload` is the e2e-only test door (unit-level proofs that don't
// want to drive the real dialog/sidecar); the real app never reads it to decide what a person
// sees -- only a real `openLockfile()` reply ever sets `designPayload` on the production path.
type CheckState = { kind: "open"; error?: string } | { kind: "check"; payload: CheckFacePayload };
type OpenedDocument = {
  path: string;
  revision: string;
  page: PagePayload;
  declareForm: DeclareFormPayload;
};
type SelectedPlanFinding = {
  finding: Finding;
  revision: string;
};

declare global {
  interface Window {
    __lockwireLoadCheckPayload?: (payloadJson: string) => void;
  }
}

const PRO_MODES = pro?.modes ?? [];
const OWN_PANEL = new Set<Mode>(["Declare", "Plan", ...PRO_MODES.filter((m) => m.needsRobot).map((m) => m.id)]);

function UnavailableFace({ children }: { children: string }) {
  return <p className="named-placeholder">{children}</p>;
}

export function App() {
  const [mode, setMode] = useState<Mode>("Check");
  const [checkState, setCheckState] = useState<CheckState>({ kind: "open" });
  const [opened, setOpened] = useState<OpenedDocument | null>(null);
  // The check reply carries the 3D scene: drawn at once while the page (cards, Declare rows) is still being built.
  const [early, setEarly] = useState<{ path: string; scene: ScenePayload } | null>(null);
  const [chats, setChats] = useState<Record<string, Chat>>({}); // per robot, this app run; the panel itself unmounts when closed
  const [assistantOpen, setAssistantOpen] = useState(false); // closed by default; nothing runs until first opened
  const [recents, setRecents] = useState(readRecents);
  const [openError, setOpenError] = useState<string | null>(null);
  const [showKeys, setShowKeys] = useState(false); // the keyboard shortcuts sheet
  const [drawerTab, setDrawerTab] = useState("assistant");
  const [drawerWant, setDrawerWant] = useState<{ id: string } | undefined>();
  useEffect(() => { const open = () => { setDrawerWant({ id: "agent" }); setAssistantOpen(true); }; window.addEventListener("lockwire:open-agent", open); return () => window.removeEventListener("lockwire:open-agent", open); }, []); // Pro buttons that start the agent: show its tab
  const [startNote, setStartNote] = useState<string | null>(null);
  const [flatPath, setFlatPath] = useState<string | null>(null); // a Webots file has no 3D model: its lockfile's viewport stays empty
  const noView = flatPath !== null && flatPath === opened?.path; // tied to that robot, so any other open shows its view again
  const [savedBeside, setSavedBeside] = useState<string | null>(null); // the lockfile just written next to its model: a line in the verdict card
  const [meshGap, setMeshGap] = useState<MeshGap | null>(null);
  // The example just opened from the app's own folder: where it lives, and "Save a copy…".
  const [example, setExample] = useState<{ starter: string | null; dir: string } | null>(null);
  const [retryOpen, setRetryOpen] = useState<(() => Promise<void>) | null>(null);
  const [existingStart, setExistingStart] = useState<{ urdf: string; path: string; revision: string } | null>(null);
  const [designPreview, setDesignPreview] = useState<CheckPayload | null>(null);
  const [proHighlight, setProHighlight] = useState<Highlight[]>([]);
  const [history, setHistory] = useState<{ available: boolean; entries: LogPayload }>({ available: false, entries: [] });
  const [historyDiff, setHistoryDiff] = useState<DiffPayload | null>(null);
  const [selectedPlanFinding, setSelectedPlanFinding] = useState<SelectedPlanFinding | null>(null);
  const [planPayload, setPlanPayload] = useState<PlanPayload | null>(null);
  const [proResult, setProResult] = useState<ProResult | null>(null);
  const [checkDoc, setCheckDoc] = useState<CheckPayload | null>(null);
  const [selectedFinding, setSelectedFinding] = useState<number | null>(null);
  const [graphOn, setGraphOn] = useState(false); // GRAPH-1
  const [isolate, setIsolate] = useState(false);
  const [pickedPath, setPickedPath] = useState<string | null>(null);
  const [declareReview, setDeclareReview] = useState(false);
  const [findingSel, setFindingSel] = useState<number | null>(null);
  const [fitIds, setFitIds] = useState<string[]>([]);
  const [comparing, setComparing] = useState(false); // the Compare with datasheet panel (mockup 3B)
  const documentGeneration = useRef(0);
  const documentLoad = useRef(0);
  const shownLoad = useRef(0); // the newest load that has ended: the window shows its outcome (a robot, an error, or a cancel)
  const verdictRef = useRef<string | undefined>(undefined); // the verdict of the last opened check
  const openedPathRef = useRef<string | undefined>(undefined);
  const errorGeneration = useRef(0);
  const renderedGeneration = documentGeneration.current;
  const sidecarState = useSidecarState();
  const connected = sidecarState.phase === "ready";
  const [starters, setStarters] = useState<Starter[]>([]);
  useEffect(() => {
    if (connected) void window.lockwire.getStarters?.().then(setStarters, () => undefined);
  }, [connected]);

  // The engine says once when a save moved an older file to the anonymous-model format; show that line as it came.
  const [formatNote, setFormatNote] = useState<string | null>(null);
  async function writeContract(request: ContractWriteRequest) {
    const reply = await window.lockwire.writeContract(request);
    const lines = Reflect.get(Reflect.get(reply ?? {}, "payload") ?? {}, "lines");
    const moved = Array.isArray(lines) ? lines.find((l) => typeof l === "string" && l.startsWith("format moved to ")) : undefined;
    if (moved) setFormatNote(moved);
    return reply;
  }

  // APP-1: the one guard every late reply goes through. A write's reply acts only while the robot it was
  // for is still the one shown and no other open is under way; a reply for robot A never lands on robot B.
  // ponytail: an edit whose reply lands while a Re-check of the same robot runs skips its reload; that Re-check shows it.
  const settled = (load: number) => { if (load === documentLoad.current) shownLoad.current = load; };
  const isShown = (contract: string) => documentLoad.current === shownLoad.current && openedPathRef.current === contract;
  async function reloadShown(contract: string): Promise<boolean> {
    if (!isShown(contract)) return false;
    await onOpenLockfile(contract);
    return true;
  }

  // File > Close robot: back to the start card with its examples. A load still in flight is dropped.
  function closeRobot(): void {
    ++documentLoad.current; ++documentGeneration.current; ++errorGeneration.current;
    resetPerRobot(undefined);
    verdictRef.current = undefined;
    setOpened(null); setEarly(null); setCheckState({ kind: "open" }); setCheckDoc(null); setPlanPayload(null); setProResult(null);
    setExistingStart(null); setMeshGap(null); setDesignPreview(null); setOpenError(null); setStartNote(null); setRetryOpen(null);
    setMode("Check");
  }
  const closeRobotRef = useRef(closeRobot);
  closeRobotRef.current = closeRobot;
  const menuStep = useRef(reloadShown);
  menuStep.current = reloadShown; // the menu listener is bound once; this keeps it on the current robot

  async function onOpenLockfile(contract: string, retried = false): Promise<void> {
    const load = ++documentLoad.current; // ordered by when the open began, not by when its IPC returns
    setOpenError(null);
    setStartNote(null);
    setRetryOpen(() => () => onOpenLockfile(contract));
    try {
      const result = await window.lockwire.openLockfile({ contract });
      if (result.opened && "error" in result && result.error.startsWith("no contract at ")) {
        // A recent whose file is gone: drop it with one line, not an error whose Retry fails again.
        setRetryOpen(null);
        setStartNote(`${recents.find((r) => r.path === contract)?.name ?? "That robot"} is no longer there, so it was removed from Recent.`);
        setRecents(removeRecent(contract));
        return;
      }
      await loadOpened(result, load, retried);
    } catch (error) {
      ++errorGeneration.current;
      setOpenError(errorMessage(error));
    } finally { settled(load); }
  }

  // a different robot must not inherit the last one's diff, picked value, review or plan.
  function resetPerRobot(path: string | undefined): void {
    if (path === openedPathRef.current) return; // a re-check of the same robot keeps its place
    openedPathRef.current = path;
    pendingSelect.current = null;
    setExample(null);
    setHistoryDiff(null); setFindingSel(null); setPickedPath(null); setDeclareReview(false); setProHighlight([]); setComparing(false);
  }

  // Re-check after a swap, then tell the user what the engine said, and if the swap broke a GO.
  async function afterSwap(contract: string, lines: string[]): Promise<void> {
    const was = verdictRef.current;
    if (!await reloadShown(contract)) return;
    const worse = verdictRef.current === "NO-GO" && (was === "GO" || was === "GO_WITH_NOTES");
    setStartNote([...lines, ...(worse ? ["The robot is now NO-GO after this swap. Open the findings, or swap again and pick another servo."] : [])].join("\n") || null);
  }

  async function loadOpened(result: FileOpenResult, token?: number, retried = false): Promise<void> {
    if (!result.opened) return; // user cancelled the dialog -- stay on OpenFace
    const load = token ?? ++documentLoad.current;
    let moved = false;
    try { moved = await showOpened(result, load, retried); } finally { settled(load); }
    // The file changed while it was being checked (an edit during Re-check): check it again, once.
    if (moved && load === documentLoad.current) await onOpenLockfile(result.path, true);
  }
  /** True when the check ran on an older revision than the page and was not shown, so the caller re-runs it. */
  async function showOpened(result: FileOpenResult & { opened: true }, load: number, retried = false): Promise<boolean> {
    const errors = errorGeneration.current;
    if (load !== documentLoad.current) return false; // a newer open began meanwhile
    setRetryOpen(() => () => onOpenLockfile(result.path));
    if ("error" in result) {
      setRetryOpen(null); // the engine read the file and refused it: opening it again cannot help
      setOpenError(result.error);
      return false;
    }
    setSelectedPlanFinding(null);
    setSelectedFinding(null);
    setFitIds([]);
    if (result.path !== openedPathRef.current) { // another robot: never show the last one's verdict while this one's page builds
      setOpened(null); setCheckState({ kind: "open" }); setCheckDoc(null);
      const scene = result.checkPayload?.scene;
      setEarly(scene && "parts" in scene ? { path: result.path, scene } : null);
    }
    try {
      const [pagePayload, declareForm] = await Promise.all([
        window.lockwire.getContractPage({ contract: result.path }),
        window.lockwire.getDeclareForm({ contract: result.path }),
      ]);
      if (load !== documentLoad.current) return false;
      if (!pagePayload.revision) throw new Error("lockfile revision missing");
      const revision = pagePayload.revision;
      const checked = result.checkPayload.revision;
      const stale = typeof checked === "string" && checked !== revision; // the check read an older file than the page
      if (stale && !retried) return true;
      ++documentGeneration.current;
      setPlanPayload(null);
      setProResult(null);
      resetPerRobot(result.path);
      setOpened({ path: result.path, revision, page: pagePayload, declareForm });
      setEarly(null);
      setRecents(pushRecent({ path: result.path, name: pagePayload.robot }));
      setExistingStart((current) => current?.path === result.path ? { ...current, revision }
        : result.model ? { urdf: result.model, path: result.path, revision } : null); // opened from Recent: its model, from the lockfile
      void window.lockwire.getHistory({ contract: result.path }).then(
        (entries) => { if (openedPathRef.current === result.path) setHistory({ available: true, entries }); },
        () => { if (openedPathRef.current === result.path) setHistory({ available: false, entries: [] }); }, // keyed to the robot on screen, not the load counter: a later open that fails leaves this robot shown
      );
      setCheckDoc(result.checkPayload);
      verdictRef.current = result.payload.verdict;
      setCheckState({
        kind: "check",
        payload: {
          ...result.payload,
          qualifier: pagePayload.qualifier,
          cards: pagePayload.cards,
          accepted: pagePayload.accepted,
          adds_up: pagePayload.adds_up,
          requirements: pagePayload.requirements,
          cli_line: pagePayload.cli_line,
        },
      });
      if (errors === errorGeneration.current) { setOpenError(null); setRetryOpen(null); }
      // ponytail: one automatic re-run per open; a file still moving after it gets this line and one tap.
      if (stale) setStartNote("The robot changed while it was being checked. Press Re-check for the current result.");
    } catch (error) {
      if (load !== documentLoad.current) return false;
      // The check is still valid when the optional page read refuses. Faces whose
      // producer is absent stay explicit rather than receiving guessed data.
      ++documentGeneration.current;
      setPlanPayload(null);
      setProResult(null);
      resetPerRobot(undefined);
      setOpened(null);
      setEarly(null);
      setCheckDoc(result.checkPayload);
      setCheckState({ kind: "check", payload: result.payload });
      setOpenError(`Opened the check, but document refresh failed: ${errorMessage(error)}`);
    }
    return false;
  }

  // One door for every way a robot gets opened (button, File > Open Robot, drop).
  async function applyStart(result: StartUrdfResult, token?: number): Promise<void> {
    if (!result.started) {
      if (result.opened) { setExistingStart(null); setMeshGap(null); setMode("Check"); await loadOpened(result.opened, token); }
      return;
    }
    if (result.removed) setRecents(removeRecent(result.removed)); // the lockfile moved: one robot, one row
    const gap = result.start.mesh_gap ?? null;
    setMeshGap(gap); // the panel says it instead of the note's mesh line
    const note = result.start.note?.split("\n").filter((line) => !gap || !line.includes(gap.example)).join("\n");
    const folder = (path: string) => path.replace(/[\\/][^\\/]*$/, "");
    const beside = folder(result.start.path) === folder(result.urdf);
    if (result.start.written && beside) setSavedBeside(result.start.path);
    const saved = result.start.written && !beside && `Saved lockwire.yaml in the robot's package folder, ${folder(result.start.path)}.`;
    const flat = /\.(wbt|proto)$/i.test(result.urdf);
    setFlatPath(flat && result.opened.opened ? result.opened.path : null);
    setStartNote([saved, note, flat && "No 3D view for this file type."].filter(Boolean).join(" ") || null);
    setExistingStart(result.start.written ? null : {
      urdf: result.urdf, path: result.start.path, revision: result.start.revision,
    });
    setMode("Check");
    await loadOpened(result.opened, token);
    if (result.start.written && "error" in result.opened) {
      setOpenError(`Lockfile was written, but opening failed: ${result.opened.error}`);
    }
  }

  async function onStartFromUrdf(overwrite = false): Promise<void> {
    setOpenError(null);
    setFormatNote(null);
    setStartNote(null);
    setRetryOpen(() => () => onStartFromUrdf(overwrite));
    const load = ++documentLoad.current;
    try {
      await applyStart(await window.lockwire.startFromUrdf(overwrite && existingStart
        ? { urdf: existingStart.urdf, expected_revision: existingStart.revision } : undefined), load);
    } catch (error) {
      ++errorGeneration.current; // a still-running older load must not clear this
      setOpenError(errorMessage(error));
      if (overwrite && existingStart) setRetryOpen(() => () => onOpenLockfile(existingStart.path));
    } finally { settled(load); }
  }

  async function onChoosePackageFolder(): Promise<void> {
    setOpenError(null);
    const load = ++documentLoad.current;
    try {
      await applyStart(await window.lockwire.choosePackageFolder(), load);
    } catch (error) {
      ++errorGeneration.current;
      setOpenError(errorMessage(error));
    } finally { settled(load); }
  }

  async function onNewRobot(starter: string | null, copy?: boolean): Promise<boolean> {
    setOpenError(null);
    setFormatNote(null);
    setStartNote(null);
    setRetryOpen(() => async () => { await onNewRobot(starter, copy); });
    const load = ++documentLoad.current;
    try {
      const result = await window.lockwire.newRobot(starter, copy);
      await applyStart(result, load);
      if (!result.started && result.opened) setExample(result.example ? { starter, dir: result.example } : null);
      return !!result.opened;
    } catch (error) {
      ++errorGeneration.current;
      setOpenError(errorMessage(error));
      return false;
    } finally { settled(load); }
  }

  // The plan beside the lockfile loads itself, once per opened document, if none was chosen (main resolves the path,
  // and answers null with no sidecar run when there is none). A plan found is what puts Plan in the nav.
  const planTried = useRef(-1);
  const [autoPlanError, setAutoPlanError] = useState<string | null>(null);
  const [autoPlanLoaded, setAutoPlanLoaded] = useState(false);
  const openedPath = opened?.path;
  useEffect(() => {
    if (!openedPath || planPayload || planTried.current === documentGeneration.current) return;
    planTried.current = documentGeneration.current;
    const generation = documentGeneration.current;
    setAutoPlanError(null);
    void Promise.resolve().then(() => window.lockwire.checkPlan({ contract: openedPath, plan: "" })).then(
      (payload) => { if (payload && documentGeneration.current === generation) { setPlanPayload(payload); setAutoPlanLoaded(true); } },
      (error) => { if (documentGeneration.current === generation) setAutoPlanError(errorMessage(error)); },
    );
  }, [openedPath, planPayload]);

  useEffect(() => {
    window.__lockwireLoadCheckPayload = (payloadJson: string) => {
      setDesignPreview(JSON.parse(payloadJson));
    };
    return () => {
      delete window.__lockwireLoadCheckPayload;
    };
  }, []);

  useEffect(() => {
    const api = window.lockwire;
    if (!api) return;
    const off = api.onOpenedFromMenu?.((reply) => {
      if ("stepped" in reply) { void menuStep.current(reply.stepped); return; } // APP-3: Edit > Undo/Redo stays put and refreshes like a Design edit
      if ("error" in reply) { ++errorGeneration.current; setOpenError(reply.error); return; }
      void applyStart(reply.result).catch((error) => { ++errorGeneration.current; setOpenError(errorMessage(error)); });
    });
    const over = (e: DragEvent) => e.preventDefault();
    const drop = (e: DragEvent) => {
      e.preventDefault();
      const file = e.dataTransfer?.files[0];
      if (!file || !api.pathForFile) return;
      setOpenError(null);
      void api.openDroppedPath(api.pathForFile(file)).then(applyStart).catch((error) => { ++errorGeneration.current; setOpenError(errorMessage(error)); });
    };
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    const offKeys = api.onShowShortcuts?.(() => setShowKeys(true));
    const offClose = api.onCloseRobot?.(() => closeRobotRef.current());
    return () => { off?.(); offKeys?.(); offClose?.(); window.removeEventListener("dragover", over); window.removeEventListener("drop", drop); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // "?" opens the shortcuts sheet; Cmd/Ctrl+1/2/3 switch screen. Re-bound each render so it sees the current robot.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isHelpKey(e) && !typing(document.activeElement)) return setShowKeys(true);
      const next = screenKey(e, onMac());
      if (next && (opened || designPreview)) { e.preventDefault(); changeMode(next); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const payload = checkState.kind === "check" ? checkState.payload : null;
  const page = opened?.page ?? null;
  const sceneSource = page ?? designPreview; // the preview is the hidden e2e's injected payload
  const scenePayload = sceneSource?.scene && "parts" in sceneSource.scene ? sceneSource.scene : page ? undefined : early?.scene;
  const designPayload = opened?.page ?? designPreview;
  const { lens: designLens, hover: designHover } = useDesignView();
  const designGroup = useSelectedGroup();
  const designPart = useSelectedPartId();
  const sceneParts = useMemo(() => scenePayload?.parts.map(toScenePart) ?? [], [scenePayload]);
  // The one owner of "a new robot drops the old selection": the selection now outlives a tab.
  const sceneKey = `${opened?.path ?? early?.path ?? "preview"}:${scenePayload?.hash ?? "skeleton"}`;
  useEffect(() => setSelectionSceneKey(sceneKey), [sceneKey]);

  async function submitDeclaration(submission: {
    path: string;
    value: string;
    source: string;
    witness: string;
    formal?: boolean;
  }): Promise<DeclareSubmitResult> {
    if (!opened) return { written: false, refusal: "Open a lockfile first." };
    if (!opened.revision) return { written: false, refusal: "lockfile revision missing; reload before applying this edit" };
    const reply = await writeContract({
      command: "declare",
      args: { contract: opened.path, expected_revision: opened.revision, ...submission },
    });
    if (typeof reply !== "object" || reply === null) return { written: false, refusal: "Invalid write reply." };
    const payload = Reflect.get(reply, "payload");
    const refusal = Reflect.get(reply, "refusal");
    const revision = typeof payload === "object" && payload !== null ? Reflect.get(payload, "revision") : null;
    if (typeof revision === "string") setOpened((current) => current?.path === opened.path ? { ...current, revision } : current); // APP-1: never onto another robot
    const written = typeof payload === "object" && payload !== null && Reflect.get(payload, "written") === true;
    if (written) await reloadShown(opened.path); // APP-4: the verdict and the Declare row follow the new value
    return { written, refusal: typeof refusal === "string" ? refusal : null };
  }

  // The Power panel's efficiency entry: one declare, signed with the saved name (asked once if none); submitDeclaration reloads so the numbers redo.
  function declareFromPanel(path: string, value: string, source: string, w = opened?.declareForm.witness.prefill ?? ""): Promise<string | null> {
    return submitDeclaration({ path, value, source, witness: w }).then((r) => r.written ? null : r.refusal ?? "Not saved.");
  }

  // The Wiring panel's click-to-wire: one `add wire` with endpoints A then B, signed with the saved name, then reload so the diagram redraws.
  // ponytail: gauge and installation are fixed defaults (22 AWG, free air); a signal wire has no rail so the ampacity check ignores them.
  function addWireFromPanel(id: string, from: string, to: string, w = opened?.declareForm.witness.prefill ?? ""): Promise<string | null> {
    if (!opened?.revision) return Promise.resolve("Open a lockfile first.");
    const end = (key: string) => { const [device, connector, pin] = key.split("|"); return { device, ...(connector ? { connector } : {}), pin: /^\d+$/.test(pin ?? "") ? Number(pin) : pin }; };
    return writeContract({ command: "add", args: { contract: opened.path, expected_revision: opened.revision, kind: "wire", id, fields: { awg: 22, installation: "free_air", owner: "you", from: end(from), to: end(to) }, source: "drawn in the app", witness: w } }).then(async (reply) => {
      const payload = Reflect.get(reply ?? {}, "payload"), refusal = Reflect.get(reply ?? {}, "refusal");
      const written = !!payload && typeof payload === "object" && Reflect.get(payload, "written") === true;
      if (written) await reloadShown(opened.path);
      return written ? null : typeof refusal === "string" ? refusal : "Not saved.";
    }).catch(() => "The wire was not saved. Try again.");
  }

  async function submitReview(submission: ReviewSubmission): Promise<DeclareSubmitResult> {
    if (!opened) return { written: false, refusal: "Open a lockfile first." };
    if (!opened.revision) return { written: false, refusal: "lockfile revision missing; reload before applying this review" };
    const reply = await writeContract({
      command: "review",
      args: { contract: opened.path, expected_revision: opened.revision, ...submission },
    });
    if (typeof reply !== "object" || reply === null) return { written: false, refusal: "Invalid write reply." };
    const payload = Reflect.get(reply, "payload");
    const refusal = Reflect.get(reply, "refusal");
    const written = typeof payload === "object" && payload !== null && Reflect.get(payload, "written") === true;
    if (written) {
      const revision = Reflect.get(payload, "revision");
      if (typeof revision === "string") setOpened((current) => current?.path === opened.path ? { ...current, revision } : current); // APP-1: never onto another robot
    }
    return { written, refusal: typeof refusal === "string" ? refusal : null };
  }

  async function addPart(part: AddPartSubmission): Promise<DeclareSubmitResult> {
    if (!opened?.revision) return { written: false, refusal: "Open a lockfile first." };
    const slug = part.id.replace(/^dev\./, "");
    const write = async (revision: string, kind: string, id: string, fields: Record<string, unknown>, source: string) => {
      const reply = await writeContract({ command: "add", args: { contract: opened.path, expected_revision: revision, kind, id, fields, source, witness: part.witness } });
      const payload = Reflect.get(reply ?? {}, "payload");
      const refusal = Reflect.get(reply ?? {}, "refusal");
      const written = !!payload && typeof payload === "object" && Reflect.get(payload, "written") === true;
      const next = written ? Reflect.get(payload, "revision") : null;
      return { written, refusal: typeof refusal === "string" ? refusal : null, revision: typeof next === "string" ? next : revision };
    };
    let revision = opened.revision;
    if (part.newRail) {
      const made = await write(revision, "rail", part.newRail.id, { voltage: part.newRail.voltage, capacity: part.newRail.capacity }, "typed in the app");
      if (!made.written) return { written: false, refusal: made.refusal };
      revision = made.revision;
    }
    const done = await write(revision, "device", part.id, { family: `fam.${slug}`, model: `mdl.${slug}`, owner: "you", power_draw: `${part.draw} W`, power_rail: part.rail }, part.source);
    // CAD-05: an attached component is still a power device. Device first, then the attach; batch-edit takes no add,
    // so these are two writes, and a refused attach is undone step by step (the rail too when this add made it).
    if (done.written && part.attach) {
      const undo = async () => { const r = await window.lockwire.writeContract({ command: "undo-edit", args: { contract: opened.path } }); return Reflect.get(Reflect.get(r ?? {}, "payload") ?? {}, "undone") === true; };
      const refusal = await attachAfterDevice({ attach: { ...part.attach, witness: part.witness }, revision: done.revision, railMade: !!part.newRail, write, undo });
      if (refusal) { await reloadShown(opened.path); return { written: false, refusal }; }
    }
    if (done.written || part.newRail) await reloadShown(opened.path); // reload page, Declare rows and the check
    return { written: done.written, refusal: done.refusal, railKept: !done.written && !!part.newRail };
  }

  // Design's Add part: the same write, then the new part is the selected one (its Inspector opens)
  // as soon as the reloaded payload lists it; the reload's own selection reset must not undo it.
  const pendingSelect = useRef<string | null>(null);
  async function addAndSelect(part: AddPartSubmission): Promise<DeclareSubmitResult> {
    // No saved name: the form waits (busy) while the name prompt is up, then shows how the resumed add went.
    pendingSelect.current = part.id;
    const done = await addPart(part);
    if (!done.written) pendingSelect.current = null;
    return done;
  }
  useEffect(() => {
    const id = pendingSelect.current;
    if (!id) return;
    pendingSelect.current = null; // the reload has landed: found or not, the wish is spent
    if (sceneParts.some((p) => p.id === id) || unplacedOf(designPayload).some((d) => d.id === id)) setSelectedPartId(id);
  }, [sceneParts, designPayload]);

  // Design's Structure tab: a stock shape (add stock) or a kit part (add attach), hung on a link at the typed position.
  async function addStructure(s: StructureSubmission): Promise<DeclareSubmitResult> {
    if (!opened?.revision) return { written: false, refusal: "Open a lockfile first." };
    const fields = structureFields(s);
    const reply = await window.lockwire.writeContract({ command: "add", args: { contract: opened.path, expected_revision: opened.revision, kind: s.kind, id: s.name, fields, source: "picked in the app", witness: s.witness } });
    const payload = Reflect.get(reply ?? {}, "payload");
    const refusal = Reflect.get(reply ?? {}, "refusal");
    const written = !!payload && typeof payload === "object" && Reflect.get(payload, "written") === true;
    if (written) { pendingSelect.current = s.name; await reloadShown(opened.path); }
    return { written, refusal: written ? null : typeof refusal === "string" ? refusal : "Not saved." };
  }

  // Design's Add shape: one `add link` carrying the shape (sizes as numbers or named sizes), then, when
  // it hangs from a link, a fixed `add joint`; then the page reloads so the drawing and checks redo.
  async function addShape(shape: ShapeSubmission): Promise<DeclareSubmitResult> {
    if (!opened?.revision) return { written: false, refusal: "Open a lockfile first." };
    let revision = opened.revision;
    let written = false;
    const sizes = shape.newSizes.map((n) => ({ kind: "param", fields: { name: n.name, value: n.mm, unit: "mm" } }));
    const steps = [...sizes, { kind: "link", id: shape.name, fields: { name: shape.name, geometry: shape.geometry } }, ...(shape.attachTo ? [{ kind: "joint", id: `${shape.name}_joint`, fields: { name: `${shape.name}_joint`, type: "fixed", parent: shape.attachTo, child: shape.name } }] : [])];
    for (const step of steps) {
      const reply = await writeContract({ command: "add", args: { contract: opened.path, expected_revision: revision, ...step, source: "drawn in the app", witness: shape.witness } });
      const payload = Reflect.get(reply ?? {}, "payload");
      const refusal = Reflect.get(reply ?? {}, "refusal");
      if (!(!!payload && typeof payload === "object" && Reflect.get(payload, "written") === true)) {
        if (written) await reloadShown(opened.path);
        return { written: false, refusal: typeof refusal === "string" ? refusal : "Not saved." };
      }
      written = true;
      const next = Reflect.get(payload, "revision");
      if (typeof next === "string") revision = next;
    }
    await reloadShown(opened.path);
    return { written: true, refusal: null };
  }

  // Named sizes: change one number (in its own unit) through the declare path, then reload so the
  // drawing, the torque and every derived mass follow it.
  async function setNamedSize(name: string, value: number): Promise<DeclareSubmitResult> {
    if (!opened?.revision) return { written: false, refusal: "Open a lockfile first." };
    return setNamedSizeAs(name, value, opened.declareForm.witness.prefill);
  }
  async function setNamedSizeAs(name: string, value: number, witness: string): Promise<DeclareSubmitResult> {
    if (!opened?.revision) return { written: false, refusal: "Open a lockfile first." };
    const reply = await writeContract(sizeEditRequest(opened.path, opened.revision, name, value, witness));
    const payload = Reflect.get(reply ?? {}, "payload");
    const refusal = Reflect.get(reply ?? {}, "refusal");
    const written = !!payload && typeof payload === "object" && Reflect.get(payload, "written") === true;
    if (written) await reloadShown(opened.path);
    return { written, refusal: typeof refusal === "string" ? refusal : written ? null : "Not saved." };
  }

  // The robot's datasheet mass, written as the same `mass_total` budget `lockwire check --mass X
  // --source S --tolerance-pct 5` writes (start_operation.add_mass_check), then re-checked.
  async function compareMass(kg: string, source: string, name?: string): Promise<string | null> {
    const witness = name ?? opened?.declareForm.witness.prefill ?? "";
    if (!opened?.revision) return "Open a lockfile first.";
    const nominal = `${kg} kg`, limit = `${Number((Number(kg) * 1.05).toPrecision(6))} kg`;
    const send = async (request: ContractWriteRequest) => {
      const reply = await writeContract(request);
      const payload = Reflect.get(reply ?? {}, "payload"), refusal = Reflect.get(reply ?? {}, "refusal");
      const done = !!payload && typeof payload === "object" && Reflect.get(payload, "written") === true;
      return { done, revision: done ? String(Reflect.get(payload, "revision")) : "", refusal: typeof refusal === "string" ? refusal : "The comparison was refused." };
    };
    const prov = { contract: opened.path, source, witness };
    let sent = await send({ command: "add", args: { ...prov, expected_revision: opened.revision, kind: "budget", id: "bud.mass", fields: { budget_kind: "mass_total", sources: "links", nominal, tolerance_pct: 5, limit, owner: "you" } } });
    // A second compare re-declares the budget's two numbers (add refuses an existing id).
    if (!sent.done && sent.refusal.includes("already exists")) {
      sent = await send({ command: "declare", args: { ...prov, expected_revision: opened.revision, path: MASS_NOMINAL, value: nominal } });
      if (sent.done) sent = await send({ command: "declare", args: { ...prov, expected_revision: sent.revision, path: "budgets/bud.mass/limit", value: limit } });
    }
    if (!sent.done) return sent.refusal;
    await reloadShown(opened.path);
    return null;
  }

  async function chooseAndCheckPlan() {
    if (!opened) return null;
    setSelectedPlanFinding(null);
    const plan = await window.lockwire.choosePlan();
    return plan ? { path: plan, payload: (await window.lockwire.checkPlan({ contract: opened.path, plan }))! } : null;
  }

  function selectPlanFinding(finding: Finding) {
    if (!opened) return;
    setSelectedPlanFinding({ finding, revision: opened.revision });
    setMode("Reference");
  }

  function errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
  }

  async function loadHistoryDiff(a: string, b?: string): Promise<void> {
    if (!opened) return;
    const contract = opened.path;
    const diff = await window.lockwire.diffContract({ contract, a, ...(b ? { b } : {}) });
    if (isShown(contract)) setHistoryDiff(diff); // a reply for the robot we left is dropped
  }

  // The form never asks who: the saved name signs the edit; with none saved the edit records no name.
  function applyDesignEdit(request: DesignEditRequest, w = opened?.declareForm.witness.prefill ?? ""): Promise<DesignEditReply> {
    return writeDesignEdit(request.command === "undo-edit" || request.command === "redo-edit" ? request : { ...request, args: { ...request.args, witness: w } } as DesignEditRequest);
  }
  async function writeDesignEdit(request: DesignEditRequest): Promise<DesignEditReply> {
    const reply = await writeContract(request);
    if (!reply || typeof reply !== "object") return { written: false, refusal: "Invalid write reply." };
    const result = Reflect.get(reply, "payload");
    const refusal = Reflect.get(reply, "refusal");
    const stepped = request.command === "undo-edit" || request.command === "redo-edit"; // the journal step answers {undone} or {redone}
    const written = !!result && typeof result === "object" && (stepped ? Reflect.get(result, "undone") === true || Reflect.get(result, "redone") === true : Reflect.get(result, "written") === true);
    const contractShaError = result && typeof result === "object" ? Reflect.get(result, "contract_sha_error") : null;
    return {
      written,
      refusal: !written && typeof refusal === "string" ? refusal : null,
      diagnostic: written
        ? typeof contractShaError === "string" ? contractShaError : typeof refusal === "string" ? refusal : null
        : null,
    };
  }

  // Writes a device's mount_frame + mount_xyz through the same `place` edit Design uses, then
  // reloads so the check re-runs. Returns the refusal, or null when written.
  async function writePlace(request: ContractWriteRequest | null): Promise<string | null> {
    if (!request || !opened) return null;
    const reply = await writeContract(request);
    const result = Reflect.get(reply ?? {}, "payload");
    if (result && typeof result === "object" && Reflect.get(result, "written") === true) { await reloadShown(opened.path); return null; }
    const refusal = Reflect.get(reply ?? {}, "refusal");
    return typeof refusal === "string" ? refusal : "The placement was refused.";
  }
  const placeProv = (w = opened?.declareForm.witness.prefill ?? ""): Prov | null => opened
    ? { contract: opened.path, expected_revision: opened.revision, witness: w, source: PLACED_SOURCE }
    : null;
  function onDropPart(device: string, hit: SceneHit | null, w?: string): void {
    const el = document.activeElement as HTMLElement | null;
    if (!w && el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable)) return; // never write mid-typing
    const prov = placeProv(w);
    if (!prov) return;
    const mounts = Object.fromEntries((scenePayload?.parts ?? []).flatMap((p) => p.id && p.mount_frame ? [[p.id, p.mount_frame]] : []));
    const request = sceneSource ? dropEdit(device, hit, { joints: jointsOf(sceneSource), links: "links" in sceneSource ? sceneSource.links : [], mounts }, prov) : null;
    if (!request) return setOpenError("Drop on the robot's body, or use Place on… in the part panel.");
    void writePlace(request).then((refusal) => { if (refusal) setOpenError(refusal); }).catch((error) => setOpenError(errorMessage(error)));
  }

  const joints = page?.joints ?? [];
  const declareRows = opened?.declareForm.rows ?? [];
  const declareSel = declareRows.some((r) => r.path === pickedPath) ? pickedPath! : declareRows[0]?.path ?? "";
  const proMode = PRO_MODES.find((m) => m.id === mode);
  const resultSource = mode === "Plan" ? planPayload : proMode?.result ? proResult : null;
  const resultLabels = resultSource?.finding_labels;
  const resultFindings = resultSource?.findings;
  const sceneIds = useMemo(() => sceneParts.flatMap((p) => p.id ?? []), [sceneParts]);
  const planSteps = mode === "Plan" ? planPayload?.steps : undefined;
  const flagged = useMemo(
    () => mode === "Check" && payload ? highlightsFromCards(payload.cards, joints) : resultFindings ? highlightsFromFindings(resultFindings, joints, resultLabels) : [],
    [mode, payload, joints, resultFindings, resultLabels],
  );
  // Check lights every flagged part; elsewhere one picked row stays lit (the rest dims).
  const highlight = useMemo(() => {
    const all = sceneIds.length ? sceneIds : joints.map((j) => j.name);
    if (mode === "Check" && payload) {
      const card = selectedFinding !== null ? payload.cards[selectedFinding] : undefined;
      return checkHighlight(payload.cards, card, all, joints);
    }
    if (mode === "Design") return headroomHighlights(joints); // torque to spare, by ratio
    const sel = resultFindings && findingSel !== null ? resultFindings[findingSel] : undefined;
    const lit = sel
      ? resultLabels?.[findingSel!]?.whole ? wholeHighlight(sel.severity, all)
        : highlightsFromFindings([sel], joints, resultLabels?.[findingSel!] ? [resultLabels[findingSel!]!] : [])
      : flagged;
    // A plan with nothing flagged (GO) shows the parts its passed steps touch, as plain selection (the engine's step targets).
    return lit.length || !planSteps ? lit : [...new Set(planSteps.filter((st) => st.status === "PASS").flatMap((st) => (st.targets ?? []).flatMap((t) => targetIds(t, joints))))].map((id) => ({ id, level: "pass" as const }));
  }, [flagged, mode, payload, selectedFinding, findingSel, joints, resultFindings, resultLabels, planSteps, sceneIds]);
  const sceneSummary = useMemo(
    () => flagSummary(flagged.map((h) => h.id), (mode === "Check" ? payload?.cards.flatMap((c) => c.targets) : resultLabels?.flatMap((l) => l.targets)) ?? []),
    [flagged, mode, payload, resultLabels],
  );
  const selectedCard = mode === "Check" && payload && selectedFinding !== null ? payload.cards[selectedFinding] : undefined;
  const selectedResult = resultFindings && findingSel !== null ? resultFindings[findingSel] : undefined;
  // A part chosen anywhere (the robot, a chip, a sheet) wins in every tab; picking a row clears it.
  const storeIds = useMemo(() => designGroup?.members ?? (designPart ? [designPart] : []), [designGroup, designPart]);
  const rowParts = (path: string) => declarePart(declareRows, path, partsOfPath(path, sceneIds, joints), sceneIds);
  const selectedIds = useMemo(
    () => storeIds.length ? storeIds
      : selectedCard ? highlightsFromCards([selectedCard], joints).map((h) => h.id)
      : selectedResult ? highlightsFromFindings([selectedResult], joints, findingSel !== null && resultLabels?.[findingSel] ? [resultLabels[findingSel]] : []).map((h) => h.id)
      : mode === "Declare" ? rowParts(declareSel) : [],
    [storeIds, selectedCard, selectedResult, resultLabels, findingSel, mode, declareSel, opened, joints, sceneIds], // eslint-disable-line react-hooks/exhaustive-deps
  );
  // Names the sheets print (a group's label or id, a part id) -> the scene ids they light.
  const names = useMemo(() => {
    const out = new Map<string, string[]>();
    const add = (k: string, id: string) => out.set(k, [...new Set([...(out.get(k) ?? []), id])]);
    for (const p of scenePayload?.parts ?? []) { const id = p.id ?? p.node; add(p.group.id, id); add(p.group.label, id); add(id, id); }
    for (const j of joints) add(j.name, j.name);
    return out;
  }, [scenePayload, joints]);
  const nameSet = useMemo(() => new Set(names.keys()), [names]);
  // "Fit view" frames the whole robot until the selection changes; then the camera follows it.
  const selKey = selectedIds.join("\u0000");
  useEffect(() => setFitIds([]), [selKey]);
  const selectFinding = (i: number | null) => { setSelectedPartId(null); setSelectedFinding(i); };
  const selectResult = (i: number | null) => { setSelectedPartId(null); setFindingSel(i); };
  const [addingPart, setAddingPart] = useState(false);
  const pickDeclare = (path: string, review = false) => { setAddingPart(false); setSelectedPartId(null); setPickedPath(path); setDeclareReview(review); };
  /** The Declare row about a part: the current one when it already names it, else the first. */
  const declareRowFor = (id: string) => rowParts(declareSel).includes(id) ? declareSel : declareRows.find((r) => rowParts(r.path).includes(id))?.path;
  const showWhole = () => { setSelectedPartId(null); setSelectedFinding(null); setFindingSel(null); setFitIds(sceneIds.length ? sceneIds : joints.map((j) => j.name)); };

  /** The one jump every clickable name goes through. A declarable path opens its Declare
   * row; a finding index its Check card; otherwise the parts light here, and the same parts
   * clicked again open in Design. */
  // Ids the robot draws: a jump to anything else would light nothing, so it does not count.
  const drawn = useMemo(() => new Set([...sceneIds, ...joints.map((j) => j.name)]), [sceneIds, joints]);
  function partsOf(to: Jump): string[] {
    // A rail named without its loads borrows them from any card target for that rail.
    const known = (id: string) => payload?.cards.flatMap((c) => c.targets).find((t) => t.id === id && t.parts?.length);
    return [...new Set([
      ...(to.parts ?? []),
      ...(to.targets ?? []).flatMap((t) => targetIds(t.parts?.length ? t : known(t.id) ?? { ...t, parts: [t.id] }, joints)),
      ...(to.name ? names.get(to.name) ?? [] : []),
    ])].filter((id) => drawn.has(id));
  }
  function jump(to: Jump): void {
    const path = declarePathOf(to.path, declareRows);
    if (path && opened) { pickDeclare(path, !!to.review); setMode("Declare"); return; }
    if (to.findingIdx !== undefined) { selectFinding(to.findingIdx); setMode("Check"); return; }
    if (to.whole) { showWhole(); return; }
    if (to.declare) { const row = partsOf(to).map(declareRowFor).find(Boolean); if (row) pickDeclare(row); setMode("Declare"); return; }
    const parts = partsOf(to);
    if (!parts.length) return;
    const again = parts.length === storeIds.length && parts.every((id) => storeIds.includes(id));
    const group = to.targets?.length === 1 ? to.targets[0]!.id : to.name && names.has(to.name) ? scenePayload?.parts.find((p) => p.group.label === to.name || p.group.id === to.name)?.group.id ?? parts.join(" ") : parts.join(" ");
    if (parts.length === 1) setSelectedPartId(parts[0]!); else setSelectedGroup({ id: group, members: parts });
    if (to.design || (again && mode !== "Design")) setMode("Design");
  }
  const onJump = Object.assign(jump, {
    can: (to: Jump) => !!(opened && declarePathOf(to.path, declareRows)) || to.findingIdx !== undefined || !!to.whole || !!(to.declare && opened) || partsOf(to).length > 0,
  });
  const body =
          mode === "Check" ? (
            payload ? (
              <CheckPanel
                payload={payload}
                graph={page?.robot_graph}
                selected={selectedFinding}
                onSelect={selectFinding}
                onJump={onJump}
                onCompare={opened ? () => setComparing(true) : undefined}
                onRecheck={opened ? () => void onOpenLockfile(opened.path) : undefined}
                saved={opened && savedBeside === opened.path ? "saved beside your model" : undefined}
                contract={opened?.path}
                expectedRevision={opened?.revision}
                onSwapped={opened ? (lines) => void afterSwap(opened.path, lines) : undefined}
                pills={pro?.checkPills?.(page)}
                more={pro?.CheckMore ? <pro.CheckMore contract={opened?.path} expectedRevision={opened?.revision} check={checkDoc} /> : null}
                notice={meshGap && <EmptyGeometryPanel gap={meshGap} onChooseFolder={() => void onChoosePackageFolder()} onSkip={() => setMeshGap(null)} />}
              />
            ) : early ? <CheckPending /> : null
          ) : mode === "Design" ? (
            <DesignPanel
              payload={designPayload}
              contract={opened?.path}
              expectedRevision={opened?.revision}
              onEdit={opened ? applyDesignEdit : undefined}
              // APP-2: a Design edit refreshes through the same full reload as every other edit, so the verdict, findings,
              // check document, Declare rows and page all come from one open together. The reload sets them; onPayload has nothing left to do.
              onReopen={opened ? async (contract) => { await reloadShown(contract); return opened.page; } : undefined}
              onPayload={opened ? () => undefined : undefined}
              onJump={onJump}
              onSwapped={opened ? (lines) => { setMode("Check"); void afterSwap(opened.path, lines); } : undefined}
              onPlace={opened ? (device, frame) => { const prov = placeProv(); return prov ? writePlace(placeRequest(device, frame, [0, 0, 0], prov)) : Promise.resolve(null); } : undefined}
            />
          )
          : mode === "Plan" && opened ? (
            <PlanInput
              key={opened.path}
              onChoosePlan={chooseAndCheckPlan}
              payload={planPayload}
              robotName={page?.robot ?? payload?.robot}
              autoName={autoPlanLoaded ? (page?.robot ?? payload?.robot) && (page?.robot ?? payload!.robot).length <= 16 ? `${page?.robot ?? payload!.robot} plan` : planName(`${openedPath!.split(/[\\/]/).pop()!.replace(/\.[^.]*$/, "")}.yaml`) : undefined}
              autoError={autoPlanError}
              onPlan={(payload) => {
                if (documentGeneration.current === renderedGeneration) setPlanPayload(payload);
              }}
              onSelectFinding={selectPlanFinding}
              joints={joints}
              selected={findingSel}
              onSelect={selectResult}
              onJump={onJump}
            />
          )
          : mode === "Reference" && page ? (
            <DocsPanel
              page={page}
              selectedPlanFinding={selectedPlanFinding?.revision === opened?.revision ? selectedPlanFinding?.finding ?? null : null}
              history={history}
              contract={opened?.path}
              expectedRevision={opened?.revision}
              diff={historyDiff}
              onDiff={loadHistoryDiff}
              onApplied={async (contract) => { await reloadShown(contract); }}
              names={nameSet}
              onJump={onJump}
            />
          )
          : mode === "Declare" && opened && addingPart ? (
            <Panel title="Add part" actions={<Button variant="quiet" onClick={() => setAddingPart(false)}>Close</Button>}>
              <AddPartForm witness={opened.declareForm.witness.prefill} catalog={opened.declareForm.add_part} onAdd={addPart} />
            </Panel>
          )
          : mode === "Declare" && opened ? (
            <DeclareFace selectedPath={declareSel} contractPath={opened.path} expectedRevision={opened.revision} payload={opened.declareForm} onSubmit={submitDeclaration} onReview={submitReview}
              reviewOpen={declareReview} onShowPart={rowParts(declareSel).length ? () => onJump({ parts: rowParts(declareSel), design: true }) : undefined} />
          )
          : mode === "Declare" || mode === "Plan" || (proMode?.needsRobot && !opened) ? (
            <EmptyState title="No robot open" action={{ label: "Open robot", onClick: () => { setMode("Check"); void onStartFromUrdf(); } }} />
          )
          : proMode ? null
          : <UnavailableFace>No document was returned for this mode.</UnavailableFace>

  function changeMode(next: Mode): void {
    setFindingSel(null);
    // A part chosen elsewhere opens on its own Declare row.
    const row = next === "Declare" && storeIds[0] ? declareRowFor(storeIds[0]) : undefined;
    if (row) pickDeclare(row);
    setMode(next);
  }

  const pickPart = (id: string | null) => {
    // Check: a pick opens the card about that part. Declare: it picks that part's row
    // (the current one when it already names the part). Elsewhere the pick stays lit.
    if (!id) return;
    if (mode === "Check" && payload) {
      const hit = payload.cards.findIndex((c) => highlightsFromCards([c], joints).some((h) => h.id === id));
      if (hit >= 0) selectFinding(hit);
    } else if (mode === "Declare") {
      const row = declareRowFor(id);
      if (row) pickDeclare(row);
    }
  };
  // GRAPH-1: the chosen check's graph nodes come from the payload; isolating hides the rest in 3D and in the graph.
  const involveCard = payload && selectedFinding !== null ? payload.cards[selectedFinding] : undefined;
  const robotGraph = page?.robot_graph ?? undefined;
  const involves = involveCard?.involves ?? [];
  const isoIds = isolate && involves.length ? isolateIds(involves) : null;
  const label = mode === "Reference" ? "Docs" : mode;
  // The mass budget's result: the datasheet comparison, else why the sum was not scored (massless links).
  const massCard = payload?.cards.find((c) => c.quantity === MASS_NOMINAL) ?? payload?.cards.find((c) => c.quantity === "budgets/bud.mass" && c.severity === "not_scored");
  const panelTabs = visiblePanels(opened?.page);
  const onPanelTab = panelTabs.some((p) => p.id === drawerTab);
  return (
    <>
    <Shell
      robotName={page?.robot ?? payload?.robot}
      onRobot={payload && (mode !== "Design" || selectedIds.length) ? () => { showWhole(); setMode("Design"); } : undefined}
      mode={mode}
      wide={mode === "Reference" && !(assistantOpen && opened)}
      assistant={{ open: assistantOpen && !!opened && !onPanelTab, onToggle: opened ? () => { if (assistantOpen && !onPanelTab) setAssistantOpen(false); else { setDrawerWant({ id: "@assist" }); setAssistantOpen(true); } } : undefined }}
      panels={opened && panelTabs.length ? { open: assistantOpen && onPanelTab, onToggle: () => { if (assistantOpen && onPanelTab) setAssistantOpen(false); else { setDrawerWant({ id: "@panels" }); setAssistantOpen(true); } } } : undefined}
      drawer={<SideDrawer open={assistantOpen && !!opened} onClose={() => setAssistantOpen(false)} cwd={assistantOpen ? opened?.path.replace(/[\\/][^\\/]*$/, "") : undefined} contract={opened?.path} revision={opened?.revision} page={opened?.page} onDeclare={declareFromPanel} onAddWire={addWireFromPanel} want={drawerWant} onTab={setDrawerTab}
        onReview={(p) => opened && setChats((m) => { const c = m[opened.path] ?? EMPTY_CHAT; return c.cards.some((x) => x.id === p.id) ? m : { ...m, [opened.path]: { ...c, cards: [...c.cards, p] } }; })}
        assistant={assistantOpen && opened && (
        <AssistantPanel contract={opened.path} revision={opened.revision} mode={mode} witness={opened.declareForm.witness.prefill} chat={chats[opened.path] ?? EMPTY_CHAT} onChat={(f) => setChats((m) => ({ ...m, [opened.path]: f(m[opened.path] ?? EMPTY_CHAT) }))} onApplied={async (contract) => { await reloadShown(contract); }} />
      )} />}
      onModeChange={changeMode}
      connected={connected}
      sceneSummary={sceneSummary}
      locked={!payload && !designPreview && !early}
      noView={noView}
      onOverwrite={existingStart && mode === "Check" && page?.model_changed ? () => void onStartFromUrdf(true) : undefined}
      onFit={page && joints.length ? showWhole : undefined}
      graph={payload && robotGraph ? { on: graphOn, onToggle: () => setGraphOn(!graphOn), isolate: involves.length ? { on: isolate, onToggle: () => setIsolate(!isolate) } : undefined } : undefined}
      scene={(viewInset) => graphOn && robotGraph && payload ? (
        <GraphView graph={robotGraph} cards={payload.cards} selected={selectedFinding} onSelectCard={selectFinding} isolate={isolate} selectedPart={designPart} onPick={(id) => { setSelectedPartId(id); pickPart(id); }} inset={viewInset} />
      ) : (
        <ViewBoundary><RobotView
          viewInset={viewInset}
          sceneKey={`${opened?.path ?? early?.path}:${scenePayload?.hash ?? "skeleton"}`}
          parts={noView ? [] : sceneParts}
          joints={sceneSource && !noView ? jointsOf(sceneSource) : []}
          links={sceneSource && !noView && "links" in sceneSource ? sceneSource.links : []}
          highlight={proMode?.highlights ? proHighlight : highlight}
          selected={mode === "Design" && designHover ? designHover : mode === "Run" && proHighlight.length ? proHighlight.map((h) => h.id) : selectedIds.length > 1 ? selectedIds : selectedIds[0] ?? null}
          dim={mode === "Reference" && !selectedIds.length}
          lens={mode === "Design" ? designLens : "off"}
          focus={fitIds.length ? fitIds : selectedIds}
          onDropPart={onDropPart}
          onPick={pickPart}
          isolate={isoIds}
        >
          {mode === "Design" && <DesignSceneExtras payload={sceneSource} />}
        </RobotView></ViewBoundary>
      )}
      notices={
        <>
          <FailureBanner />
          {openError && (() => { const f = explainOpenError(openError); return <div className="open-face-error">
            <Notice kind="error" action={retryOpen && f.retry ? { label: "Retry opening", onClick: () => void retryOpen() } : { label: "Open a different robot", onClick: () => void onStartFromUrdf() }}>
              {f.line}{f.raw && <details><summary>Details</summary>{f.raw}</details>}
            </Notice>
          </div>; })()}
          {formatNote && <div className="start-note"><Notice kind="info">{formatNote}</Notice></div>}
          {startNote && <div className="start-note"><Notice kind="info">{startNote}</Notice></div>}
          {example && <div className="start-note"><Notice kind="info" action={{ label: example.starter === null ? "Save…" : "Save a copy…", onClick: () => void onNewRobot(example.starter, true) }}>{example.starter === null ? "Not saved yet. Save it to keep your work." : "This is an example. Save a copy to keep your changes."}</Notice></div>}
        </>
      }
      left={mode === "Design" ? <DesignParts payload={designPayload} add={opened ? { witness: opened.declareForm.witness.prefill, catalog: opened.declareForm.add_part, onAdd: addAndSelect, onAddStructure: addStructure, shape: { links: sceneSource && "links" in sceneSource ? sceneSource.links : [], params: sceneSource && "params" in sceneSource ? sceneSource.params ?? [] : [], onAdd: addShape, onSize: setNamedSize } } : undefined} />
        : mode === "Check" && comparing && opened ? <CompareWithDatasheet result={massCard ? massLine(massCard) ?? massCard.title : null} onCompare={compareMass} />
        : mode === "Declare" && opened ? <DeclareValues payload={opened.declareForm} selectedPath={declareSel} onSelect={(path) => pickDeclare(path)} onAddPart={() => setAddingPart(true)} /> : undefined}
      above={mode === "Design" ? <DesignLens payload={designPayload} /> : undefined}
      tools={mode === "Design" ? <DesignTools payload={designPayload} canAnalyse={!!(opened?.path && opened.revision)} /> : undefined}
      center={mode === "Check" && !payload && !early ? <OpenFace connected={!((sidecarState.phase === "starting" || sidecarState.phase === "error") && window.lockwire?.getSidecarState)} engineError={sidecarState.phase === "error" ? sidecarState.message : undefined} recent={recents} onOpenRobot={() => void onStartFromUrdf()} starters={starters} onNewRobot={(s) => void onNewRobot(s)} onAgent={() => void onNewRobot(null).then((ok) => { if (ok) window.dispatchEvent(new Event("lockwire:open-agent")); })} onOpenRecent={(p) => void onOpenLockfile(p)} /> : undefined}
    >
      {mode === "Check" || mode === "Design" || mode === "Reference" || !body || (OWN_PANEL.has(mode) && opened) ? body : (
        <Panel title={label}>{body}</Panel>
      )}
      {pro && (
        <pro.Pages
          mode={mode}
          contract={opened?.path}
          joints={joints}
          sceneIds={sceneIds}
          result={proResult}
          onResult={(result) => { if (documentGeneration.current === renderedGeneration) setProResult(result); }}
          selected={findingSel}
          onSelect={selectResult}
          onHighlight={setProHighlight}
        />
      )}
    </Shell>
    {showKeys && <ShortcutsSheet onClose={() => setShowKeys(false)} />}
    <FindPart page={page} onPick={(id) => { const again = storeIds.length === 1 && storeIds[0] === id; setSelectedPartId(id); setFitIds((f) => again && drawn.has(id) ? (f.length ? [] : [id, id]) : []); pickPart(id); }} />
    </>
  );
}
