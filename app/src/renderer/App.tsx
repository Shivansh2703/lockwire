import { useEffect, useMemo, useRef, useState } from "react";

import { pushRecent, readRecents } from "./recents";
import type { CheckFacePayload } from "./checkFacePayload";
import { DesignLens, DesignPanel, jointsOf, DesignParts, DesignSceneExtras, DesignTools } from "./DesignPanel";
import type { DesignEditReply, DesignEditRequest } from "./DesignControls";
import type { AddPartSubmission } from "./AddPartForm";
import { AddPartForm } from "./AddPartForm";
import { DeclareFace, DeclareValues, type DeclareSubmitResult, type ReviewSubmission } from "./DeclareFace";
import { FailureBanner } from "./FailureBanner";
import { CheckPanel } from "./CheckPanel";
import { Button, EmptyState, Notice, Panel } from "./kit";
import { useDesignView } from "./scene/designView";
import { setSelectedGroup, setSelectedPartId, setSelectionSceneKey, useSelectedGroup, useSelectedPartId } from "./scene/selectionStore";
import { declarePathOf, type Jump } from "./jump";
import { RobotView } from "./scene/RobotView";
import { dropEdit, placeRequest, type Prov } from "./scene/placeOnBody";
import type { SceneHit } from "./scene/buildScene";
import type { ContractWriteRequest } from "../shared/ipc-contract";
import { flagSummary } from "./FindingList";
import { declarePart, highlightsFromCards, highlightsFromFindings, partsOfPath, targetIds, topHighlight, wholeHighlight, type Highlight } from "./scene/robotScene";
import { Shell } from "./Shell";
import { toScenePart } from "./DesignPanel";
import type { Mode } from "./Frame";
import { DocsPanel } from "./DocsPanel";
import { OpenFace } from "./OpenFace";
import type { FileOpenResult, StartUrdfResult } from "../main/ipc";
import { PlanInput, planName } from "./PlanInput";
import { pro, type ProResult } from "./proRegistry";
import { useSidecarState } from "./useSidecarState";
import type { CheckPayload, DeclareFormPayload, DiffPayload, Finding, LogPayload, PagePayload, PlanPayload, Starter } from "../shared/api.gen";

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
  const [recents, setRecents] = useState(readRecents);
  const [openError, setOpenError] = useState<string | null>(null);
  const [startNote, setStartNote] = useState<string | null>(null);
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
  const [pickedPath, setPickedPath] = useState<string | null>(null);
  const [declareReview, setDeclareReview] = useState(false);
  const [findingSel, setFindingSel] = useState<number | null>(null);
  const [fitIds, setFitIds] = useState<string[]>([]);
  const documentGeneration = useRef(0);
  const documentLoad = useRef(0);
  const errorGeneration = useRef(0);
  const renderedGeneration = documentGeneration.current;
  const sidecarState = useSidecarState();
  const connected = sidecarState.phase === "ready";
  const [starters, setStarters] = useState<Starter[]>([]);
  useEffect(() => {
    if (connected) void window.lockwire.getStarters?.().then(setStarters, () => undefined);
  }, [connected]);

  async function onOpenLockfile(contract: string): Promise<void> {
    setOpenError(null);
    setStartNote(null);
    setRetryOpen(() => () => onOpenLockfile(contract));
    try {
      await loadOpened(await window.lockwire.openLockfile({ contract }));
    } catch (error) {
      ++errorGeneration.current;
      setOpenError(errorMessage(error));
    }
  }

  async function loadOpened(result: FileOpenResult): Promise<void> {
    if (!result.opened) return; // user cancelled the dialog -- stay on OpenFace
    const load = ++documentLoad.current;
    const errors = errorGeneration.current;
    setRetryOpen(() => () => onOpenLockfile(result.path));
    if ("error" in result) {
      setOpenError(result.error);
      return;
    }
    setSelectedPlanFinding(null);
    setSelectedFinding(null);
    setFitIds([]);
    try {
      const [pagePayload, declareForm] = await Promise.all([
        window.lockwire.getContractPage({ contract: result.path }),
        window.lockwire.getDeclareForm({ contract: result.path }),
      ]);
      if (load !== documentLoad.current) return;
      if (!pagePayload.revision) throw new Error("lockfile revision missing");
      const revision = pagePayload.revision;
      ++documentGeneration.current;
      setPlanPayload(null);
      setProResult(null);
      setOpened({ path: result.path, revision, page: pagePayload, declareForm });
      setRecents(pushRecent({ path: result.path, name: pagePayload.robot }));
      setExistingStart((current) => current?.path === result.path ? { ...current, revision } : current);
      void window.lockwire.getHistory({ contract: result.path }).then(
        (entries) => setHistory({ available: true, entries }),
        () => setHistory({ available: false, entries: [] }),
      );
      setCheckDoc(result.checkPayload);
      setCheckState({
        kind: "check",
        payload: {
          ...result.payload,
          qualifier: pagePayload.qualifier,
          cards: pagePayload.cards,
          cli_line: pagePayload.cli_line,
        },
      });
      if (errors === errorGeneration.current) { setOpenError(null); setRetryOpen(null); }
    } catch (error) {
      if (load !== documentLoad.current) return;
      // The check is still valid when the optional page read refuses. Faces whose
      // producer is absent stay explicit rather than receiving guessed data.
      ++documentGeneration.current;
      setPlanPayload(null);
      setProResult(null);
      setOpened(null);
      setCheckDoc(result.checkPayload);
      setCheckState({ kind: "check", payload: result.payload });
      setOpenError(`Opened the check, but document refresh failed: ${errorMessage(error)}`);
    }
  }

  // One door for every way a robot gets opened (button, File > Open Robot, drop).
  async function applyStart(result: StartUrdfResult): Promise<void> {
    if (!result.started) {
      if (result.opened) { setExistingStart(null); setMode("Check"); await loadOpened(result.opened); }
      return;
    }
    setStartNote(result.start.note ?? null);
    setExistingStart(result.start.written ? null : {
      urdf: result.urdf, path: result.start.path, revision: result.start.revision,
    });
    setMode("Check");
    await loadOpened(result.opened);
    if (result.start.written && "error" in result.opened) {
      setOpenError(`Lockfile was written, but opening failed: ${result.opened.error}`);
    }
  }

  async function onStartFromUrdf(overwrite = false): Promise<void> {
    setOpenError(null);
    setStartNote(null);
    setRetryOpen(() => () => onStartFromUrdf(overwrite));
    try {
      await applyStart(await window.lockwire.startFromUrdf(overwrite && existingStart
        ? { urdf: existingStart.urdf, expected_revision: existingStart.revision } : undefined));
    } catch (error) {
      ++errorGeneration.current; // a still-running older load must not clear this
      setOpenError(errorMessage(error));
      if (overwrite && existingStart) setRetryOpen(() => () => onOpenLockfile(existingStart.path));
    }
  }

  async function onNewRobot(starter: string | null): Promise<void> {
    setOpenError(null);
    setStartNote(null);
    setRetryOpen(() => () => onNewRobot(starter));
    try {
      await applyStart(await window.lockwire.newRobot(starter));
    } catch (error) {
      ++errorGeneration.current;
      setOpenError(errorMessage(error));
    }
  }

  // The plan beside the lockfile loads itself, once per opened document, if none was chosen (main resolves the path).
  const planTried = useRef(-1);
  const [autoPlanError, setAutoPlanError] = useState<string | null>(null);
  const [autoPlanLoaded, setAutoPlanLoaded] = useState(false);
  const openedPath = opened?.path;
  useEffect(() => {
    if (mode !== "Plan" || !openedPath || planPayload || planTried.current === documentGeneration.current) return;
    planTried.current = documentGeneration.current;
    const generation = documentGeneration.current;
    setAutoPlanError(null);
    void Promise.resolve().then(() => window.lockwire.checkPlan({ contract: openedPath, plan: "" })).then(
      (payload) => { if (payload && documentGeneration.current === generation) { setPlanPayload(payload); setAutoPlanLoaded(true); } },
      (error) => { if (documentGeneration.current === generation) setAutoPlanError(errorMessage(error)); },
    );
  }, [mode, openedPath, planPayload]);

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
    return () => { off?.(); window.removeEventListener("dragover", over); window.removeEventListener("drop", drop); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const payload = checkState.kind === "check" ? checkState.payload : null;
  const page = opened?.page ?? null;
  const sceneSource = page ?? designPreview; // the preview is the hidden e2e's injected payload
  const scenePayload = sceneSource?.scene && "parts" in sceneSource.scene ? sceneSource.scene : undefined;
  const designPayload = opened?.page ?? designPreview;
  const { lens: designLens, hover: designHover } = useDesignView();
  const designGroup = useSelectedGroup();
  const designPart = useSelectedPartId();
  const sceneParts = useMemo(() => scenePayload?.parts.map(toScenePart) ?? [], [scenePayload]);
  // The one owner of "a new robot drops the old selection": the selection now outlives a tab.
  const sceneKey = `${opened?.path ?? "preview"}:${scenePayload?.hash ?? "skeleton"}`;
  useEffect(() => setSelectionSceneKey(sceneKey), [sceneKey]);

  async function submitDeclaration(submission: {
    path: string;
    value: string;
    source: string;
    witness: string;
  }): Promise<DeclareSubmitResult> {
    if (!opened) return { written: false, refusal: "Open a lockfile first." };
    if (!opened.revision) return { written: false, refusal: "lockfile revision missing; reload before applying this edit" };
    const reply = await window.lockwire.writeContract({
      command: "declare",
      args: { contract: opened.path, expected_revision: opened.revision, ...submission },
    });
    if (typeof reply !== "object" || reply === null) return { written: false, refusal: "Invalid write reply." };
    const payload = Reflect.get(reply, "payload");
    const refusal = Reflect.get(reply, "refusal");
    const revision = typeof payload === "object" && payload !== null ? Reflect.get(payload, "revision") : null;
    if (typeof revision === "string") setOpened((current) => current ? { ...current, revision } : current);
    return {
      written: typeof payload === "object" && payload !== null && Reflect.get(payload, "written") === true,
      refusal: typeof refusal === "string" ? refusal : null,
    };
  }

  async function submitReview(submission: ReviewSubmission): Promise<DeclareSubmitResult> {
    if (!opened) return { written: false, refusal: "Open a lockfile first." };
    if (!opened.revision) return { written: false, refusal: "lockfile revision missing; reload before applying this review" };
    const reply = await window.lockwire.writeContract({
      command: "review",
      args: { contract: opened.path, expected_revision: opened.revision, ...submission },
    });
    if (typeof reply !== "object" || reply === null) return { written: false, refusal: "Invalid write reply." };
    const payload = Reflect.get(reply, "payload");
    const refusal = Reflect.get(reply, "refusal");
    const written = typeof payload === "object" && payload !== null && Reflect.get(payload, "written") === true;
    if (written) {
      const revision = Reflect.get(payload, "revision");
      if (typeof revision === "string") setOpened((current) => current ? { ...current, revision } : current);
    }
    return { written, refusal: typeof refusal === "string" ? refusal : null };
  }

  async function addPart(part: AddPartSubmission): Promise<DeclareSubmitResult> {
    if (!opened?.revision) return { written: false, refusal: "Open a lockfile first." };
    const slug = part.id.replace(/^dev\./, "");
    const reply = await window.lockwire.writeContract({
      command: "add",
      args: {
        contract: opened.path, expected_revision: opened.revision, kind: "device", id: part.id,
        fields: { family: `fam.${slug}`, model: `mdl.${slug}`, owner: "you", power_draw: `${part.draw} W`, power_rail: part.rail },
        source: part.source, witness: part.witness,
      },
    });
    const payload = Reflect.get(reply ?? {}, "payload");
    const refusal = Reflect.get(reply ?? {}, "refusal");
    const written = !!payload && typeof payload === "object" && Reflect.get(payload, "written") === true;
    if (written) await onOpenLockfile(opened.path); // reload page, Declare rows and the check
    return { written, refusal: typeof refusal === "string" ? refusal : null };
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
    setHistoryDiff(await window.lockwire.diffContract({ contract: opened.path, a, ...(b ? { b } : {}) }));
  }

  async function applyDesignEdit(request: DesignEditRequest): Promise<DesignEditReply> {
    const reply = await window.lockwire.writeContract(request);
    if (!reply || typeof reply !== "object") return { written: false, refusal: "Invalid write reply." };
    const result = Reflect.get(reply, "payload");
    const refusal = Reflect.get(reply, "refusal");
    const written = !!result && typeof result === "object" && Reflect.get(result, "written") === true;
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
    const reply = await window.lockwire.writeContract(request);
    const result = Reflect.get(reply ?? {}, "payload");
    if (result && typeof result === "object" && Reflect.get(result, "written") === true) { await onOpenLockfile(opened.path); return null; }
    const refusal = Reflect.get(reply ?? {}, "refusal");
    return typeof refusal === "string" ? refusal : "The placement was refused.";
  }
  const placeProv = (): Prov | null => opened && opened.declareForm.witness.prefill
    ? { contract: opened.path, expected_revision: opened.revision, witness: opened.declareForm.witness.prefill, source: "placed in the app" }
    : null;
  function onDropPart(device: string, hit: SceneHit | null): void {
    const el = document.activeElement as HTMLElement | null;
    if (el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable)) return; // never write mid-typing
    const prov = placeProv();
    if (!prov) return setOpenError("Set your witness name in Declare first.");
    const mounts = Object.fromEntries((scenePayload?.parts ?? []).flatMap((p) => p.id && p.mount_frame ? [[p.id, p.mount_frame]] : []));
    const request = sceneSource ? dropEdit(device, hit, { joints: jointsOf(sceneSource), links: "links" in sceneSource ? sceneSource.links : [], mounts }, prov) : null;
    if (!request) return setOpenError("Drop on the robot's body, or use Place on… in the part panel.");
    void writePlace(request).then((refusal) => { if (refusal) setOpenError(refusal); }).catch((error) => setOpenError(errorMessage(error)));
  }

  function replaceOpenedPage(nextPage: CheckPayload | PagePayload): void {
    if (!("faces" in nextPage)) return;
    setOpened((current) => current ? { ...current, revision: nextPage.revision ?? current.revision, page: nextPage } : current);
    setCheckState((current) => current.kind === "check" ? {
      kind: "check",
      payload: {
        ...current.payload,
        robot: nextPage.robot,
        headline: nextPage.headline,
        qualifier: nextPage.qualifier,
        scored: nextPage.scored,
        violations: nextPage.violations,
        warnings: nextPage.warnings,
        unchecked: nextPage.unchecked,
        cards: nextPage.cards,
        cli_line: nextPage.cli_line,
      },
    } : current);
  }

  async function reopenAfterUndo(contract: string): Promise<void> {
    setOpened((current) => current ? { ...current, revision: "" } : current);
    const nextPage = await window.lockwire.getContractPage({ contract });
    if (!nextPage.revision) throw new Error("lockfile revision missing");
    replaceOpenedPage(nextPage);
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
  // One row or card selected: only it stays lit (the rest of the robot dims), instead of every finding at once.
  const highlight = useMemo(() => {
    const all = sceneIds.length ? sceneIds : joints.map((j) => j.name);
    if (mode === "Check" && payload) {
      const card = selectedFinding !== null ? payload.cards[selectedFinding] : undefined;
      return card ? (card.whole ? wholeHighlight(card.severity, all) : highlightsFromCards([card], joints)) : topHighlight(payload.cards, joints);
    }
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
                selected={selectedFinding}
                onSelect={selectFinding}
                onJump={onJump}
                onRecheck={opened ? () => void onOpenLockfile(opened.path) : undefined}
                pills={pro?.checkPills?.(page)}
                more={pro?.CheckMore ? <pro.CheckMore contract={opened?.path} expectedRevision={opened?.revision} check={checkDoc} /> : null}
              />
            ) : null
          ) : mode === "Design" ? (
            <DesignPanel
              payload={designPayload}
              contract={opened?.path}
              expectedRevision={opened?.revision}
              onEdit={opened ? applyDesignEdit : undefined}
              onReopen={opened ? (contract) => window.lockwire.getContractPage({ contract }) : undefined}
              onPayload={opened ? replaceOpenedPage : undefined}
              onJump={onJump}
              onPlace={opened ? (device, frame) => { const prov = placeProv(); return prov ? writePlace(placeRequest(device, frame, [0, 0, 0], prov)) : Promise.resolve("Set your witness name in Declare first."); } : undefined}
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
              onApplied={reopenAfterUndo}
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

  const label = mode === "Reference" ? "Docs" : mode;
  return (
    <Shell
      robotName={page?.robot ?? payload?.robot}
      onRobot={payload && (mode !== "Design" || selectedIds.length) ? () => { showWhole(); setMode("Design"); } : undefined}
      mode={mode}
      wide={mode === "Reference"}
      onModeChange={(next) => {
        setFindingSel(null);
        // A part chosen elsewhere opens on its own Declare row.
        const row = next === "Declare" && storeIds[0] ? declareRowFor(storeIds[0]) : undefined;
        if (row) pickDeclare(row);
        setMode(next);
      }}
      connected={connected}
      sceneSummary={sceneSummary}
      locked={!payload && !designPreview}
      onOverwrite={existingStart && mode === "Check" ? () => void onStartFromUrdf(true) : undefined}
      onFit={page && joints.length ? showWhole : undefined}
      scene={(viewInset) => (
        <RobotView
          viewInset={viewInset}
          sceneKey={`${opened?.path}:${scenePayload?.hash ?? "skeleton"}`}
          parts={sceneParts}
          joints={sceneSource ? jointsOf(sceneSource) : []}
          links={sceneSource && "links" in sceneSource ? sceneSource.links : []}
          highlight={proMode?.highlights ? proHighlight : highlight}
          selected={mode === "Design" && designHover ? designHover : mode === "Run" && proHighlight.length ? proHighlight.map((h) => h.id) : selectedIds.length > 1 ? selectedIds : selectedIds[0] ?? null}
          dim={mode === "Reference" && !selectedIds.length}
          lens={mode === "Design" ? designLens : "off"}
          focus={fitIds.length ? fitIds : selectedIds}
          onDropPart={onDropPart}
          onPick={(id) => {
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
          }}
        >
          {mode === "Design" && <DesignSceneExtras payload={sceneSource} />}
        </RobotView>
      )}
      notices={
        <>
          <FailureBanner />
          {openError && <div className="open-face-error">
            <Notice kind="error" action={retryOpen ? { label: "Retry opening", onClick: () => void retryOpen() } : undefined}>{openError}</Notice>
          </div>}
          {startNote && <div className="start-note"><Notice kind="info">{startNote}</Notice></div>}
        </>
      }
      left={mode === "Design" ? <DesignParts payload={designPayload} />
        : mode === "Declare" && opened ? <DeclareValues payload={opened.declareForm} selectedPath={declareSel} onSelect={(path) => pickDeclare(path)} onAddPart={() => setAddingPart(true)} /> : undefined}
      above={mode === "Design" ? <DesignLens payload={designPayload} /> : undefined}
      tools={mode === "Design" ? <DesignTools payload={designPayload} canAnalyse={!!(opened?.path && opened.revision)} /> : undefined}
      center={mode === "Check" && !payload ? <OpenFace recent={recents} onOpenRobot={() => void onStartFromUrdf()} starters={starters} onNewRobot={(s) => void onNewRobot(s)} onOpenRecent={(p) => void onOpenLockfile(p)} /> : undefined}
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
  );
}
