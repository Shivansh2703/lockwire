import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef, useState, type ReactNode } from "react";

import type { PagePayload } from "../shared/api.gen";
import type { AssistantProposal, AssistantSettings } from "../shared/ipc-contract";
import { AllowScreen, Diff } from "./AssistantPanel";
import { Button, Field, IconButton, Notice, Panel, PillTabs } from "./kit";
import { visiblePanels } from "./panelRegistry";
import { isPowerSpec } from "./powerSpec";

type Tab = string; // "assistant" | "agent" | "terminal" | a registered panel id
// Two views, never one wrapped row: the Assistant view (what 0.2.9 had) and the Panels view (data panels).
const TABS: { id: Tab; label: string }[] = [{ id: "assistant", label: "Assistant" }, { id: "agent", label: "Agent" }, { id: "terminal", label: "Terminal" }];

type Item = string | { card: AssistantProposal; check: string };

// The Agent (main/agent.ts): reads the robot's folder and lockwire's read tools, streams what it
// says, and proposes edits as cards. "Review and sign" hands a card to the Assistant tab's own
// signing step; the Agent itself changes nothing. It uses the Assistant's key and per-robot switch.
function AgentView({ contract, revision, shown, onReview }: { contract: string; revision: string; shown: boolean; onReview: (p: AssistantProposal) => void }) {
  const [text, setText] = useState("");
  const [out, setOut] = useState<Item[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [usage, setUsage] = useState("");
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  useEffect(() => { if (shown) void window.lockwire.getAssistantSettings().then(setSettings); }, [contract, shown]); // re-read on show: the Assistant tab may have allowed it
  useEffect(() => window.lockwire.onAgentEvent(({ contract: c, event: e }) => {
    if (c !== contract) return;
    if (e.type === "text") setOut((o) => [...o, e.text]);
    else if (e.type === "tool") setOut((o) => [...o, `Using ${e.name.replace(/^mcp__(lockwire|propose)__/, "")}`]);
    else if (e.type === "proposal") setOut((o) => [...o, { card: e.proposal, check: e.check }]);
    else if (e.type === "done") { setBusy(false); if (e.error) setError(e.error); if (e.usage) setUsage(e.usage); }
  }), [contract]);
  const send = async () => {
    setError("");
    setOut((o) => [...o, `> ${text}`]);
    const r = await (isPowerSpec(text) ? window.lockwire.designPower(contract, text, revision) : window.lockwire.startAgent(contract, text, revision));
    if (r.ok) { setBusy(true); setText(""); } else { setError(r.error); void window.lockwire.getAssistantSettings().then(setSettings); } // off or no key: re-read, and the key screen takes over
  };
  if (settings && (!settings.hasKey || !settings.enabledRobots.includes(contract))) {
    return <Panel title="Agent"><AllowScreen contract={contract} hasKey={settings.hasKey} onSaved={setSettings} /></Panel>;
  }
  return (
    <Panel title="Agent">
      <div className="assistant-col">
        <span className="assistant-note">Powered by Claude. It reads and proposes; nothing changes until you sign. To design a power supply, type its cells and rails, like 4S, 5 V 3 A, 12 V 1 A. On the Resizable rover or the Power-focused hexapod, describe a robot to build it, like a four-wheel rover about 40 cm long that carries 2 kg.</span>
        {out.map((t, i) => typeof t !== "string" ? (
          <div key={i} className="assistant-card">
            <span>The Agent proposes <strong>{t.card.command}</strong>. Nothing changes until you sign.</span>
            <Diff diff={t.card.diff} />
            <span className="assistant-note">{t.check}</span>
            <Button variant="primary" onClick={() => onReview(t.card)}>Review and sign</Button>
          </div>
        ) : t.startsWith("Plan:") ? (
          <details key={i} className="assistant-turn"><summary>Plan</summary>{t.slice(5).trim()}</details>
        ) : <p key={i} className="assistant-turn">{t}</p>)}
        {busy && <span className="assistant-note" role="status">Working</span>}
        {error && <Notice kind="error">{error}</Notice>}
        {usage && <span className="assistant-note">{usage}</span>}
        <Field label="Ask the Agent" control="textarea" rows={3} value={text} onChange={(e: { target: { value: string } }) => setText(e.target.value)} />
        <div className="assistant-row">
          <Button variant="primary" disabled={!text.trim() || busy} onClick={() => void send()}>Send</Button>
          {busy && <Button variant="quiet" onClick={() => void window.lockwire.stopAgent(contract)}>Stop</Button>}
        </div>
      </div>
    </Panel>
  );
}

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
  return (
    <div className="drawer-termbox">
      <p className="drawer-warn">This is a real shell on your computer. Commands run as you.</p>
      <div className="drawer-term" ref={host} />
    </div>
  );
}

