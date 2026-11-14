import { useState } from "react";

import type { RobotLink, RobotParam } from "../shared/api.gen";
import type { DeclareSubmitResult } from "./DeclareFace";
import { Button, Field, Notice } from "./kit";
import { kept, shown, useUnits } from "./units";

// Change a named size (arm_length = 220 mm): one number per name. Saving declares it, the page reloads,
// and every shape and torque that uses the name follows. A link whose mass was worked out from its
// shapes says so here, in the engine's own words; a declared mass is never listed, so it never moves.

/** The number as typed, or null when it is not a positive number. */
export const sizeValue = (text: string): number | null => (/^\d+(\.\d+)?$/.test(text.trim()) && Number(text) > 0 ? Number(text) : null);

/** The write the app sends to change a named size: the declare path, the number in the size's own unit. */
export const sizeEditRequest = (contract: string, expectedRevision: string, name: string, value: number, witness: string) => ({
  command: "declare" as const,
  args: { contract, expected_revision: expectedRevision, path: `params/${name}/value`, value, source: "changed in the app", witness },
});

export function NamedSizes({ params, links, onSave }: { params: RobotParam[]; links: RobotLink[]; onSave: (name: string, value: number) => Promise<DeclareSubmitResult> }) {
  useUnits();
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [result, setResult] = useState<DeclareSubmitResult | "busy" | null>(null);
  const notes = links.filter((l) => l.mass_note);

  async function save(p: RobotParam) {
    const value = sizeValue(typed[p.name] ?? "");
    if (value === null) return setResult({ written: false, refusal: "Type a size greater than zero." });
    setResult("busy");
    try {
      const done = await onSave(p.name, kept(value, p.unit)); // typed in the shown unit, saved in the size's own
      setResult(done);
      if (done.written) setTyped((t) => { const { [p.name]: _gone, ...rest } = t; return rest; });
    } catch (error) {
      setResult({ written: false, refusal: error instanceof Error ? error.message : String(error) });
    }
  }
  return (
    <div className="declare-review add-part" aria-label="Named sizes">
      {params.map((p) => (
        <div key={p.name} className="named-size">
          <Field label={`${p.name} (${shown(p.value, p.unit).unit})`} aria-label={`size ${p.name}`} value={typed[p.name] ?? String(shown(p.value, p.unit).value)}
            onChange={(e: { target: { value: string } }) => setTyped((t) => ({ ...t, [p.name]: e.target.value }))} />
          <Button variant="secondary" aria-label={`save ${p.name}`} disabled={result === "busy" || typed[p.name] === undefined} onClick={() => void save(p)}>Save</Button>
        </div>
      ))}
      {notes.map((l) => <small key={l.name} className="add-part-help">{l.name}: {l.mass_note}</small>)}
      {result && result !== "busy" && (result.written ? <Notice kind="info">Size saved</Notice> : <Notice kind="error">{result.refusal ?? "Refused"}</Notice>)}
    </div>
  );
}
