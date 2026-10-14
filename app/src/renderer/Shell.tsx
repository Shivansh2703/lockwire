import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { PRODUCT_NAME } from "../shared/branding";
import { MODES, type Mode } from "./Frame";
import { pro } from "./proRegistry";
import { Button, CommandLine, IconButton, PillTabs, StatusPill, ToolPill, Workspace } from "./kit";
import type { ViewInset } from "./scene/buildScene";
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
const NONE: ViewInset = { top: 0, right: 0, bottom: 0, left: 0 };

// What the floating pieces cover: below the tab bar, right of the left panel, left of the right panel, above the view pill.
function measureInset(root: HTMLElement): ViewInset {
  const box = root.getBoundingClientRect();
  const slot = (n: string) => root.querySelector(`.kit-slot-${n}`)?.getBoundingClientRect();
  const top = slot("top"), right = slot("right"), bottom = slot("bottom"), left = slot("left");
  return {
    top: top ? Math.max(0, top.bottom - box.top + GAP) : 0,
    right: right ? Math.max(0, box.right - right.left + GAP) : 0,
    bottom: bottom ? Math.max(0, box.bottom - bottom.top + GAP) : 0,
    left: left ? Math.max(0, left.right - box.left + GAP) : 0,
  };
}

export function Shell({
  robotName, mode, onModeChange, scene, sceneSummary, locked, connected, cliLine, onFit, onOverwrite, notices, wide, center, left, above, tools, children,
}: {
  robotName?: string;
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
  const [inset, setInset] = useState(NONE);
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
        topLeft={robotName ? <span className="shell-chip app-no-drag" title={robotName}>{robotName}</span> : undefined}
        top={locked ? undefined : <div className="app-no-drag shell-tabs"><PillTabs tabs={TABS} value={mode} onChange={onModeChange} label="Mode" />{!pro && <span className="shell-pro-line">Test / Run: {PRODUCT_NAME} Pro</span>}</div>}
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
