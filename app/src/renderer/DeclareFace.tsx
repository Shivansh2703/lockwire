import { useState } from "react";

import { fmtQty } from "./kit/format";
import { limbOf } from "./kit/limb";
import type { DeclareFormPayload } from "../shared/api.gen";

import { useShellCommand } from "./Shell";
import { Button, CommandLine, Disclosure, Field, FindingDetail, Figure, IconButton, Jumper, Notice, Panel, PartTree, Segmented } from "./kit";
import { MeasureCompareDeclare } from "./MeasureCompareDeclare";
import type { AddPartSubmission } from "./AddPartForm";

export type { AddPartSubmission };
export type { DeclareFormPayload } from "../shared/api.gen";

// The Declare mode of the window (box e4-b, plan v8 §4/§7 E4): a form whose fields,
// labels, units and number kinds all come from `api.declare_form`'s own payload
// -- this component computes nothing about the CONTRACT. The one thing
// it does compose is the footer CLI line and the submit args, both straight
// string-joins of exactly the values already going to `onSubmit`, mirroring what
// `lockwire.cli_line.cli_line` (shlex.quote) already does server-side -- not a
// derived fact about the robot, just the same values spelled as a shell command.
//
// This is NOT the add-a-part form (one submit creating family, model and device) --
// that is E5's,. This form always declares exactly one quantity per
// submit, the same shape `lockwire declare <contract> <path> <value> --source
// <source> --witness <witness>` takes.

export interface DeclareSubmission {
  path: string;
  value: string;
  source: string;
  witness: string;
}

export interface DeclareSubmitResult {
  written: boolean;
  refusal?: string | null;
}

export interface ReviewSubmission {
  path: string;
  state: "approved" | "questioned";
  witness: string;
  note?: string;
}

export interface DeclareFaceProps {
  /** Which declarable value the right-hand panel is about; the left list (DeclareValues) owns the pick. */
  selectedPath: string;
  /** The path argument the printed CLI line and the submit both target. */
  contractPath: string;
  payload: DeclareFormPayload;
  /** Raw revision captured when the parent opened this contract; never inferred here. */
  expectedRevision?: string;
  /** The one door this face is allowed to call: the sidecar's `declare`
   * route into `lockwire.api.declare` and the one writer behind it. This face never
   * imports a writer, never touches a filesystem API, and never fabricates a second
   * path to disk -- the only way a submit reaches a byte on disk is through this
   * callback. */
  onSubmit: (submission: DeclareSubmission) => Promise<DeclareSubmitResult>;
  /** Reviews use the existing contract writer; this face supplies the selected
   * declared path and the reviewer's explicit judgement. */
  onReview: (submission: ReviewSubmission) => Promise<DeclareSubmitResult>;
  /** Arrived from a source or witness elsewhere: the Review section starts open. */
  reviewOpen?: boolean;
  /** The title (the part's name) opens that part in Design. */
  onShowPart?: () => void;
}

/** Display only: the one number format (kit/format.ts) for a plain number; anything else (vectors, text) as typed. */
export function shortValue(value: string, unit = ""): string {
  const n = Number(value);
  return value.trim() !== "" && Number.isFinite(n) && /^-?\d+(?:\.\d+)?(?:e-?\d+)?$/i.test(value.trim()) ? fmtQty(n, unit) : `${value}${unit && ` ${unit}`}`;
}

