import { useState, type FormEvent } from "react";

import type { CheckPayloadPro } from "../../shared/pro/types";
import type { ActuatorCase, ReviewDraftPayload } from "../../shared/pro/types";
import type { ContractActuatorReviewDraftRequest } from "../../shared/pro/ipc-contract";

type DraftReply = { document: CheckPayloadPro; cliLine: string };

type Props = {
  contract: string;
  expectedRevision: string;
  cases: ActuatorCase[];
  onPrepare: (request: ContractActuatorReviewDraftRequest) => Promise<DraftReply>;
};

const targets = ["physical", "firmware", "configuration", "model", "calibration"] as const;
const categories = ["physical", "model_calibration", "spec_design"] as const;

function lines(value: string): string[] {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

function DraftDocument({ draft, nextStep, cliLine, currentRevision }: {
  draft: ReviewDraftPayload;
  nextStep: string | null | undefined;
  cliLine: string;
  currentRevision: string;
}) {
  return <article className="actuator-review-draft-result">
    <h2>Actuator review draft</h2>
    <p>Draft base raw revision: <code>{draft.base_raw_revision}</code></p>
    <p>Current opened revision: <code>{currentRevision}</code></p>
    <p>{draft.discrepancy}</p>
    <p>Correction category: {draft.correction_category}</p>
    <ul>{draft.evidence.map((citation) => <li key={`${citation.result_id}/${citation.claim_id}/${citation.artifact.sha256}`}>
      <span>{citation.result_id} / {citation.claim_id}: {citation.provenance}</span>{" "}
      <code>{citation.artifact.path}</code>{" "}<code>{citation.artifact.sha256}</code>
    </li>)}</ul>
    <ul>{draft.alternatives.map((alternative, index) => <li key={index}>{alternative}</li>)}</ul>
    <p><code>{draft.proposed_patch.kind}</code>: {draft.proposed_patch.patch}</p>
    <p>{draft.rationale}</p>
    <ul>{draft.required_recheck.map((item) => <li key={item}>{item}</li>)}</ul>
    {nextStep ? <p>{nextStep}</p> : null}
    <p>Replay: <code>{cliLine}</code></p>
  </article>;
}

// This form deliberately has no defaults for a case or any evidence identity.
// A draft can only carry the exact case, artifact, root, and raw revision the
// engineer explicitly selects; it has no apply or write path.
export function ActuatorReviewDraft({ contract, expectedRevision, cases, onPrepare }: Props) {
  const [caseId, setCaseId] = useState("");
  const [evidenceRoot, setEvidenceRoot] = useState("");
  const [artifactPath, setArtifactPath] = useState("");
  const [artifactSha256, setArtifactSha256] = useState("");
  const [provenance, setProvenance] = useState("");
  const [discrepancy, setDiscrepancy] = useState("");
  const [alternatives, setAlternatives] = useState("");
  const [target, setTarget] = useState("");
  const [proposedPatch, setProposedPatch] = useState("");
  const [rationale, setRationale] = useState("");
  const [category, setCategory] = useState("");
  const [rechecks, setRechecks] = useState("");
  const [draft, setDraft] = useState<ReviewDraftPayload | null>(null);
  const [nextStep, setNextStep] = useState<string | null | undefined>(null);
  const [cliLine, setCliLine] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!caseId || !evidenceRoot || !artifactPath || !artifactSha256 || !target || !category) {
      setRefusal("Select a declared case and provide every evidence identity before preparing a draft.");
      return;
    }
    try {
      const reply = await onPrepare({
        contract,
        case_id: caseId,
        evidence: { path: artifactPath, sha256: artifactSha256 },
        evidence_root: evidenceRoot,
        expected_revision: expectedRevision,
        provenance,
        discrepancy,
        alternatives: lines(alternatives),
        proposed_target: target as ContractActuatorReviewDraftRequest["proposed_target"],
        proposed_patch: proposedPatch,
        rationale,
        correction_category: category as ContractActuatorReviewDraftRequest["correction_category"],
        required_recheck: lines(rechecks),
      });
      const nextDraft = reply.document.actuator_review_draft;
      if (!nextDraft) throw new Error("the sidecar returned no actuator review draft");
      setDraft(nextDraft);
      setNextStep(reply.document.actuator_review_next_step);
      setCliLine(reply.cliLine);
      setRefusal(null);
    } catch (error) {
      // Keep a previously prepared proposal visible for conflict resolution.
      setRefusal(error instanceof Error ? error.message : "Actuator review draft was refused.");
    }
  }

  return <details className="actuator-review-draft">
    <summary>Prepare actuator review draft</summary>
    <form onSubmit={submit}>
      <label>Declared actuator case
        <select value={caseId} onChange={(event) => setCaseId(event.target.value)}>
          <option value="">Select a declared case</option>
          {cases.map((item) => <option key={item.case_id} value={item.case_id}>{item.case_id}</option>)}
        </select>
      </label>
      <label>Evidence root <input value={evidenceRoot} onChange={(event) => setEvidenceRoot(event.target.value)} /></label>
      <label>Artifact relative path <input value={artifactPath} onChange={(event) => setArtifactPath(event.target.value)} /></label>
      <label>Artifact SHA-256 <input value={artifactSha256} onChange={(event) => setArtifactSha256(event.target.value)} /></label>
      <label>Provenance <textarea value={provenance} onChange={(event) => setProvenance(event.target.value)} /></label>
      <label>Discrepancy <textarea value={discrepancy} onChange={(event) => setDiscrepancy(event.target.value)} /></label>
      <label>Alternatives (one per line) <textarea value={alternatives} onChange={(event) => setAlternatives(event.target.value)} /></label>
      <label>Proposed target
        <select value={target} onChange={(event) => setTarget(event.target.value)}>
          <option value="">Select a target</option>
          {targets.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label>Proposed patch <textarea value={proposedPatch} onChange={(event) => setProposedPatch(event.target.value)} /></label>
      <label>Rationale <textarea value={rationale} onChange={(event) => setRationale(event.target.value)} /></label>
      <label>Correction category
        <select value={category} onChange={(event) => setCategory(event.target.value)}>
          <option value="">Select a correction category</option>
          {categories.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label>Required rechecks (one per line) <textarea value={rechecks} onChange={(event) => setRechecks(event.target.value)} /></label>
      <button type="submit">Prepare read-only draft</button>
    </form>
    {refusal ? <p role="alert">{refusal}</p> : null}
    {draft ? <DraftDocument draft={draft} nextStep={nextStep} cliLine={cliLine} currentRevision={expectedRevision} /> : null}
  </details>;
}
