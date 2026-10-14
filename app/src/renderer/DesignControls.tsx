import { useEffect, useId, useState } from "react";

import { Button, Field, Notice, PanelFooter, Segmented } from "./kit";
import type { CheckPayload, PagePayload } from "../shared/api.gen";
import type { ContractWriteRequest } from "../shared/ipc-contract";

type Command = "place" | "move" | "rotate" | "reparent" | "mount-shape";
type Triple = [number, number, number];
type ShapeKind = "plate" | "bracket" | "tube" | "custom-mesh";

export type DesignEditRequest = Extract<
  ContractWriteRequest,
  { command: "place" | "move" | "rotate" | "reparent" | "mount-shape" }
>;
export type DesignEditReply = { written: boolean; refusal: string | null; diagnostic?: string | null };

export function DesignControls({
  contract,
  expectedRevision,
  selectedPartId,
  onEdit,
  onReopen,
  onPayload,
}: {
  contract: string;
  expectedRevision: string;
  selectedPartId: string | null;
  onEdit: (request: DesignEditRequest) => Promise<DesignEditReply>;
  onReopen: (contract: string) => Promise<CheckPayload | PagePayload>;
  onPayload: (payload: CheckPayload | PagePayload) => void;
}) {
  const [command, setCommand] = useState<Command>("move");
  const [target, setTarget] = useState(selectedPartId ?? "");
  const [parent, setParent] = useState("");
  const [values, setValues] = useState<Triple>([0, 0, 0]);
  const [unit, setUnit] = useState("mm");
  const [kind, setKind] = useState<ShapeKind>("plate");
  const [meshPath, setMeshPath] = useState("");
  const [fastenerPattern, setFastenerPattern] = useState("");
  const [source, setSource] = useState("");
  const [witness, setWitness] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [diagnostic, setDiagnostic] = useState<string | null>(null);
  const [refreshPending, setRefreshPending] = useState(false);
  const [busy, setBusy] = useState(false);

  async function refresh(): Promise<void> {
    try {
      onPayload(await onReopen(contract));
      setRefreshPending(false);
      setRefusal(null);
    } catch (error) {
      setRefusal(`Write succeeded, refresh failed: ${error instanceof Error ? error.message : String(error)}. Retry refresh.`);
    }
  }

  useEffect(() => {
    if (selectedPartId) setTarget(selectedPartId);
  }, [selectedPartId]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || refreshPending) return;
    const provenance = { source, witness, expected_revision: expectedRevision };
    const request: DesignEditRequest = command === "place" ? { command, args: { ...provenance, contract, target, on: parent, offset: values, unit } }
      : command === "move" ? { command, args: { ...provenance, contract, target, offset: values, unit } }
        : command === "rotate" ? { command, args: { ...provenance, contract, target, rpy: values, unit } }
          : command === "reparent" ? { command, args: { ...provenance, contract, target, to: parent } }
            : { command, args: { ...provenance, contract, target, shape: { kind, dimensions_mm: values, ...(fastenerPattern ? { fastener_pattern: fastenerPattern } : {}), ...(meshPath ? { mesh_path: meshPath } : {}) } } };
    setBusy(true);
    try {
      const reply = await onEdit(request);
      if (!reply.written) {
        setDiagnostic(null);
        setRefusal(reply.refusal ?? "The edit was refused.");
        return;
      }
      setRefusal(null);
      setDiagnostic(reply.diagnostic ?? null);
      setRefreshPending(true);
      await refresh();
    } catch (error) {
      setRefusal(`Write failed: ${error instanceof Error ? error.message : String(error)}. Retry applying the edit.`);
    } finally {
      setBusy(false);
    }
  }

  const formId = useId();
  const dirty = !!(source || witness || parent || meshPath || fastenerPattern || values.some((v) => v !== 0) || command !== "move" || unit !== "mm" || kind !== "plate");
  const more = command === "place" || command === "reparent";
  const needsValues = command !== "reparent";
  const text = (label: string, value: string, set: (v: string) => void, required = false, placeholder?: string, hint?: string) =>
    <Field label={label} aria-label={label} placeholder={placeholder} hint={hint} value={value} onChange={(event: React.ChangeEvent<HTMLInputElement>) => set(event.target.value)} required={required} />;
  const num = (index: number, label: string) =>
    <Field label={label} aria-label={label} type="number" value={values[index]} onChange={(event: React.ChangeEvent<HTMLInputElement>) => setValues(values.map((value, i) => i === index ? Number(event.target.value) : value) as Triple)} />;
  return (
    <form id={formId} className="design-controls" onSubmit={submit}>
      <Segmented
        label="Operation"
        value={more ? "more" : command}
        onChange={(v) => setCommand(v === "more" ? "place" : v)}
        options={[{ value: "move", label: "Move" }, { value: "rotate", label: "Rotate" }, { value: "mount-shape", label: "Mount" }, { value: "more", label: "More" }]}
      />
      {more && <Segmented label="More operations" value={command} onChange={setCommand} options={[{ value: "place", label: "Place" }, { value: "reparent", label: "Reparent" }]} />}
      {(command === "place" || command === "reparent") && text(command === "place" ? "On link" : "New parent", parent, setParent, true)}
      {needsValues && <div className="design-controls-values">{num(0, "X")}{num(1, "Y")}{num(2, "Z")}</div>}
      {(command === "place" || command === "move") && <Segmented label="Unit" value={unit === "m" ? "m" : "mm"} onChange={setUnit} options={[{ value: "mm", label: "mm" }, { value: "m", label: "m" }]} />}
      {command === "rotate" && text("Unit", unit, setUnit, true)}
      {command === "mount-shape" && <>
        <Field label="Primitive" aria-label="Primitive" control="select" value={kind} onChange={(event: React.ChangeEvent<HTMLSelectElement>) => setKind(event.target.value as ShapeKind)}>
          <option value="plate">Plate</option><option value="bracket">Bracket</option><option value="tube">Tube</option><option value="custom-mesh">Custom mesh</option>
        </Field>
        {text("Fastener pattern", fastenerPattern, setFastenerPattern)}
        {kind === "custom-mesh" && text("Mesh path", meshPath, setMeshPath, true)}
      </>}
      {text("Source", source, setSource, true, "e.g. caliper, CAD file", "Where the number came from")}
      {text("Witness", witness, setWitness, true, "Who checked it", "Who checked it")}
      {dirty && <PanelFooter><Button type="submit" form={formId} variant="primary" disabled={busy || refreshPending || !target}>Apply</Button></PanelFooter>}
      {refreshPending && <Button disabled={busy} onClick={() => void refresh()}>Retry refresh</Button>}
      {refusal && <div className="design-controls-refusal"><Notice kind="error">{refusal}</Notice></div>}
      {diagnostic && <div className="design-controls-diagnostic"><Notice kind="info">{diagnostic}</Notice></div>}
    </form>
  );
}