function shellQuote(value: string): string {
  // Mirrors `lockwire.cli_line.cli_line`'s use of `shlex.quote` (stdlib): unquoted
  // only when every character is shell-safe; otherwise single-quoted, with each
  // embedded single quote closed/escaped/reopened. Not a derived fact -- the exact
  // same values passed to `onSubmit`, spelled as a shell would need them.
  if (value !== "" && /^[\w@%+=:,./-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function valueToString(value: number | number[] | null): string {
  if (value === null) return "";
  return Array.isArray(value) ? value.join(",") : String(value);
}

function valueWithDisplayedUnit(value: string, unit: string | null): string {
  return unit && /^-?\d+(?:\.\d+)?$/.test(value.trim())
    ? `${value} ${unit}`
    : value;
}

/** The left Panel: every declarable value, searchable; the pick lights up its joint on the robot. */
const GROUP_NAMES: Record<string, string> = { Topology: "Battery", Structural: "Structure analysis" }; // Design's names, plain words
const groupName = (g: string) => GROUP_NAMES[g] ?? g;

export function DeclareValues({ payload, selectedPath, onSelect, onAddPart }: { payload: DeclareFormPayload; selectedPath: string; onSelect: (path: string) => void; onAddPart?: () => void }) {
  const rows = payload.rows.filter((row) => row.group !== "World"); // the fixed ground frame is never something to declare
  const size = new Map<string, number>();
  for (const row of rows) size.set(row.group, (size.get(row.group) ?? 0) + 1);
  const unset = new Set([...size.keys()].filter((g) => rows.every((r) => r.group !== g || r.value === null)));
  // A frame row ("Depth Camera Front Lower Depth Optical Frame") sits under its part ("Depth Front Lower Camera") as just its own tail ("Depth Optical Frame").
  const words = (s: string) => s.split(" ");
  const partOf = (g: string) => [...size.keys()].find((p) => size.get(p)! > 1 && words(p).every((w) => g.includes(w)) && p !== g && g.split(" ").length > words(p).length);
  const frameTail = (g: string, p: string) => { const rest = words(g); for (const w of words(p)) rest.splice(rest.indexOf(w), 1); return rest[0] === words(p).slice(-1)[0] ? rest.slice(1).join(" ") || rest.join(" ") : rest.join(" "); };
  const massRows = rows.filter((r) => r.short === "Model mass" && size.get(r.group) === 1 && !r.group.includes(" · ") && !partOf(r.group));
  const asMass = massRows.length > 1 ? new Set(massRows) : new Set<typeof rows[number]>(); // "Model mass" once as a group header, not on every row
  return (
    <Panel title="Values" actions={onAddPart && <Button variant="quiet" onClick={onAddPart}>+ Add part</Button>}>
      <div className="declare-values"><PartTree placeholder="Search values" parts={rows.map((row) => {
        const solo = size.get(row.group) === 1; // one-row group: a single "Group · value" row, no drop-down
        if (asMass.has(row)) {
          const meta = row.value === null ? "unset" : `${shortValue(valueToString(row.value), row.unit ?? "")} · ${row.kind}`;
          // Display only: a geometry-less "… Body" frame carries the URDF's 0.001 kg placeholder, not a real mass.
          if (row.value === 0.001 && row.unit === "kg" && / Body$/.test(row.group)) return { id: row.path, meta: "frame (no mass)", name: groupName(row.group), tip: row.label, group: { id: "frames", label: "Frame (no mass)", kind: "other", open: false } };
          const l = massRows.length >= 8 ? limbOf(row.group) : null; // a robot of many links groups per limb, as Design does
          if (l) return { id: row.path, meta, name: l.rest, tip: row.label, group: { id: `limb:${l.label}`, label: l.label, open: false } };
          return { id: row.path, meta, name: groupName(row.group), tip: row.label, group: { id: "Model mass", label: "Model mass", open: true } };
        }
        const part = solo && !row.group.includes(" · ") ? partOf(row.group) : undefined;
        const meta = row.value === null ? (part ? `${row.short} · unset` : "unset") : `${shortValue(valueToString(row.value), row.unit ?? "")} · ${row.kind}`;
        const name = !solo ? row.short : part ? frameTail(row.group, part) : row.group.includes(" · ") ? `${row.group.split(" · ").slice(-1)[0]} · ${row.short} · ${row.group.split(" · ")[0]}` : `${groupName(row.group)} · ${row.short}`;
        return { id: row.path, meta, name, tip: row.label, ...(part && { depth: 2 }), group: { id: row.group, label: groupName(row.group), ...(solo && { kind: "solo" }), ...(unset.has(row.group) && !solo && { note: `${size.get(row.group)} unset` }) } };
      })} selected={selectedPath} onSelect={onSelect} /></div>
    </Panel>
  );
}

type Sent = "idle" | "submitting" | "written" | "refused";

export function DeclareFace({
  selectedPath,
  contractPath,
  payload,
  expectedRevision,
  onSubmit,
  onReview,
  reviewOpen,
  onShowPart,
}: DeclareFaceProps) {
  const selectedRow = payload.rows.find((row) => row.path === selectedPath) ?? null;
  const [value, setValue] = useState(() => valueToString(selectedRow?.value ?? null));
  const [valueFocused, setValueFocused] = useState(false);
  const [source, setSource] = useState("");
  const [witness, setWitness] = useState(payload.witness.prefill);
  const [status, setStatus] = useState<Sent>("idle");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [reviewState, setReviewState] = useState<"" | "approved" | "questioned">("");
  const [reviewWitness, setReviewWitness] = useState(payload.witness.prefill);
  const [reviewNote, setReviewNote] = useState("");
  const [reviewStatus, setReviewStatus] = useState<Sent>("idle");
  const [reviewRefusal, setReviewRefusal] = useState<string | null>(null);

  // Picking another value starts its draft from that value; source and witness carry over.
  const [shownPath, setShownPath] = useState(selectedPath);
  if (shownPath !== selectedPath) {
    setShownPath(selectedPath);
    setValue(valueToString(selectedRow?.value ?? null));
    setStatus("idle");
    setReviewState("");
    setReviewNote("");
    setReviewStatus("idle");
    setReviewRefusal(null);
  }

  // Escape cancels the in-progress draft (E2.3): back to the selected row's own value and a
  // blank source/witness, never a submit -- `submit()` is only ever reached from the Declare
  // button's onClick.
  function cancelDraft() {
    setValue(valueToString(selectedRow?.value ?? null));
    setSource("");
    setWitness(payload.witness.prefill);
  }

  const selectedUnit = selectedRow?.unit ?? null;
  const submittedValue = valueWithDisplayedUnit(value, selectedUnit);
  const cliLine = [
    "lockwire",
    "declare",
    contractPath,
    selectedPath,
    submittedValue,
    "--source",
    source,
    "--witness",
    witness,
  ]
    .map(shellQuote)
    .join(" ");

  // Display only: long paths lose their middle; the value is never shortened.
  const mid = (s: string) => (s.length > 32 ? `${s.slice(0, 14)}…${s.slice(-14)}` : s);
  const cliDisplay = [mid(shellQuote(contractPath)), mid(shellQuote(selectedPath))].reduce((line, p) => `${line} ${p}`, "lockwire declare") + ` ${[submittedValue, "--source", source, "--witness", witness].map(shellQuote).join(" ")}`;

  const ready = value.trim() !== "" && source.trim() !== ""; // nothing to declare yet: no command chip
  const inShell = useShellCommand(ready ? cliLine : null);

  async function submit() {
    setStatus("submitting");
    setRefusal(null);
    try {
      const result = await onSubmit({
        path: selectedPath,
        value: submittedValue,
        source,
        witness,
      });
      if (result.written) {
        setStatus("written");
      } else {
        setStatus("refused");
        setRefusal(result.refusal ?? null);
      }
    } catch (error) {
      setStatus("refused");
      setRefusal(error instanceof Error ? error.message : String(error));
    }
  }

  async function submitReview() {
    if (!selectedRow || selectedRow.value === null || !selectedRow.provenance || !reviewState) return;
    setReviewStatus("submitting");
    setReviewRefusal(null);
    try {
      const result = await onReview({
        path: selectedRow.path,
        state: reviewState,
        witness: reviewWitness,
        ...(reviewNote.trim() ? { note: reviewNote } : {}),
      });
      setReviewStatus(result.written ? "written" : "refused");
      if (!result.written) setReviewRefusal(result.refusal ?? null);
    } catch (error) {
      setReviewStatus("refused");
      setReviewRefusal(error instanceof Error ? error.message : String(error));
    }
  }

  const witnessIsDefault = witness === payload.witness.prefill;
  const reviewable = selectedRow !== null && selectedRow.value !== null && selectedRow.provenance !== null;

  return (
    <Panel title={selectedRow ? <span className="declare-title">{selectedRow.short}<small className="declare-title-part">{onShowPart
      ? <Jumper label={`Show ${selectedRow.group} in Design`} onClick={onShowPart}>{groupName(selectedRow.group)}</Jumper> : groupName(selectedRow.group)}</small></span> : "Declare"}>
      <div className="declare-face" onKeyDown={(event) => { if (event.key === "Escape") cancelDraft(); }}>
        <Field
          label={<>{selectedUnit && valueFocused ? `Value (${selectedUnit})` : "Value"} <span className="kit-kind">{selectedRow?.value === null ? "unset" : selectedRow?.kind ?? "declared"}</span></>}
          aria-label="value"
          value={valueFocused ? value : shortValue(value, selectedUnit ?? "")}
          onFocus={() => setValueFocused(true)}
          onBlur={() => setValueFocused(false)}
          onChange={(event: { target: { value: string } }) => setValue(event.target.value)} />
        <Field label="Source" hint="Where the number came from" placeholder="e.g. datasheet p.12" aria-label="source" value={source} onChange={(event: { target: { value: string } }) => setSource(event.target.value)} />
        <Field
          label="Witness"
          hint={witnessIsDefault ? "Who checked it (default)" : "Who checked it"}
          placeholder="Who checked it"
          aria-label="witness"
          value={witness}
          onChange={(event: { target: { value: string } }) => setWitness(event.target.value)}
        />
        <div className="declare-actions">
          <Button variant="primary" onClick={submit} disabled={status === "submitting" || !selectedPath}>Declare</Button>
          <MeasureCompareDeclare
            key={selectedPath}
            observation={selectedPath ? {
              path: selectedPath,
              value: submittedValue,
              source: source.trim() || null,
              witness: witness.trim() || null,
            } : null}
            contractPath={contractPath}
            expectedRevision={expectedRevision}
          />
        </div>
        {status === "written" && <Notice kind="info">Written</Notice>}
        {status === "refused" && <Notice kind="error">{refusal ?? "Refused"}</Notice>}
        <Disclosure summary="Review" open={reviewOpen || undefined}>
          <div className="declare-review" aria-label="Review declared value">
            {reviewable ? (
              <FindingDetail
                rows={[
                  { label: "Declared", value: <Figure value={typeof selectedRow.value === "number" ? selectedRow.value : valueToString(selectedRow.value)} unit={selectedRow.unit ?? undefined} kind={selectedRow.kind} /> },
                  { label: "Source", value: selectedRow.provenance!.source },
                  { label: "Witness", value: selectedRow.provenance!.witness },
                ]}
              />
            ) : selectedRow && <Notice kind="info">Nothing declared yet, so nothing to review.</Notice>}
            <Segmented
              label="Review decision"
              options={[{ value: "approved", label: "Approve" }, { value: "questioned", label: "Question" }]}
              value={reviewState}
              onChange={(v) => { setReviewState(v); setReviewStatus("idle"); }}
            />
            <Field label="Witness" aria-label="Review witness" value={reviewWitness} onChange={(event: { target: { value: string } }) => { setReviewWitness(event.target.value); setReviewStatus("idle"); }} />
            <Field label="Note" control="textarea" aria-label="Review note" value={reviewNote} onChange={(event: { target: { value: string } }) => { setReviewNote(event.target.value); setReviewStatus("idle"); }} />
            <Button
              onClick={submitReview}
              disabled={reviewStatus === "submitting" || !reviewable || !reviewState || !reviewWitness.trim() || (reviewState === "questioned" && !reviewNote.trim())}
            >
              Record review
            </Button>
            {reviewStatus === "written" && <Notice kind="info">Review recorded</Notice>}
            {reviewStatus === "refused" && <Notice kind="error">{reviewRefusal ?? "Review refused"}</Notice>}
          </div>
        </Disclosure>
        {inShell
          ? ready && <IconButton icon="copy" label="Copy command" onClick={() => { void navigator.clipboard?.writeText(cliLine).catch(() => {}); }} />
          : <div className="declare-face-cli-line"><CommandLine command={cliLine} display={cliDisplay} /></div>}
      </div>
    </Panel>
  );
}
