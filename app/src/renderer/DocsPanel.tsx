import { useState } from "react";

import type { DiffPayload, Finding, LogPayload, PagePayload } from "../shared/api.gen";
import { HistoryDrawer } from "./HistoryDrawer";
import type { OnJump } from "./jump";
import { EmptyState, Jumper, Panel, Segmented } from "./kit";
import { ReferenceFace } from "./ReferenceFace";
import { WaysFace } from "./WaysFace";

type View = "docs" | "ways" | "history";
const VIEWS: { value: View; label: string }[] = [{ value: "docs", label: "Sheets" }, { value: "ways", label: "Ways" }, { value: "history", label: "History" }];
const show = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

// What a compare changed, one row per fact: path: old -> new (the payload's own values).
// A row naming a quantity opens its Declare row.
function DiffRows({ diff, onJump }: { diff: DiffPayload; onJump?: OnJump }) {
  const rows: [string, string | null][] = [
    ...diff.changes.map((c) => [`${c.path}: ${show(c.old)} → ${show(c.new)}`, c.path] as [string, string]),
    ...diff.added.map((e) => [`added ${e.kind} ${e.id}`, null] as [string, null]),
    ...diff.removed.map((e) => [`removed ${e.kind} ${e.id}`, null] as [string, null]),
    ...diff.findings_new.map((f) => [`new finding: ${f.quantity} (${f.severity})`, f.quantity] as [string, string]),
    ...diff.findings_gone.map((f) => [`finding gone: ${f.quantity} (${f.severity})`, f.quantity] as [string, string]),
  ];
  return (
    <div className="history-diff-result" data-changed={diff.changed}>
      {rows.length === 0 ? <EmptyState title="No changes" /> : rows.map(([r, path], i) => (
        <code key={i} className="history-diff-row">{path && onJump?.can({ path }) ? <Jumper label={`Go to ${path}`} onClick={() => onJump({ path })}>{r}</Jumper> : r}</code>
      ))}
    </div>
  );
}

// Docs: one wide panel, Docs | History. The shell dims the robot behind it.
export function DocsPanel({
  page, selectedPlanFinding, history, contract, expectedRevision, diff, onDiff, onApplied, names, onJump,
}: {
  page: PagePayload;
  selectedPlanFinding: Finding | null;
  history: { available: boolean; entries: LogPayload };
  contract?: string;
  expectedRevision?: string;
  diff: DiffPayload | null;
  onDiff: (a: string, b?: string) => void;
  onApplied: (contract: string) => Promise<void>;
  names?: Set<string>;
  onJump?: OnJump;
}) {
  const [view, setView] = useState<View>("docs");
  return (
    <Panel title={VIEWS.find((v) => v.value === view)!.label} actions={<Segmented label="Docs view" options={VIEWS} value={view} onChange={setView} />}>
      {view === "docs" ? (
        <ReferenceFace payload={page.faces.reference} selectedPlanFinding={selectedPlanFinding} names={names} onJump={onJump} />
      ) : view === "ways" ? (
        <WaysFace />
      ) : (
        <>
          <HistoryDrawer
            gitAvailable={history.available}
            entries={history.entries}
            contract={contract}
            expectedRevision={expectedRevision}
            onDiff={onDiff}
            onApplied={onApplied}
            onJump={onJump}
            result={diff && <DiffRows diff={diff} onJump={onJump} />}
          />
        </>
      )}
    </Panel>
  );
}
