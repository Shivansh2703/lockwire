import { VERDICT_EXIT_CODES, type CheckPayload } from "../shared/api.gen";
import { callSidecar } from "./sidecar";

// Helpers shared by the free dispatch (ipc.ts) and the paid handlers (pro/): argument guards and the
// one sidecar round trip. Kept out of ipc.ts so pro/ never imports ipc.ts (no import cycle).

export function isRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function hasOnlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

export function hasStrings(value: Record<string, unknown>, names: readonly string[]): boolean {
  return names.every((name) => typeof value[name] === "string");
}

export function isEmptyRequest(args: unknown): boolean {
  return args === undefined || (isRecord(args) && hasOnlyKeys(args, []));
}

export interface SidecarRunReply {
  version: string;
  cli_line: string;
  exit_code: number;
  verdict: string | null;
  payload: unknown | null;
  refusal: string | null;
}

export type DomainVerdict = CheckPayload["verdict"];

export function isDomainVerdict(value: unknown): value is DomainVerdict {
  return typeof value === "string" && Object.hasOwn(VERDICT_EXIT_CODES, value);
}

export function invalidRunReply(): never {
  throw new Error("sidecar: invalid run reply");
}

export function validateRunReplySemantics(reply: SidecarRunReply): SidecarRunReply {
  if (reply.refusal !== null) {
    // A sidecar refusal is a handled domain response, not a process/transport failure. Existing
    // commands retain their own nonzero exit conventions, but a refusal can never carry a
    // success exit, verdict, or payload at the same time.
    if (reply.exit_code === 0 || reply.verdict !== null || reply.payload !== null) invalidRunReply();
    return reply;
  }

  const document = isRecord(reply.payload) ? reply.payload : null;
  const hasDocumentVerdict = document !== null && Object.hasOwn(document, "verdict");
  const hasDocumentExit = document !== null && Object.hasOwn(document, "exit_code");

  // Non-document commands legitimately use a null envelope verdict (and some, such as RunState,
  // have an unrelated `exit_code` field of their own). A decision document may not omit its exit,
  // and a verdict-bearing envelope may not exist without the document that substantiates it.
  if (!hasDocumentVerdict) {
    if (reply.verdict !== null) invalidRunReply();
    return reply;
  }
  if (!hasDocumentExit) invalidRunReply();

  const documentVerdict = document.verdict;
  const documentExit = document.exit_code;
  if (
    !isDomainVerdict(documentVerdict) ||
    typeof documentExit !== "number" ||
    !Number.isInteger(documentExit) ||
    VERDICT_EXIT_CODES[documentVerdict] !== documentExit ||
    reply.verdict !== documentVerdict ||
    reply.exit_code !== documentExit
  ) {
    invalidRunReply();
  }
  return reply;
}

export function parseSidecarRunReply(value: unknown): SidecarRunReply {
  if (
    !isRecord(value) ||
    !hasOnlyKeys(value, ["version", "cli_line", "exit_code", "verdict", "payload", "refusal"]) ||
    typeof value.version !== "string" ||
    typeof value.cli_line !== "string" ||
    typeof value.exit_code !== "number" ||
    !Number.isInteger(value.exit_code) ||
    (value.verdict !== null && !isDomainVerdict(value.verdict)) ||
    !Object.hasOwn(value, "payload") ||
    (value.refusal !== null && typeof value.refusal !== "string")
  ) {
    throw new Error("sidecar: invalid run reply");
  }
  return validateRunReplySemantics({
    version: value.version,
    cli_line: value.cli_line,
    exit_code: value.exit_code,
    verdict: value.verdict,
    payload: value.payload,
    refusal: value.refusal,
  });
}

export function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

export async function runSidecarCommand(request: unknown): Promise<SidecarRunReply> {
  const response = await callSidecar("/v1/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  return parseSidecarRunReply(await response.json());
}
