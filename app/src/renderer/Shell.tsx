import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { MODES, type Mode } from "./Frame";
import { pro } from "./proRegistry";
import { Button, CommandLine, IconButton, PillTabs, StatusPill, ToolPill, Workspace } from "./kit";
import { FRAME_INSET, type ViewInset } from "./scene/buildScene";
import { currentTheme, initTheme } from "./theme";
import { ThemeToggle } from "./ThemeToggle";
import { VIEW_HINT } from "./OpenFace";
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
  const box = (root.querySelector(".kit-workspace") ?? root).getBoundingClientRect(); // the workspace, not the shell: the drawer narrows it
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
  robotName, onRobot, mode, onModeChange, tabs, scene, sceneSummary, locked, connected, cliLine, onFit, onOverwrite, notices, wide, center, left, above, tools, assistant, drawer, children,
}: {
  robotName?: string;
  /** The robot's name chip opens Design on the whole robot. */
  onRobot?: () => void;
  mode: Mode;
  onModeChange: (mode: Mode) => void;
  /** The tabs that are relevant now (the current one always shows); "More" reveals the rest. Unset: all. */
  tabs?: readonly Mode[];
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
  /** The top-bar Assistant button; no onToggle = no robot open yet, so it is disabled. */
  assistant?: { open: boolean; onToggle?: () => void };
  /** The side drawer (SideDrawer); it sits outside the workspace, which shrinks to make room. */
  drawer?: ReactNode;
  /** The right-hand panel. */
  children?: ReactNode;
}) {
  useState(() => { initTheme(); return currentTheme(); });
  const ref = useRef<HTMLDivElement>(null);
  const [pageCommand, setPageCommand] = useState<string | null>(null);
  const command = pageCommand ?? cliLine ?? null;
  const [inset, setInset] = useState(FRAME_INSET);
  const [hide, setHide] = useState(false); // "Hide panels": only the robot, theme, Connected, fit
  const bare = hide && !center; // never on the open screen (no robot to show)
  const [more, setMore] = useState(false); // "More": every tab, for the rest of the run
  const shown = more || !tabs ? TABS : TABS.filter((t) => t.id === mode || tabs.includes(t.id));
  const [hinted, setHinted] = useState(false); // the gesture hint goes at the first touch of the view
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
  }, [mode, wide, bare, !!children, !!left, !!above, !!assistant?.open]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <CommandCtx.Provider value={setPageCommand}>
    <div className="shell" ref={ref} data-wide={wide || undefined}>
      <div className="app-drag shell-drag" />
      <Workspace
        topLeft={robotName && !bare ? onRobot
          ? <button type="button" className="shell-chip shell-chip-btn app-no-drag" title={`Show all of ${robotName}`} onClick={onRobot}>{robotName}</button>
          : <span className="shell-chip app-no-drag" title={robotName}>{robotName}</span> : undefined}
        top={locked || bare ? undefined : <div className="app-no-drag shell-tabs"><PillTabs tabs={shown} value={mode} onChange={onModeChange} label="Mode" />
          {shown.length < TABS.length && <Button variant="quiet" className="shell-more" title="Show every page" onClick={() => setMore(true)}>More ▾</Button>}</div>}
        topRight={<div className="app-no-drag"><ToolPill label="App">{assistant && !bare && <Button variant="quiet" aria-expanded={assistant.open} aria-controls="side-drawer" disabled={!assistant.onToggle} onClick={assistant.onToggle}>Assistant</Button>}<ThemeToggle /></ToolPill></div>}
        left={bare ? undefined : left}
        right={bare ? undefined : children}
        bottomLeft={locked && connected ? undefined : <div className="app-no-drag"><StatusPill status={connected ? "go" : "bad"}>{connected ? "Connected" : "Not connected"}</StatusPill></div>}
        bottom={locked && !onOverwrite ? undefined : (
          <div className="app-no-drag shell-bottom">
            {!bare && above}
            <ToolPill label="View">
              <IconButton icon="fit" label="Fit view" onClick={onFit} disabled={!onFit} />
              {!bare && tools}
              {!bare && onOverwrite && <Button variant="quiet" className="kit-btn kit-btn-quiet overwrite-from-model" title="The model file changed since it was locked. Lock it again from the file (this replaces the locked values)." onClick={onOverwrite}>Update from model file</Button>}
              {bare ? null : command ? <CommandLine command={command} /> : <span className="shell-bottom-spacer" />}
            </ToolPill>
          </div>
        )}
        bottomRight={locked || center ? undefined : <div className="app-no-drag"><ToolPill label="Panels"><Button variant="quiet" aria-pressed={bare} onClick={() => setHide(!bare)}>◐ {bare ? "Show" : "Hide"} panels</Button></ToolPill></div>}
      >
        {center && <div className="shell-floor" />}
        <div className="shell-scene" onPointerDown={() => setHinted(true)} onWheel={() => setHinted(true)}>{typeof scene === "function" ? scene(inset) : scene}</div>
        <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{sceneSummary ?? ""}</div>
        {/* centred on the uncovered canvas, so a side panel never hides its end */}
        {!locked && !bare && !hinted && <div className="shell-hint" style={{ left: `calc(50% + ${(inset.left - inset.right) / 2}px)` }}>{VIEW_HINT}</div>}
        {center && <div className="shell-center">{center}</div>}
        {/* same uncovered canvas: a notice never sits on the side panel's verdict */}
        {notices && <div className="shell-notices" style={{ left: `calc(50% + ${(inset.left - inset.right) / 2}px)`, maxWidth: `calc(100% - ${inset.left + inset.right}px)` }}>{notices}</div>}
      </Workspace>
      {drawer}
    </div>
    </CommandCtx.Provider>
  );
}
