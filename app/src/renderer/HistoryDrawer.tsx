import { useState, type ReactNode } from "react";

import type { ContractUndoRequest, ContractWriteRequest } from "../shared/ipc-contract";
import type { OnJump } from "./jump";
import { Button, Disclosure, EmptyState, Field, Jumper, PanelFooter, StatusPill, type Status } from "./kit";
import { commitSummary, type LogEntry } from "./historyPayload";

const VERDICT: Record<string, Status> = { GO: "go", GO_WITH_NOTES: "warn", "NO-GO": "bad", REFUSED: "bad", NOT_SCORED: "muted" };

// The same one sentence `vcs.NOT_A_REPO` gives every version-control-leg caller when there
// is no repository (or no `git` on the PATH) to act on. `lockwire log` itself
// does not exist yet at this base (e0-35 is unmerged/bounced) so there is no log-specific
// CLI sentence to mirror one-for-one; this reuses the app's one established no-git sentence
// rather than inventing a second (the app never invents a word).
// Display only: "10 Sep" (the entry's own ISO date stays in the tooltip); unparseable text shows as is.
const shortDate = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${d.getDate()} ${d.toLocaleDateString("en-US", { month: "short" })}`;
};

const NOT_A_REPO = "not a git repository — nothing committed";

export interface HistoryDrawerProps {
  /** `false` outside a repository or with no `git` on the PATH -- entries is ignored. */
  gitAvailable: boolean;
  entries: LogEntry[];
  /**
   * Issues `lockwire diff a [b]`, exactly the CLI's own two-argument shape (`b` omitted
   * means "the file on disk", `cli.py`'s own default) -- this component composes the args,
   * it does not hand the caller a bare sha to reinterpret (one source for the two
   * per-entry commands).
   */
  onDiff: (a: string, b?: string) => void;
  /** The loaded contract. Omitting it keeps this standalone history view read-only. */
  contract?: string;
  expectedRevision?: string;
  onApplied?: (contract: string) => Promise<void>;
  /** The last compare, drawn by the caller. */
  result?: ReactNode;
}

type UndoPlan = { kind: string; scope: string[]; sentence: string };

function undoPlan(reply: unknown): UndoPlan | null {
  if (!reply || typeof reply !== "object") return null;
  const payload = (reply as { payload?: unknown }).payload;
  if (!payload || typeof payload !== "object") return null;
  const plan = payload as Partial<UndoPlan>;
  return typeof plan.kind === "string" && typeof plan.sentence === "string" && Array.isArray(plan.scope)
    ? { kind: plan.kind, sentence: plan.sentence, scope: plan.scope.filter((path): path is string => typeof path === "string") }
    : null;
}

function refusal(reply: unknown): string | null {
  return reply && typeof reply === "object" && typeof (reply as { refusal?: unknown }).refusal === "string"
    ? (reply as { refusal: string }).refusal
    : null;
}

// The Reference face's history drawer: one row per commit touching the contract.
// Every value on a row -- sha, author, date, verdict, message, the fact-diff count --
// is read off the entry (`lockwire log --json`'s own data model, `gitlog.as_json`); none
// of it is derived by parsing `message` or re-scoring the commit here (a rule
// this milestone keeps re-learning).
export function HistoryDrawer({ gitAvailable, entries, onDiff, contract, expectedRevision, onApplied, result, onJump }: HistoryDrawerProps & { onJump?: OnJump }) {
  const [message, setMessage] = useState("");
  const [source, setSource] = useState("HEAD");
  const [plan, setPlan] = useState<(UndoPlan & { source: string }) | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  if (!gitAvailable) {
    return <div className="history-drawer-no-repo"><EmptyState title={NOT_A_REPO} /></div>;
  }

  async function commit() {
    if (!contract || !expectedRevision) {
      setStatus("lockfile revision missing; reload before commit");
      return;
    }
    const request: ContractWriteRequest = {
      command: "commit",
      args: { contract, message, paths: [contract], expected_revision: expectedRevision },
    };
    const reply = await window.lockwire.writeContract(request);
    setStatus(refusal(reply) ?? "commit requested");
  }

  async function planUndo() {
    if (!contract || !expectedRevision) {
      setStatus("lockfile revision missing; reload before undo");
      return;
    }
    const request: ContractUndoRequest = { contract, source, apply: false, expected_revision: expectedRevision };
    const reply = await window.lockwire.undoContract(request);
    const nextPlan = undoPlan(reply);
    setPlan(nextPlan && { ...nextPlan, source });
    setStatus(refusal(reply));
  }

  async function applyUndo() {
    if (!contract || !plan || !expectedRevision) {
      setStatus("lockfile revision missing; reload before undo");
      return;
    }
    const request: ContractUndoRequest = { contract, source: plan.source, apply: true, expected_revision: expectedRevision };
    let reply: unknown;
    try {
      reply = await window.lockwire.undoContract(request);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
      return;
    }
    const refused = refusal(reply);
    if (refused) {
      setStatus(refused);
      return;
    }
    if (onApplied) {
      try {
        await onApplied(contract);
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        setStatus(`undo applied, but reload failed: ${reason}; reload before applying another edit`);
        return;
      }
    }
    setStatus("undo applied");
  }

  const history = entries.length === 0 ? (
    <div className="history-drawer-empty"><EmptyState title="No saved versions" /></div>
  ) : (
    <ul className="history-drawer">
      {entries.map((entry) => (
        <li key={entry.sha} className="history-entry" data-verdict={entry.verdict}>
          <div className="history-entry-header">
            <span className="history-date" title={`${entry.date} · ${entry.sha.slice(0, 7)}`}>{shortDate(entry.date)}</span>
            <span className="history-verdict"><StatusPill status={VERDICT[entry.verdict] ?? "muted"}>{entry.verdict}</StatusPill></span>
            <span className="history-author">{entry.author}</span>
          </div>
          <p className="history-message" title={entry.message}>{commitSummary(entry.message) || entry.message}</p>
          {entry.fact_diff ? (
            <Disclosure summary={<span className="history-fact-count">{entry.fact_diff.changes.length} facts changed</span>}>
              <ul className="history-changes">
                {entry.fact_diff.changes.map((change) => (
                  <li key={change.path} className="history-change">
                    {onJump?.can({ path: change.path }) ? <Jumper label={`Go to ${change.path}`} onClick={() => onJump({ path: change.path })}><code>{change.path}</code></Jumper> : <code>{change.path}</code>}: {JSON.stringify(change.old)} {"→"} {JSON.stringify(change.new)}
                  </li>
                ))}
              </ul>
            </Disclosure>
          ) : (
            <p className="history-no-parent">earliest commit for this file</p>
          )}
          <div className="history-entry-actions">
            {/* the commit against its parent, exactly as `git log -p` does */}
            <Button variant="quiet" className="kit-btn kit-btn-quiet history-diff-parent" onClick={() => onDiff(`${entry.sha}~`, entry.sha)}>
              Changes
            </Button>
            {/* that commit against the file on disk -- `b` omitted is the CLI's own default */}
            <Button variant="quiet" className="kit-btn kit-btn-quiet history-diff-disk" onClick={() => onDiff(entry.sha)}>
              Compare
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );

  return (
    <section className="history-drawer-panel">
      {history}
      {result}
      {contract && (
        <PanelFooter><div className="history-write-actions">
          <Field
            label="Note"
            aria-label="commit message"
            placeholder="What changed"
            value={message}
            onChange={(event: { target: { value: string } }) => setMessage(event.target.value)}
          />
          <Button variant="primary" className="kit-btn kit-btn-primary history-commit" onClick={commit}>Save version</Button>
          <div className="history-undo"><Disclosure summary="Go back to a version">
          <Field
            label="Undo source"
            aria-label="undo source"
            value={source}
            disabled={plan !== null}
            onChange={(event: { target: { value: string } }) => setSource(event.target.value)}
          />
          <Button className="kit-btn kit-btn-secondary history-undo-plan" onClick={planUndo}>Plan undo</Button>
          {plan && (
            <div className="history-undo-plan-result">
              <p className="history-undo-ref">{plan.source}</p>
              <p className="history-undo-scope">{plan.scope.join(", ")}</p>
              <p>{plan.sentence}</p>
              <Button variant="primary" className="kit-btn kit-btn-primary history-undo-apply" onClick={applyUndo}>Apply undo</Button>
            </div>
          )}
          </Disclosure></div>
          {status && <p className="history-write-status">{status}</p>}
        </div></PanelFooter>
      )}
    </section>
  );
}
