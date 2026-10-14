// Watches a file's mtime on an interval, independent of window focus -- v2 only re-read the
// contract when the BrowserWindow regained focus, which misses an agent (or a terminal) writing
// while the window stays in front of you (plan v8 line 435). This module has no notion of focus
// at all: it is a stdlib poll (fs.stat on a timer), so it fires the same whether the window is
// focused, backgrounded, or minimized.
//
// mtime alone is blind to a same-mtime rewrite: a filesystem's mtime clock has a grain (1s on
// some setups, coarser on others), and a rewrite that lands inside that grain leaves mtimeMs
// unchanged even though the bytes on disk are different -- the watcher would report a fired
// count of 0 while the file had in fact changed. A size check would not fully cover it either
// (an edit that changes content but not byte length -- e.g. one field's value swapped for
// another of the same width -- keeps the size identical too), so this hashes the file's content
// (sha256, stdlib) and fires on either the mtime or the hash differing from what was last seen.
//
// Uses the synchronous fs API deliberately: the contract file is small and this only runs once
// per poll interval, and staying synchronous keeps the whole poll driven by a single timer tick
// with no real-event-loop gap for an fs callback to land in -- the same property that lets a
// fake-timer test assert a same-mtime rewrite fires without waiting on real I/O completion.
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";

export interface ContractWatcher {
  stop(): void;
}

/**
 * Poll `path`'s mtime and content hash every `intervalMs` and call `onChange` the first time
 * either differs from what was previously observed -- never on the initial read, which would
 * fire immediately for every newly-opened contract with nothing having actually changed. The
 * mtime-or-hash comparison IS the guard: remove the hash half and a same-mtime rewrite (the
 * content changing within the timestamp's grain) goes undetected; remove the whole comparison
 * and `onChange` fires on every tick instead of only on a real change.
 */
export function watchContractMtime(path: string, onChange: () => void, intervalMs = 500): ContractWatcher {
  let lastMtimeMs: number | null = null;
  let lastHash: string | null = null;
  let stopped = false;
  let timer: NodeJS.Timeout | undefined;

  function poll(): void {
    if (stopped) return;
    try {
      const info = statSync(path);
      const contents = readFileSync(path);
      const hash = createHash("sha256").update(contents).digest("hex");
      if (lastMtimeMs !== null && (info.mtimeMs !== lastMtimeMs || hash !== lastHash)) {
        onChange();
      }
      lastMtimeMs = info.mtimeMs;
      lastHash = hash;
    } catch {
      // The file may be mid-write (unlink+rename) or briefly missing; skip this tick rather
      // than treating a transient stat/read failure as a change.
    }
    if (!stopped) timer = setTimeout(poll, intervalMs);
  }

  timer = setTimeout(poll, 0);

  return {
    stop() {
      stopped = true;
      clearTimeout(timer);
    },
  };
}
