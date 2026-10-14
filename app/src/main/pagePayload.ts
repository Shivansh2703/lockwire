import Ajv from "ajv";

import type {
  CheckPayload,
  DeclareFormPayload,
  DiffPayload,
  EditPayload,
  LogPayload,
  PagePayload,
  PlanPayload,
  WhatIfPayload,
  StartPayload,
} from "../shared/api.gen";
import schemaDocument from "../renderer/schema.json";

const ajv = new Ajv({ allErrors: true, strict: false });

type SchemaDocument = { components: { schemas: Record<string, object> } };

/** Compile one named schema from a schema document (schema.json by default); pro/ compiles its
 * own names from renderer/pro/schema.json through the same door. */
export function payloadValidator<T>(name: string, document: SchemaDocument = schemaDocument) {
  return ajv.compile<T>({
    ...document.components.schemas[name],
    $id: `lockwire-${name}`,
    components: document.components,
  });
}

const validateCheck = payloadValidator<CheckPayload>("CheckPayload");
const validateDeclareForm = payloadValidator<DeclareFormPayload>("DeclareFormPayload");
const validateDiff = payloadValidator<DiffPayload>("DiffPayload");
const validateEdit = payloadValidator<EditPayload>("EditPayload");
const validateLog = payloadValidator<LogPayload>("LogPayload");
const validatePage = payloadValidator<PagePayload>("PagePayload");
const validatePlan = payloadValidator<PlanPayload>("PlanPayload");
const validateWhatIf = payloadValidator<WhatIfPayload>("WhatIfPayload");
const validateStart = payloadValidator<StartPayload>("StartPayload");

export function validateStartPayload(payload: unknown): StartPayload {
  return validated(payload, validateStart, "file:start-urdf");
}

export function validated<T>(payload: unknown, validate: (value: unknown) => value is T, command: string): T {
  if (!validate(payload)) throw new Error(`${command}: invalid payload`);
  return payload;
}

export function validateCheckPayload(payload: unknown): CheckPayload {
  return validated(payload, validateCheck, "file:open");
}

export function validateDeclareFormPayload(payload: unknown): DeclareFormPayload {
  return validated(payload, validateDeclareForm, "contract:declare-form");
}

export function validateDiffPayload(payload: unknown): DiffPayload {
  return validated(payload, validateDiff, "contract:diff");
}

export function validateEditPayload(payload: unknown): EditPayload {
  return validated(payload, validateEdit, "contract:write");
}

export function validateLogPayload(payload: unknown): LogPayload {
  return validated(payload, validateLog, "contract:history");
}

export function validatePlanPayload(payload: unknown): PlanPayload {
  return validated(payload, validatePlan, "contract:plan");
}

export function validateWhatIfPayload(payload: unknown): WhatIfPayload {
  return validated(payload, validateWhatIf, "contract:what-if");
}

/** Refuse a sidecar reply that the producer's generated PagePayload schema would not accept. */
export function validatePagePayload(payload: unknown): PagePayload {
  try {
    return validated(payload, validatePage, "contract:page");
  } catch {
    throw new Error("contract:page: invalid page payload");
  }
}
