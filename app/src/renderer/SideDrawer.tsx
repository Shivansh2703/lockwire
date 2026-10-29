import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { IconButton, PillTabs } from "./kit";

type Tab = "assistant" | "terminal";
const TABS: { id: Tab; label: string }[] = [{ id: "assistant", label: "Assistant" }, { id: "terminal", label: "Terminal" }];

// A real shell in main (node-pty); this is only its screen and keyboard. One session for the
// app run: it is started once, and a keypress after it exits starts a fresh one.
function TerminalView({ cwd, active }: { cwd?: string; active: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const term = useRef<Terminal | null>(null);
  useEffect(() => {
    const el = host.current!;
    const css = getComputedStyle(document.documentElement);
    const t = new Terminal({
      fontFamily: css.getPropertyValue("--mono").trim() || "Menlo, monospace",
      fontSize: 13,
      cursorBlink: true,
      theme: { background: css.getPropertyValue("--term-bg").trim(), foreground: css.getPropertyValue("--term-fg").trim() },
    });
    const fit = new FitAddon();
    t.loadAddon(fit);
    t.open(el);
    term.current = t;
    let dead = false;
    const start = () => { dead = false; fit.fit(); window.lockwire.startTerminal({ cols: t.cols, rows: t.rows, cwd }).catch((e: unknown) => t.write(`\r\n[could not start a shell: ${e instanceof Error ? e.message : String(e)}]\r\n`)); };
    const refit = () => { if (el.offsetWidth > 0) { fit.fit(); void window.lockwire.resizeTerminal(t.cols, t.rows); } };
    const off = window.lockwire.onTerminalEvent((e) => {
      if (e.type === "data") t.write(e.data);
      else { dead = true; t.write(`\r\n[shell exited (${e.code}); press any key to start a new one]\r\n`); }
    });
    t.onData((d) => { if (dead) start(); else void window.lockwire.writeTerminal(d); });
    const ro = new ResizeObserver(refit);
    ro.observe(el);
    start();
    return () => { ro.disconnect(); off(); t.dispose(); term.current = null; void window.lockwire.killTerminal(); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- the shell starts in the folder that was open then
  useEffect(() => { if (active) term.current?.focus(); }, [active]);
  return <div className="drawer-term" ref={host} />;
}

/** The right-hand slide-out: Assistant | Terminal. Always mounted (hidden when closed) so the shell and its scrollback outlive closing; the Terminal starts the first time its tab is shown. */
export function SideDrawer({ open, onClose, assistant, cwd }: {
  open: boolean;
  onClose: () => void;
  /** The Assistant tab's content (null = no robot open yet). */
  assistant: ReactNode;
  /** The open robot's folder, where a new shell starts. */
  cwd?: string;
}) {
  const [tab, setTab] = useState<Tab>("assistant");
  const [termSeen, setTermSeen] = useState(false);
  const showTerm = open && tab === "terminal";
  useEffect(() => { if (showTerm) setTermSeen(true); }, [showTerm]);
  return (
    <aside
      className="shell-drawer app-no-drag"
      id="side-drawer"
      aria-label="Side drawer"
      hidden={!open}
      // Esc closes, except inside the shell, where it belongs to the program running there.
      onKeyDown={(e) => { if (e.key === "Escape" && !(e.target as HTMLElement).closest(".drawer-term")) onClose(); }}
    >
      {open && <header className="drawer-head">
        <PillTabs tabs={TABS} value={tab} onChange={setTab} label="Drawer" />
        <IconButton icon="close" label="Close drawer" onClick={onClose} />
      </header>}
      <div className="drawer-body" hidden={tab !== "assistant"}>{open && assistant}</div>
      <div className="drawer-body" hidden={tab !== "terminal"}>{termSeen && <TerminalView cwd={cwd} active={showTerm} />}</div>
    </aside>
  );
}
