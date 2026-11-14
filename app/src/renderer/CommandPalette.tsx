import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

export interface PaletteCommand {
  id: string;
  group: string;
  title: string;
  run: () => void;
}

/** Every word typed must appear in the command's group or title. */
export function matchCommands(commands: readonly PaletteCommand[], query: string): PaletteCommand[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return commands.filter((c) => words.every((w) => `${c.group} ${c.title}`.toLowerCase().includes(w)));
}

/** Cmd+K (Ctrl+K off the Mac): type to find a command, Enter runs it. Nothing shows until it is asked for. */
export function CommandPalette({ commands }: { commands: readonly PaletteCommand[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [at, setAt] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        e.stopPropagation();
        setQuery("");
        setAt(0);
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  const shown = useMemo(() => matchCommands(commands, query), [commands, query]);
  const activeId = open ? shown[at]?.id : undefined;
  useEffect(() => {
    // The lit row stays inside the list while the arrows move past its edge (absent under jsdom).
    if (activeId) document.getElementById(`cmdk-${activeId}`)?.scrollIntoView?.({ block: "nearest" });
  }, [activeId]);
  if (!open) return null;

  const run = (c: PaletteCommand | undefined) => {
    if (!c) return;
    setOpen(false);
    c.run();
  };
  const onKeyDown = (e: ReactKeyboardEvent) => {
    e.stopPropagation(); // the app's own single-key shortcuts must not fire while typing here
    if (e.key === "Escape") setOpen(false);
    else if (e.key === "ArrowDown") { e.preventDefault(); setAt((i) => Math.min(i + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setAt((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); run(shown[at]); }
  };
  const groups = [...new Set(shown.map((c) => c.group))];
  return (
    <div className="cmdk-veil app-no-drag" onMouseDown={(e) => { if (e.target === e.currentTarget) setOpen(false); }}>
      <div className="cmdk" role="dialog" aria-modal="true" aria-label="Commands" onKeyDown={onKeyDown}>
        <input
          ref={input}
          className="cmdk-input"
          role="combobox"
          aria-label="Find a command"
          aria-expanded="true"
          aria-controls="cmdk-list"
          aria-activedescendant={shown[at] ? `cmdk-${shown[at].id}` : undefined}
          placeholder="Type a command"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setAt(0); }}
        />
        {shown.length === 0 ? <p className="cmdk-empty" role="status">No command matches. Try fewer letters.</p> : (
          <ul className="cmdk-list" id="cmdk-list" role="listbox" aria-label="Commands">
            {groups.map((g) => (
              <li key={g} role="presentation">
                <div className="cmdk-group" aria-hidden="true">{g}</div>
                {shown.filter((c) => c.group === g).map((c) => (
                  <button
                    key={c.id}
                    id={`cmdk-${c.id}`}
                    type="button"
                    role="option"
                    className="cmdk-item"
                    aria-selected={shown[at]?.id === c.id}
                    tabIndex={-1}
                    onMouseMove={() => setAt(shown.indexOf(c))}
                    onClick={() => run(c)}
                  >
                    {c.title}
                  </button>
                ))}
              </li>
            ))}
          </ul>
        )}
        <div className="cmdk-foot" aria-hidden="true">
          <span><kbd>↑</kbd> <kbd>↓</kbd> move</span>
          <span><kbd>↵</kbd> run</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
