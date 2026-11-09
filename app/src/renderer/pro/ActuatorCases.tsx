import type { ActuatorCase } from "../../shared/pro/types";

const evidenceClassLabels = {
  unverified: "unverified",
  physical_measurement: "physical measurement",
  prediction_with_held_out_reference: "prediction with held-out reference",
} as const;

// The carrier has already evaluated both claims independently. This component
// only presents their labels, verdicts, claim context, and named gaps; it
// never infers one claim from the other or reads evidence from the filesystem.
export function ActuatorCases({ cases }: { cases: ActuatorCase[] }) {
  return cases.map((actuatorCase) => (
    <article className="actuator-case" key={actuatorCase.case_id}>
      <h2 className="actuator-case-id">{actuatorCase.case_id}</h2>
      {actuatorCase.evidence ? (
        <p className="actuator-case-evidence">
          <code>{actuatorCase.evidence.path}</code>
          <code>{actuatorCase.evidence.sha256}</code>
        </p>
      ) : null}
      {[actuatorCase.conformance, actuatorCase.simulation_validity].map((claim) => (
        <section className="actuator-claim" key={claim.label}>
          <h3 className="actuator-claim-label">{claim.label}</h3>
          <strong className="actuator-claim-verdict">{claim.verdict}</strong>
          <div className="actuator-claim-context">
            <p>Evidence class: {evidenceClassLabels[claim.context.evidence_class]}</p>
            <p>Claim uncertainty: {claim.context.uncertainty}</p>
            <p>Freshness: {claim.context.freshness === "bound_to_revision"
              ? <>bound to raw revision <code>{claim.context.contract_revision}</code></>
              : "unverified"}</p>
            {claim.context.source_witnesses.length ? (
              <ul className="actuator-claim-sources">{claim.context.source_witnesses.map((item) => (
                <li key={`${item.source}/${item.witness}`}>Measured signal source {item.source}; witness {item.witness}</li>
              ))}</ul>
            ) : <p>Source and witness unknown</p>}
            {claim.context.dependencies.length ? (
              <details className="actuator-claim-dependencies">
                <summary>Dependencies: {claim.context.dependencies.length} hash-bound artifacts</summary>
                <ul>{claim.context.dependencies.map((item) => (
                  <li key={`${item.path}/${item.sha256}`}><code>{item.path}</code> <code>{item.sha256}</code></li>
                ))}</ul>
              </details>
            ) : <p>Dependencies: none admitted</p>}
            {claim.context.draft_binding ? (
              <div className="actuator-claim-draft-binding">
                <p>Draft binding: {claim.context.draft_binding.result_id} / {claim.context.draft_binding.claim_id}
                  {" "}at raw revision <code>{claim.context.draft_binding.base_raw_revision}</code></p>
                <ul>{claim.context.draft_binding.evidence.map((item) => (
                  <li key={`${item.artifact.path}/${item.artifact.sha256}`}>
                    {item.provenance} <code>{item.artifact.path}</code> <code>{item.artifact.sha256}</code>
                  </li>
                ))}</ul>
              </div>
            ) : null}
          </div>
          {claim.context.selected_evidence ? (
            <p className="actuator-claim-selected-evidence">
              <span>selected evidence</span>
              <code>{claim.context.selected_evidence.path}</code>
              <code>{claim.context.selected_evidence.sha256}</code>
            </p>
          ) : null}
          {claim.context.validation_reference ? (
            <p className="actuator-claim-validation-reference">
              <span>validation reference {claim.context.reference_held_out ? "held out" : "not held out"}</span>
              <code>{claim.context.validation_reference.path}</code>
              <code>{claim.context.validation_reference.sha256}</code>
            </p>
          ) : null}
          {claim.context.calibration_artifacts.map((artifact) => (
            <p className="actuator-claim-calibration-artifact" key={`${artifact.path}:${artifact.sha256}`}>
              <span>calibration artifact</span>
              <code>{artifact.path}</code>
              <code>{artifact.sha256}</code>
            </p>
          ))}
          {(claim.comparisons ?? []).map((record) => (
            <p className="actuator-claim-comparison" key={record.quantity}>
              <code>{record.quantity}</code>
              <span>{record.observed}</span><span>{record.expected}</span><span>{record.margin}</span>
              <span>{record.provenance?.source ?? "source unknown"}</span>
              <span>{record.provenance?.witness ?? "witness unknown"}</span>
              <span>{record.uncertainty ? `uncertainty ${record.uncertainty.value} ${record.uncertainty.unit}` : "uncertainty unknown"}</span>
            </p>
          ))}
          {claim.gaps.length ? (
            <ul className="actuator-claim-gaps">
              {claim.gaps.map((gap) => (
                <li key={gap.quantity}>
                  <code>{gap.quantity}</code>
                  <span>{gap.observed}</span><span>{gap.expected}</span>
                  <span>{gap.source ?? "source unknown"}</span>
                  <span>{gap.witness ?? "witness unknown"}</span>
                  {claim.label === "simulation validity" ? <span>position uncertainty unknown</span> : null}
                  <span>{gap.action}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ))}
    </article>
  ));
}
