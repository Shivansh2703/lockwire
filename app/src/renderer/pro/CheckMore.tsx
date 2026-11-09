import { useEffect, useState } from "react";

import type { PagePayload } from "../../shared/api.gen";
import type { ActuatorCase, CheckPayloadPro, PagePayloadPro } from "../../shared/pro/types";
import type { ContractActuatorReviewDraftRequest } from "../../shared/pro/ipc-contract";
import { Disclosure, StatusPill } from "../kit";
import { ActuatorCases } from "./ActuatorCases";
import { ActuatorReviewDraft } from "./ActuatorReviewDraft";
import { proApi } from "./api";

/** The Check page's "Device state" pill: the worst device state the page payload reports. */
export function checkPills(freePage: PagePayload | null) {
  const page = freePage as PagePayloadPro | null; // the paid sidecar fills the paid fields
  const healthWord = page?.health?.worst ? page.health_words[page.health.worst] : undefined;
  return healthWord ? (
    <span title="Worst device state"><span className="kit-sr">Device state: </span><StatusPill status="muted">{healthWord}</StatusPill></span>
  ) : null;
}

/** The "More" section of the Check page: actuator case reports and the review-draft form. */
export function CheckMore({ contract, expectedRevision, check }: { contract?: string; expectedRevision?: string; check: CheckPayloadPro | null }) {
  const [cases, setCases] = useState<ActuatorCase[]>(check?.actuator_cases ?? []);
  useEffect(() => setCases(check?.actuator_cases ?? []), [check]);

  async function onPrepare(request: ContractActuatorReviewDraftRequest) {
    const reply = await proApi().prepareActuatorReviewDraft(request);
    setCases((current) => reply.document.actuator_cases ?? current);
    return reply;
  }

  return cases.length || contract ? (
    <Disclosure summary="More">
      <ActuatorCases cases={cases} />
      {contract && expectedRevision ? (
        <ActuatorReviewDraft contract={contract} expectedRevision={expectedRevision} cases={cases} onPrepare={onPrepare} />
      ) : null}
    </Disclosure>
  ) : null;
}
