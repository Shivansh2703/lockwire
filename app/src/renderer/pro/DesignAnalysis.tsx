import { useEffect, useRef, useState } from "react";

import type { AnalysisJobStatePayload } from "../../shared/pro/types";
import { Disclosure, Field, IconButton, Inspector, JobBar, Notice, Panel, type Status } from "../kit";
import { setDesignView } from "../scene/designView";
import { proApi } from "./api";

// Structural analysis on the Design page: start, poll and cancel one revision-bound job.
const JOB_STATUS: Record<AnalysisJobStatePayload["status"], Status> = { running: "muted", completed: "go", failed: "bad", cancelled: "warn" };

export function DesignAnalysis({ contract, expectedRevision }: { contract: string; expectedRevision: string }) {
  const [procedureId, setProcedureId] = useState("");
  const [evidenceRoot, setEvidenceRoot] = useState("");
  const [packageManifest, setPackageManifest] = useState("");
  const [state, setState] = useState<AnalysisJobStatePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function start(): Promise<void> {
    setError(null);
    try {
      setState(await proApi().startAnalysis({
        contract,
        expected_revision: expectedRevision,
        procedure_id: procedureId.trim(),
        evidence_root: evidenceRoot.trim(),
        package_manifest: packageManifest.trim(),
      }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function control(command: "poll" | "cancel"): Promise<void> {
    setError(null);
    try {
      setState(command === "poll" ? await proApi().pollAnalysis() : await proApi().cancelAnalysis());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  // Polling is automatic while the job runs (ponytail: fixed 1s interval, no backoff).
  const poll = useRef(control);
  poll.current = control;
  useEffect(() => {
    if (state?.status !== "running") return;
    const timer = setTimeout(() => void poll.current("poll"), 1000);
    return () => clearTimeout(timer);
  }, [state]);

  const result = state?.document;
  const solverHash = typeof result?.solver.binary_sha256 === "string" ? result.solver.binary_sha256 : null;
  const running = state?.status === "running";
  return (
    <Panel title="Analysis" actions={<IconButton icon="close" label="Close" onClick={() => setDesignView({ analysis: false })} />}>
      <div className="structural-analysis" aria-label="Structural analysis">
        <Field label="Procedure" aria-label="Analysis procedure ID" value={procedureId} onChange={(event: React.ChangeEvent<HTMLInputElement>) => setProcedureId(event.target.value)} />
        <Field label="Evidence" aria-label="Analysis evidence root" value={evidenceRoot} onChange={(event: React.ChangeEvent<HTMLInputElement>) => setEvidenceRoot(event.target.value)} />
        <Field label="Package" aria-label="Analysis package manifest" value={packageManifest} onChange={(event: React.ChangeEvent<HTMLInputElement>) => setPackageManifest(event.target.value)} />
        <button
          type="button"
          className="kit-btn kit-btn-primary analysis-start"
          disabled={!procedureId.trim() || !evidenceRoot.trim() || !packageManifest.trim() || running}
          onClick={start}
        >
          Start
        </button>
        {error && <Notice kind="error">{error}</Notice>}
        {state && (
          <div className="analysis-status">
            <JobBar status={JOB_STATUS[state.status]} word={state.status} progress={running ? undefined : 1} onCancel={running ? () => void control("cancel") : undefined} />
          </div>
        )}
        {state?.error && <Notice kind="error">{state.error}</Notice>}
        {result && (
          <div className="analysis-result">
            <Inspector
              rows={[
                { label: "Procedure", value: `${result.procedure_id} (${result.procedure_kind})` },
                { label: "Revision", value: result.contract_revision },
                { label: "Package SHA-256", value: result.package_manifest_sha256 },
                ...(solverHash ? [{ label: "Solver SHA-256", value: solverHash }] : []),
                { label: "Output SHA-256", value: result.output_sha256 },
                { label: "Physical conformance", value: result.physical_conformance },
                { label: "Simulation validity", value: result.simulation_validity },
                { label: "Reason", value: result.reason },
              ]}
            />
            <Disclosure summary="Raw results">
              <pre>{JSON.stringify({ results: result.results, comparisons: result.comparisons }, null, 2)}</pre>
            </Disclosure>
          </div>
        )}
      </div>
    </Panel>
  );
}
