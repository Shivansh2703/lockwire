import proSchema from "../../renderer/pro/schema.json";
import type {
  AnalysisJobStatePayload,
  CheckPayloadPro,
  PagePayloadPro,
  PreflightPayload,
  RunStatePayload,
  WhatIfPayloadPro,
} from "../../shared/pro/types";
import { payloadValidator, validated } from "../pagePayload";

const compile = <T>(name: string) => payloadValidator<T>(name, proSchema);

const validateAnalysisJobState = compile<AnalysisJobStatePayload>("AnalysisJobStatePayload");
const validatePreflight = compile<PreflightPayload>("PreflightPayload");
const validateRunState = compile<RunStatePayload>("RunStatePayload");

const validatePagePro = compile<PagePayloadPro>("PagePayloadPro");
const validateCheckPro = compile<CheckPayloadPro>("CheckPayloadPro");
const validateWhatIfPro = compile<WhatIfPayloadPro>("WhatIfPayloadPro");

// The paid sidecar's page / check / what-if replies carry fields the free schema forbids; these
// replace the free validators when pro/ is present (main/proRegistry.ts hands them to ipc.ts).
export const proValidators = {
  page: (payload: unknown): PagePayloadPro => validated(payload, validatePagePro, "contract:page"),
  check: (payload: unknown): CheckPayloadPro => validated(payload, validateCheckPro, "file:open"),
  whatIf: (payload: unknown): WhatIfPayloadPro => validated(payload, validateWhatIfPro, "contract:what-if"),
};

export function validateAnalysisJobStatePayload(payload: unknown): AnalysisJobStatePayload {
  return validated(payload, validateAnalysisJobState, "analysis");
}

export function validatePreflightPayload(payload: unknown): PreflightPayload {
  return validated(payload, validatePreflight, "contract:preflight");
}

export function validateRunStatePayload(payload: unknown): RunStatePayload {
  return validated(payload, validateRunState, "run");
}
