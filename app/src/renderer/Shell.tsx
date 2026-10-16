import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { PRODUCT_NAME } from "../shared/branding";
import { MODES, type Mode } from "./Frame";
import { pro } from "./proRegistry";
import { Button, CommandLine, IconButton, PillTabs, StatusPill, ToolPill, Workspace } from "./kit";
import { FRAME_INSET, type ViewInset } from "./scene/buildScene";
import { currentTheme, initTheme } from "./theme";
import { ThemeToggle } from "./ThemeToggle";
import "./theme.css";

// The app shell: the robot fills the window; the pages float over it.
// Hidden Mac title bar: the top row is a drag strip, every control in it opts out.
const LABEL: Record<string, string> = { Reference: "Docs", ...Object.fromEntries(pro?.modes.map((m) => [m.id, m.label]) ?? []) };
const TABS = MODES.map((id) => ({ id, label: LABEL[id] ?? id }));

/** A page publishes its own command to the bottom bar (the bar is fixed; only the text changes). */
const CommandCtx = createContext<((c: string | null) => void) | null>(null);
/** Returns true when a Shell shows the command (the page then draws no line of its own). */
export function useShellCommand(command: string | null): boolean {
  const set = useContext(CommandCtx);
  useEffect(() => { set?.(command); return () => set?.(null); }, [set, command]);
  return !!set;
}

const GAP = 12;

// What the floating pieces cover: a side with a panel is cleared by its measured rect + gap, a side without one keeps only a small margin, so the robot centres in the open area that is really visible. Top/bottom keep the bar floors.
const SIDE_MARGIN = 24;
export function measureInset(root: HTMLElement): ViewInset {
  const box = root.getBoundingClientRect();
  const slot = (n: string) => { const r = root.querySelector(`.kit-slot-${n}`)?.getBoundingClientRect(); return r && r.width > 0 && r.height > 0 ? r : undefined; };
  const top = slot("top"), right = slot("right"), bottom = slot("bottom"), left = slot("left");
  return {
    top: Math.max(FRAME_INSET.top, top ? top.bottom - box.top + GAP : 0),
    right: right ? box.right - right.left + GAP : SIDE_MARGIN,
    bottom: Math.max(FRAME_INSET.bottom, bottom ? box.bottom - bottom.top + GAP : 0),
    left: left ? left.right - box.left + GAP : SIDE_MARGIN,
  };
}

export function Shell({
  robotName, onRobot, mode, onModeChange, scene, sceneSummary, locked, connected, cliLine, onFit, onOverwrite, notices, wide, center, left, above, tools, children,
}: {
  robotName?: string;
  /** The robot's name chip opens Design on the whole robot. */
  onRobot?: () => void;
  mode: Mode;
  onModeChange: (mode: Mode) => void;
  /** The RobotView, behind everything; a function gets the px the floating UI covers. */
  scene?: ReactNode | ((inset: ViewInset) => ReactNode);
  /** Text twin of the 3D view, announced politely when it changes (from the same labels as the rows). */
  sceneSummary?: string;
  /** No robot open yet: the page tabs are disabled. */
  locked?: boolean;
  connected: boolean;
  cliLine?: string | null;
  onFit?: () => void;
  onOverwrite?: () => void;
  notices?: ReactNode;
  /** A wide reading panel (--panel-w-wide); every other right panel is --panel-w. */
  wide?: boolean;
  /** Centred over the canvas (the Open panel). */
  center?: ReactNode;
  /** The left-hand panel (Design: Parts). */
  left?: ReactNode;
  /** Above the bottom tool pill (Design: lens tabs). */
  above?: ReactNode;
  /** Extra icon buttons in the bottom tool pill. */
  tools?: ReactNode;
  /** The right-hand panel. */
  children?: ReactNode;
}) {
  useState(() => { initTheme(); return currentTheme(); });
  const ref = useRef<HTMLDivElement>(null);
  const [pageCommand, setPageCommand] = useState<string | null>(null);
  const command = pageCommand ?? cliLine ?? null;
  const [inset, setInset] = useState(FRAME_INSET);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setInset((o) => {
      const n = measureInset(el);
      return n.top === o.top && n.right === o.right && n.bottom === o.bottom && n.left === o.left ? o : n;
    });
    update();
    window.addEventListener("resize", update);
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    el.querySelectorAll(".kit-slot-top, .kit-slot-right, .kit-slot-bottom, .kit-slot-left").forEach((n) => ro?.observe(n));
    return () => { window.removeEventListener("resize", update); ro?.disconnect(); };
  }, [mode, wide, !!children, !!left, !!above]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <CommandCtx.Provider value={setPageCommand}>
    <div className="shell" ref={ref} data-wide={wide || undefined}>
      <div className="app-drag shell-drag" />
      <Workspace
        topLeft={robotName ? onRobot
          ? <button type="button" className="shell-chip shell-chip-btn app-no-drag" title={`Show all of ${robotName}`} onClick={onRobot}>{robotName}</button>
          : <span className="shell-chip app-no-drag" title={robotName}>{robotName}</span> : undefined}
        top={locked ? undefined : <div className="app-no-drag shell-tabs"><PillTabs tabs={TABS} value={mode} onChange={onModeChange} label="Mode" />{!pro && ["Test", "Run"].map((t) => (
          <span key={t} className="shell-pro-tab" aria-disabled="true" title={`Test and Run are in ${PRODUCT_NAME} Pro`}>{t}<span className="shell-pro-badge">Pro</span></span>
        ))}</div>}
        topRight={<div className="app-no-drag"><ToolPill label="App"><ThemeToggle /></ToolPill></div>}
        left={left}
        right={children}
        bottomLeft={locked && connected ? undefined : <div className="app-no-drag"><StatusPill status={connected ? "go" : "bad"}>{connected ? "Connected" : "Not connected"}</StatusPill></div>}
        bottom={locked && !onOverwrite ? undefined : (
          <div className="app-no-drag shell-bottom">
            {above}
            <ToolPill label="View">
              <IconButton icon="fit" label="Fit view" onClick={onFit} disabled={!onFit} />
              {tools}
              {onOverwrite && <Button variant="quiet" className="kit-btn kit-btn-quiet overwrite-from-model" onClick={onOverwrite}>Overwrite from model</Button>}
              {command ? <CommandLine command={command} /> : <span className="shell-bottom-spacer" />}
            </ToolPill>
          </div>
        )}
      >
        {center && <div className="shell-floor" />}
        <div className="shell-scene">{typeof scene === "function" ? scene(inset) : scene}</div>
        <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{sceneSummary ?? ""}</div>
        {center && <div className="shell-center">{center}</div>}
        {notices && <div className="shell-notices">{notices}</div>}
      </Workspace>
    </div>
    </CommandCtx.Provider>
  );
}
