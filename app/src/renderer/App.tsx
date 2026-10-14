import { useEffect, useMemo, useRef, useState } from "react";

import { pushRecent, readRecents } from "./recents";
import type { CheckFacePayload } from "./checkFacePayload";
import { DesignLens, DesignPanel, jointsOf, DesignParts, DesignSceneExtras, DesignTools } from "./DesignPanel";
import type { DesignEditReply, DesignEditRequest } from "./DesignControls";
import { DeclareFace, DeclareValues, type DeclareSubmitResult, type ReviewSubmission } from "./DeclareFace";
import { FailureBanner } from "./FailureBanner";
import { CheckPanel } from "./CheckPanel";
import { EmptyState, Notice, Panel } from "./kit";
import { useDesignView } from "./scene/designView";
import { useSelectedGroup, useSelectedPartId } from "./scene/selectionStore";
import { RobotView } from "./scene/RobotView";
import { flagSummary } from "./FindingList";
import { declarePart, highlightsFromCards, highlightsFromFindings, partsOfPath, targetIds, wholeHighlight, type Highlight } from "./scene/robotScene";
import { Shell } from "./Shell";
import { toScenePart } from "./DesignPanel";
import type { Mode } from "./Frame";
import { DocsPanel } from "./DocsPanel";
import { OpenFace } from "./OpenFace";
import type { FileOpenResult, StartUrdfResult } from "../main/ipc";
import { PlanInput } from "./PlanInput";
import { pro, type ProResult } from "./proRegistry";
import { useSidecarState } from "./useSidecarState";
import type { CheckPayload, DeclareFormPayload, DiffPayload, Finding, LogPayload, PagePayload, PlanPayload } from "../shared/api.gen";

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
  const [findingSel, setFindingSel] = useState<number | null>(null);
  const [fitIds, setFitIds] = useState<string[]>([]);
  const documentGeneration = useRef(0);
  const documentLoad = useRef(0);
  const errorGeneration = useRef(0);
  const renderedGeneration = documentGeneration.current;
  const sidecarState = useSidecarState();
  const connected = sidecarState.phase === "ready";

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
      setRecents(pushRecent({ path: result.path, name: result.payload.robot }));
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
  const designLens = useDesignView().lens;
  const designGroup = useSelectedGroup();
  const designPart = useSelectedPartId();
  const sceneParts = useMemo(() => scenePayload?.parts.map(toScenePart) ?? [], [scenePayload]);

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

  async function chooseAndCheckPlan() {
    if (!opened) return null;
    setSelectedPlanFinding(null);
    const plan = await window.lockwire.choosePlan();
    return plan ? { path: plan, payload: await window.lockwire.checkPlan({ contract: opened.path, plan }) } : null;
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
      return card ? (card.whole ? wholeHighlight(card.severity, all) : highlightsFromCards([card], joints)) : flagged;
    }
    const sel = resultFindings && findingSel !== null ? resultFindings[findingSel] : undefined;
    const lit = sel
      ? resultLabels?.[findingSel!]?.whole ? wholeHighlight(sel.severity, all)
        : highlightsFromFindings([sel], joints, resultLabels?.[findingSel!] ? [resultLabels[findingSel!]!] : [])
      : flagged;
    // A plan with nothing flagged (GO) shows the parts its passed steps touch, as plain selection (the engine's step targets).
    return lit.length || !planSteps ? lit : [...new Set(planSteps.filter((st) => st.status === "PASS").flatMap((st) => (st.targets ?? []).flatMap((t) => targetIds(t, joints))))].map((id) => ({ id, level: "selected" as const }));
  }, [flagged, mode, payload, selectedFinding, findingSel, joints, resultFindings, resultLabels, planSteps, sceneIds]);
  const sceneSummary = useMemo(
    () => flagSummary(flagged.map((h) => h.id), (mode === "Check" ? payload?.cards.flatMap((c) => c.targets) : resultLabels?.flatMap((l) => l.targets)) ?? []),
    [flagged, mode, payload, resultLabels],
  );
  const selectedCard = mode === "Check" && payload && selectedFinding !== null ? payload.cards[selectedFinding] : undefined;
  const selectedResult = resultFindings && findingSel !== null ? resultFindings[findingSel] : undefined;
  const selectedIds = useMemo(
    () => selectedCard ? highlightsFromCards([selectedCard], joints).map((h) => h.id)
      : selectedResult ? highlightsFromFindings([selectedResult], joints, findingSel !== null && resultLabels?.[findingSel] ? [resultLabels[findingSel]] : []).map((h) => h.id)
      : mode === "Declare" ? declarePart(opened?.declareForm.rows, declareSel, partsOfPath(declareSel, sceneIds, joints), sceneIds) : mode === "Design" ? (designGroup?.members ?? (designPart ? [designPart] : [])) : [],
    [selectedCard, selectedResult, resultLabels, findingSel, mode, declareSel, opened, joints, sceneIds, designGroup, designPart],
  );
  const body =
          mode === "Check" ? (
            payload ? (
              <CheckPanel
                payload={payload}
                selected={selectedFinding}
                onSelect={setSelectedFinding}
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
            />
          )
          : mode === "Plan" && opened ? (
            <PlanInput
              key={opened.path}
              onChoosePlan={chooseAndCheckPlan}
              payload={planPayload}
              onPlan={(payload) => {
                if (documentGeneration.current === renderedGeneration) setPlanPayload(payload);
              }}
              onSelectFinding={selectPlanFinding}
              joints={joints}
              selected={findingSel}
              onSelect={setFindingSel}
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
            />
          )
          : mode === "Declare" && opened ? (
            <DeclareFace selectedPath={declareSel} contractPath={opened.path} expectedRevision={opened.revision} payload={opened.declareForm} onSubmit={submitDeclaration} onReview={submitReview} />
          )
          : mode === "Declare" || mode === "Plan" || proMode?.needsRobot ? (
            <EmptyState title="No robot open" action={{ label: "Open robot", onClick: () => { setMode("Check"); void onStartFromUrdf(); } }} />
          )
          : proMode ? null
          : <UnavailableFace>No document was returned for this mode.</UnavailableFace>

  const label = mode === "Reference" ? "Docs" : mode;
  return (
    <Shell
      robotName={payload?.robot}
      mode={mode}
      wide={mode === "Reference"}
      onModeChange={(next) => { setFindingSel(null); setMode(next); }}
      connected={connected}
      sceneSummary={sceneSummary}
      cliLine={mode === "Check" ? payload?.cli_line : undefined}
      locked={!payload && !designPreview}
      onOverwrite={existingStart && mode === "Check" ? () => void onStartFromUrdf(true) : undefined}
      onFit={page && joints.length ? () => { setSelectedFinding(null); setFitIds(joints.map((j) => j.name)); } : undefined}
      scene={(viewInset) => (
        <RobotView
          viewInset={viewInset}
          sceneKey={`${opened?.path}:${scenePayload?.hash ?? "skeleton"}`}
          parts={sceneParts}
          joints={sceneSource ? jointsOf(sceneSource) : []}
          links={sceneSource && "links" in sceneSource ? sceneSource.links : []}
          highlight={proMode?.highlights ? proHighlight : highlight}
          selected={mode === "Design" ? designGroup?.members : selectedIds.length > 1 ? selectedIds : selectedIds[0] ?? null}
          dim={mode === "Reference"}
          lens={mode === "Design" ? designLens : "off"}
          focus={selectedIds.length ? selectedIds : fitIds}
          onPick={(id) => {
            const hit = id && payload ? payload.cards.findIndex((c) => highlightsFromCards([c], joints).some((h) => h.id === id)) : -1;
            if (hit >= 0) setSelectedFinding(hit);
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
        : mode === "Declare" && opened ? <DeclareValues payload={opened.declareForm} selectedPath={declareSel} onSelect={setPickedPath} /> : undefined}
      above={mode === "Design" ? <DesignLens payload={designPayload} /> : undefined}
      tools={mode === "Design" ? <DesignTools payload={designPayload} canAnalyse={!!(opened?.path && opened.revision)} /> : undefined}
      center={mode === "Check" && !payload ? <OpenFace recent={recents} onOpenRobot={() => void onStartFromUrdf()} onOpenRecent={(p) => void onOpenLockfile(p)} /> : undefined}
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
          onSelect={setFindingSel}
          onHighlight={setProHighlight}
        />
      )}
    </Shell>
  );
}
