import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WHITESPACE_ONLY_ENTRY, X500_HISTORY } from "./history.fixture";
import { HistoryDrawer } from "./HistoryDrawer";
import { commitSummary, type LogEntry } from "./historyPayload";

let container: HTMLDivElement;
let root: Root;
const noopDiff = () => {};

function bridge() {
  const writeContract = vi.fn().mockResolvedValue({ refusal: null });
  const undoContract = vi.fn();
  Object.defineProperty(window, "lockwire", {
    configurable: true,
    value: { writeContract, undoContract } as unknown as typeof window.lockwire,
  });
  return { writeContract, undoContract };
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("HistoryDrawer", () => {
  it("renders one row per commit, reading sha/author/verdict/message off the entry", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={X500_HISTORY} onDiff={noopDiff} />);
    });
    const rows = container.querySelectorAll(".history-entry");
    expect(rows.length).toBe(X500_HISTORY.length);
    const first = X500_HISTORY[0]!;
    expect(container.querySelector(".history-sha")).toBeNull(); // hash lives in the tooltip
    const date = container.querySelector<HTMLElement>(".history-date")!;
    expect(date.title).toBe(`${first.date} · ${first.sha.slice(0, 7)}`);
    expect(date.textContent).not.toMatch(/\d{4}-\d\d-\d\d|T\d\d:/); // short date, never ISO
    expect(container.querySelector(".history-verdict")!.textContent).toBe(first.verdict);
    expect(container.querySelector(".history-author")!.textContent).toBe(first.author);
    expect(container.querySelector(".history-message")!.textContent).toBe(commitSummary(first.message));
  });

  it("shows the fact-diff count for a commit with a parent", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={X500_HISTORY} onDiff={noopDiff} />);
    });
    // X500_HISTORY[0] is the fixture's 6-change commit (real, measured `lockwire log --json`
    // output -- see history.fixture.ts's header comment).
    expect(X500_HISTORY[0]!.fact_diff!.changes.length).toBe(6);
    expect(container.querySelector(".history-fact-count")!.textContent).toBe("6 facts changed");
  });

  it("the earliest commit (fact_diff: null) reads as the earliest, not as zero facts changed", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={X500_HISTORY} onDiff={noopDiff} />);
    });
    // X500_HISTORY[2] is the fixture's earliest commit, whose real `lockwire log --json`
    // output carries `fact_diff: null` (no parent to diff against).
    expect(X500_HISTORY[2]!.fact_diff).toBeNull();
    const rows = container.querySelectorAll(".history-entry");
    const lastRow = rows[rows.length - 1]!;
    expect(lastRow.querySelector(".history-no-parent")!.textContent).toBe("earliest commit for this file");
    expect(lastRow.querySelector(".history-fact-count")).toBeNull();
  });

  it("renders the empty state when no commits touch the file", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={[]} onDiff={noopDiff} />);
    });
    expect(container.querySelector(".history-drawer-empty")!.textContent).toBe("No saved versions");
    expect(container.querySelectorAll(".history-entry").length).toBe(0);
  });

  it("mutation bite: swapping the fact-diff branch for the no-parent branch breaks the count", () => {
    // Simulates the forbidden shape -- rendering "earliest commit" text for a commit that
    // DOES have a fact_diff -- and shows the count assertion above would have caught it.
    const withoutDiff: LogEntry = { ...X500_HISTORY[0]!, fact_diff: null };
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={[withoutDiff]} onDiff={noopDiff} />);
    });
    expect(container.querySelector(".history-fact-count")).toBeNull();
    expect(container.querySelector(".history-no-parent")!.textContent).toBe("earliest commit for this file");
    // The real fixture entry has a fact_diff, so the mutated (null) version above renders
    // differently from what X500_HISTORY[0] itself would render -- proving the branch is live.
    expect(withoutDiff.fact_diff).not.toEqual(X500_HISTORY[0]!.fact_diff);
  });

  // Charter bite 1 (v8's own bite): rendering `git diff` text instead of the structured
  // fact_diff would show SOMETHING for a whitespace-only commit; reading off the payload's
  // own `changes` array must show none.
  it("a whitespace-only commit shows no changed facts", () => {
    expect(WHITESPACE_ONLY_ENTRY.fact_diff!.changed).toBe(false);
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={[WHITESPACE_ONLY_ENTRY]} onDiff={noopDiff} />);
    });
    expect(container.querySelector(".history-fact-count")!.textContent).toBe("0 facts changed");
    expect(container.querySelectorAll(".history-change").length).toBe(0);
  });

  // Charter item 2 + mutation bite 2: an entry opens the commit against its PARENT
  // (`<commit>~ <commit>`), never the commit alone.
  it("an entry opens the commit against its parent", () => {
    const calls: [string, string | undefined][] = [];
    root = createRoot(container);
    act(() => {
      root.render(
        <HistoryDrawer
          gitAvailable
          entries={X500_HISTORY}
          onDiff={(a, b) => calls.push([a, b])}
        />,
      );
    });
    const first = X500_HISTORY[0]!;
    act(() => {
      (container.querySelector(".history-diff-parent") as HTMLButtonElement).click();
    });
    expect(calls).toEqual([[`${first.sha}~`, first.sha]]);
  });

  // Charter item 2, second action: that commit against the file on disk -- `b` omitted,
  // the same default `lockwire diff` itself uses (`cli.py`'s `b: str | None = ... None`).
  it("a second action compares the commit with the file on disk", () => {
    const calls: [string, string | undefined][] = [];
    root = createRoot(container);
    act(() => {
      root.render(
        <HistoryDrawer
          gitAvailable
          entries={X500_HISTORY}
          onDiff={(a, b) => calls.push([a, b])}
        />,
      );
    });
    const first = X500_HISTORY[0]!;
    act(() => {
      (container.querySelector(".history-diff-disk") as HTMLButtonElement).click();
    });
    expect(calls).toEqual([[first.sha, undefined]]);
  });

  // Charter bite 3 (component-level slice of it): outside a repository / no git on the
  // PATH, the drawer shows the same one sentence `vcs.NOT_A_REPO` gives every other caller
  // -- never an empty list that reads as "no history" (the empty-ledger defect e0-32 named).
  it("outside a repository the drawer shows one sentence, not an empty list", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable={false} entries={X500_HISTORY} onDiff={noopDiff} />);
    });
    expect(container.querySelector(".history-drawer-no-repo")!.textContent).toBe(
      "not a git repository — nothing committed",
    );
    // Not the "no commits" empty state -- a different sentence for a different cause.
    expect(container.querySelector(".history-drawer-empty")).toBeNull();
    expect(container.querySelectorAll(".history-entry").length).toBe(0);
  });

  // Mutation bite 3, at component level: swap the no-repo branch out from under the flag
  // and the drawer would fall through to rendering entries anyway, unable to tell "no git"
  // from "has git, has entries" -- the empty-ledger defect this box exists to prevent.
  it("mutation bite: a gitAvailable=false drawer never falls through to the entry list", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable={false} entries={X500_HISTORY} onDiff={noopDiff} />);
    });
    expect(container.querySelectorAll(".history-entry").length).toBe(0);
    expect(container.querySelector(".history-drawer-no-repo")).not.toBeNull();
  });

  it("commits only the displayed contract through the typed write form", async () => {
    const { writeContract } = bridge();
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={[]} onDiff={noopDiff} contract="/robots/x500/lockwire.yaml" expectedRevision="revision" />);
    });
    const message = container.querySelector('[aria-label="commit message"]') as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      setter.call(message, "declare rail — witness: Maya");
      message.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      (container.querySelector(".history-commit") as HTMLButtonElement).click();
    });
    expect(writeContract).toHaveBeenCalledWith({
      command: "commit",
      args: {
        contract: "/robots/x500/lockwire.yaml",
        message: "declare rail — witness: Maya",
        paths: ["/robots/x500/lockwire.yaml"],
        expected_revision: "revision",
      },
    });
  });

  it("shows the required witness format before a commit", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={[]} onDiff={noopDiff} contract="/robots/x500/lockwire.yaml" expectedRevision="revision" />);
    });

    const message = container.querySelector('[aria-label="commit message"]') as HTMLInputElement;
    expect(message.placeholder).toBe("What changed");
    expect(container.querySelector(".history-commit")!.textContent).toBe("Save version");
    expect(container.textContent).not.toMatch(/commit/i);
  });

  it("plans undo before applying the same ref and shows its exact scope", async () => {
    const { undoContract } = bridge();
    undoContract
      .mockResolvedValueOnce({
        refusal: null,
        payload: { kind: "restore", scope: ["lockwire.yaml"], sentence: "restores the contract from HEAD" },
      })
      .mockResolvedValueOnce({ refusal: null, payload: { kind: "restore", scope: ["lockwire.yaml"] } });
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={[]} onDiff={noopDiff} contract="/robots/x500/lockwire.yaml" expectedRevision="revision" />);
    });
    await act(async () => {
      (container.querySelector(".history-undo-plan") as HTMLButtonElement).click();
    });
    expect(undoContract).toHaveBeenCalledWith({ contract: "/robots/x500/lockwire.yaml", source: "HEAD", apply: false, expected_revision: "revision" });
    expect(container.querySelector(".history-undo-ref")!.textContent).toBe("HEAD");
    expect(container.querySelector(".history-undo-scope")!.textContent).toBe("lockwire.yaml");
    await act(async () => {
      (container.querySelector(".history-undo-apply") as HTMLButtonElement).click();
    });
    expect(undoContract).toHaveBeenLastCalledWith({ contract: "/robots/x500/lockwire.yaml", source: "HEAD", apply: true, expected_revision: "revision" });
  });

  it("does not form an undo IPC request without the loaded revision", async () => {
    const { undoContract } = bridge();
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable entries={[]} onDiff={noopDiff} contract="/robots/x500/lockwire.yaml" />);
    });
    await act(async () => {
      (container.querySelector(".history-undo-plan") as HTMLButtonElement).click();
    });
    expect(undoContract).not.toHaveBeenCalled();
    expect(container.querySelector(".history-write-status")?.textContent).toContain("revision missing");
  });

  it("names the no-repository refusal and offers no write controls", () => {
    root = createRoot(container);
    act(() => {
      root.render(<HistoryDrawer gitAvailable={false} entries={[]} onDiff={noopDiff} contract="/robots/x500/lockwire.yaml" />);
    });
    expect(container.querySelector(".history-drawer-no-repo")!.textContent).toBe(
      "not a git repository — nothing committed",
    );
    expect(container.querySelector(".history-write-actions")).toBeNull();
  });
});