/** The right-hand slide-out: two views, each on one row: Assistant | Agent | Terminal, and the robot's data panels. Always mounted (hidden when closed) so the shell and its scrollback outlive closing; the Terminal starts the first time its tab is shown. */
export function SideDrawer({ open, onClose, assistant, cwd, contract, revision, onReview, page, onDeclare, onAddWire, want, onTab }: {
  open: boolean;
  onClose: () => void;
  /** The Assistant tab's content (null = no robot open yet). */
  assistant: ReactNode;
  /** The open robot's folder, where a new shell starts. */
  cwd?: string;
  /** The open robot's contract and its revision, for the Agent tab's proposals. */
  contract?: string;
  revision?: string;
  /** "Review and sign" on an Agent card: the Assistant tab's signing step takes it from here. */
  onReview?: (p: AssistantProposal) => void;
  /** The opened robot's page; registered panels (panelRegistry) show once it has something for them. */
  page?: PagePayload | null;
  /** The one declare door for panels that write a fact (App's `declareFromPanel`). */
  onDeclare?: (path: string, value: string, source: string) => Promise<string | null>;
  /** The one wire door for the Wiring panel (App's `addWireFromPanel`). */
  onAddWire?: (id: string, from: string, to: string) => Promise<string | null>;
  /** A new object each time something outside asks for a tab: a tab id (the Agent button of a Pro screen), "@assist" (the Assistant button: last Assistant-view tab) or "@panels" (the Panels button: last data panel). */
  want?: { id: string };
  /** Told the current tab, so the top-bar buttons know which view is showing. */
  onTab?: (t: Tab) => void;
}) {
  const [tab, setTab] = useState<Tab>("assistant");
  const [termSeen, setTermSeen] = useState(false);
  const extra = visiblePanels(page);
  const isPanel = extra.some((p) => p.id === tab);
  const last = useRef({ assist: "assistant", panel: "" });
  useEffect(() => { if (isPanel) last.current.panel = tab; else last.current.assist = tab; onTab?.(tab); }, [tab, isPanel]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (want) setTab(want.id === "@assist" ? last.current.assist : want.id === "@panels" ? (extra.some((p) => p.id === last.current.panel) ? last.current.panel : extra[0]?.id ?? "assistant") : want.id); }, [want]); // eslint-disable-line react-hooks/exhaustive-deps
  const tabs = isPanel ? extra.map((p) => ({ id: p.id, label: p.label })) : TABS;
  useEffect(() => { if (![...TABS, ...extra].some((t) => t.id === tab)) setTab("assistant"); }); // a panel whose data went away
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
        <PillTabs tabs={tabs} value={tab} onChange={setTab} label="Drawer" />
        <IconButton icon="close" label="Close drawer" onClick={onClose} />
      </header>}
      <div className="drawer-body" hidden={tab !== "assistant"}>{open && assistant}</div>
      <div className="drawer-body" hidden={tab !== "agent"}>{open && contract && <AgentView contract={contract} revision={revision ?? ""} shown={tab === "agent"} onReview={(p) => { onReview?.(p); setTab("assistant"); }} />}</div>
      <div className="drawer-body" hidden={tab !== "terminal"}>{termSeen && <TerminalView cwd={cwd} active={showTerm} />}</div>
      {extra.map((p) => <div className="drawer-body" key={p.id} hidden={tab !== p.id}>{open && page && <p.Body page={page} contract={contract} revision={revision} onDeclare={onDeclare} onAddWire={onAddWire} />}</div>)}
    </aside>
  );
}
