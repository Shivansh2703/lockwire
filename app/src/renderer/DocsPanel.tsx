import { useState } from "react";

import type { DiffPayload, Finding, LogPayload, PagePayload } from "../shared/api.gen";
import { HistoryDrawer } from "./HistoryDrawer";
import { EmptyState, Panel, Segmented } from "./kit";
import { ReferenceFace } from "./ReferenceFace";

type View = "docs" | "history";
const VIEWS: { value: View; label: string }[] = [{ value: "docs", label: "Docs" }, { value: "history", label: "History" }];
const show = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

// What a compare changed, one row per fact: path: old -> new (the payload's own values).
function DiffRows({ diff }: { diff: DiffPayload }) {
  const rows = [
    ...diff.changes.map((c) => `${c.path}: ${show(c.old)} → ${show(c.new)}`),
    ...diff.added.map((e) => `added ${e.kind} ${e.id}`),
    ...diff.removed.map((e) => `removed ${e.kind} ${e.id}`),
    ...diff.findings_new.map((f) => `new finding: ${f.quantity} (${f.severity})`),
    ...diff.findings_gone.map((f) => `finding gone: ${f.quantity} (${f.severity})`),
  ];
  return (
    <div className="history-diff-result" data-changed={diff.changed}>
      {rows.length === 0 ? <EmptyState title="No changes" /> : rows.map((r, i) => <code key={i} className="history-diff-row">{r}</code>)}
    </div>
  );
}

// Docs: one wide panel, Docs | History. The shell dims the robot behind it.
export function DocsPanel({
  page, selectedPlanFinding, history, contract, expectedRevision, diff, onDiff, onApplied,
}: {
  page: PagePayload;
  selectedPlanFinding: Finding | null;
  history: { available: boolean; entries: LogPayload };
  contract?: string;
  expectedRevision?: string;
  diff: DiffPayload | null;
  onDiff: (a: string, b?: string) => void;
  onApplied: (contract: string) => Promise<void>;
}) {
  const [view, setView] = useState<View>("docs");
  return (
    <Panel title="Docs" actions={<Segmented label="Docs view" options={VIEWS} value={view} onChange={setView} />}>
      {view === "docs" ? (
        <ReferenceFace payload={page.faces.reference} selectedPlanFinding={selectedPlanFinding} />
      ) : (
        <>
          <HistoryDrawer
            gitAvailable={history.available}
            entries={history.entries}
            contract={contract}
            expectedRevision={expectedRevision}
            onDiff={onDiff}
            onApplied={onApplied}
            result={diff && <DiffRows diff={diff} />}
          />
        </>
      )}
    </Panel>
  );
}
