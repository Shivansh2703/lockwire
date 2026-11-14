import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { MODES, type Mode } from "./Frame";
import { pro } from "./proRegistry";
import { Button, CommandLine, IconButton, PillTabs, StatusPill, ToolPill, Workspace } from "./kit";
import { FRAME_INSET, type ViewInset } from "./scene/buildScene";
import { currentTheme, initTheme } from "./theme";
import { ThemeToggle } from "./ThemeToggle";
import { VIEW_HINT } from "./OpenFace";
import { ModeInfo, MoreMenu } from "./MoreMenu";
import { ViewMenu } from "./ViewMenu";
import { CommandPalette, type PaletteCommand } from "./CommandPalette";
import "./theme.css";
import "./polish.css";

// The app shell: the robot fills the window; the pages float over it.
// Hidden Mac title bar: the top row is a drag strip, every control in it opts out.
const LABEL: Record<string, string> = { Reference: "Docs", ...Object.fromEntries(pro?.modes.map((m) => [m.id, m.label]) ?? []) };
const ALL = MODES.map((id) => ({ id, label: LABEL[id] ?? id }));
const IN_MORE: readonly Mode[] = ["Declare", "Reference"]; // reached from the More menu, not a tab
const TABS = ALL.filter((t) => !IN_MORE.includes(t.id));
const MORE = ALL.filter((t) => IN_MORE.includes(t.id));

/** A page publishes its own command to the bottom bar (the bar is fixed; only the text changes). */
const CommandCtx = createContext<((c: string | null) => void) | null>(null);
/** Returns true when a Shell shows the command (the page then draws no line of its own). */
export function useShellCommand(command: string | null): boolean {
  const set = useContext(CommandCtx);
  useEffect(() => { set?.(command); return () => set?.(null); }, [set, command]);
  return !!set;
}

const GAP = 12;

// What the floating pieces cover: the robot is framed in the largest free rectangle around the real panel boxes (a slot's children, else the slot itself), so a short card does not hide the open space under it. Each box is cleared by GAP; the bar floors and a small side margin bound the area.
const SIDE_MARGIN = 24;
// Where notices and the gesture hint sit: beside the panels' full-height slot columns, never over a card (they float at the top, so they must not read the free rect).
export function measureSides(root: HTMLElement): { left: number; right: number } {
  const box = (root.querySelector(".kit-workspace") ?? root).getBoundingClientRect();
  const slot = (n: string) => { const r = root.querySelector(`.kit-slot-${n}`)?.getBoundingClientRect(); return r && r.width > 0 && r.height > 0 ? r : undefined; };
  const right = slot("right"), left = slot("left");
  return { right: right ? box.right - right.left + GAP : SIDE_MARGIN, left: left ? left.right - box.left + GAP : SIDE_MARGIN };
}

export function measureInset(root: HTMLElement): ViewInset {
  const ws = (root.querySelector(".kit-workspace") ?? root).getBoundingClientRect(); // the workspace, not the shell: the drawer narrows it
  const boxes: { l: number; t: number; r: number; b: number }[] = [];
  root.querySelectorAll("[class*='kit-slot-']").forEach((slot) => {
    const rects = Array.from(slot.children, (c) => c.getBoundingClientRect()).filter((r) => r.width > 0 && r.height > 0);
    if (!rects.length) rects.push(slot.getBoundingClientRect());
    for (const r of rects) if (r.width > 0 && r.height > 0) boxes.push({ l: r.left - GAP, t: r.top - GAP, r: r.right + GAP, b: r.bottom + GAP });
  });
  const lo = { x: ws.left + SIDE_MARGIN, y: ws.top + FRAME_INSET.top }, hi = { x: ws.right - SIDE_MARGIN, y: ws.bottom - FRAME_INSET.bottom };
  if (hi.x <= lo.x || hi.y <= lo.y) return { top: FRAME_INSET.top, right: SIDE_MARGIN, bottom: FRAME_INSET.bottom, left: SIDE_MARGIN }; // no layout yet
  const edges = (a: number, z: number, vals: number[]) => [...new Set([a, z, ...vals.filter((v) => v > a && v < z)])].sort((p, q) => p - q);
  const xs = edges(lo.x, hi.x, boxes.flatMap((o) => [o.l, o.r])), ys = edges(lo.y, hi.y, boxes.flatMap((o) => [o.t, o.b]));
  let best = { l: lo.x, t: lo.y, r: hi.x, b: hi.y, area: -1 };
  for (let i = 0; i < xs.length; i++) for (let j = i + 1; j < xs.length; j++) for (let k = 0; k < ys.length; k++) for (let m = k + 1; m < ys.length; m++) {
    const area = (xs[j]! - xs[i]!) * (ys[m]! - ys[k]!);
    if (area <= best.area || boxes.some((o) => o.l < xs[j]! && o.r > xs[i]! && o.t < ys[m]! && o.b > ys[k]!)) continue;
    best = { l: xs[i]!, r: xs[j]!, t: ys[k]!, b: ys[m]!, area };
  }
  return { top: best.t - ws.top, right: ws.right - best.r, bottom: ws.bottom - best.b, left: best.l - ws.left };
}

export function Shell({
  robotName, onRobot, mode, onModeChange, graph, scene, sceneSummary, locked, noView, connected, cliLine, onFit, onOverwrite, notices, wide, center, left, above, tools, assistant, panels, drawer, children,
}: {
  robotName?: string;
  /** The robot's name chip opens Design on the whole robot. */
  onRobot?: () => void;
  mode: Mode;
  onModeChange: (mode: Mode) => void;
  /** GRAPH-1: the one switch to the graph view (the same place on every page), and "Only what this check uses" once a check is chosen. */
  graph?: { on: boolean; onToggle: () => void; isolate?: { on: boolean; onToggle: () => void } };
  /** The RobotView, behind everything; a function gets the px the floating UI covers. */
  scene?: ReactNode | ((inset: ViewInset) => ReactNode);
  /** Text twin of the 3D view, announced politely when it changes (from the same labels as the rows). */
  sceneSummary?: string;
  /** No robot open yet: the page tabs are disabled. */
  locked?: boolean;
  noView?: boolean; // no 3D to turn: no gesture hint
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
  /** The top-bar Assistant button (one tap; the drawer opens on Assistant, Agent or Terminal, whichever was last used); no onToggle = no robot open yet, so it is disabled. */
  assistant?: { open: boolean; onToggle?: () => void };
  /** "Panels": the data panels (Power, Controls, Parts list, Wiring...). Passed only while the robot has data for one; opens the drawer on the last-used one. */
  panels?: { open: boolean; onToggle: () => void };
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
  const [sides, setSides] = useState({ left: FRAME_INSET.left, right: FRAME_INSET.right });
  const [hide, setHide] = useState(false); // "Hide panels": only the robot, theme, Connected, fit
  const bare = hide && !center; // never on the open screen (no robot to show)
  const [hinted, setHinted] = useState(false); // the gesture hint goes at the first touch of the view
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => { setSides((o) => { const n = measureSides(el); return n.left === o.left && n.right === o.right ? o : n; }); setInset((o) => {
      const n = measureInset(el);
      return n.top === o.top && n.right === o.right && n.bottom === o.bottom && n.left === o.left ? o : n;
    }); };
    update();
    window.addEventListener("resize", update);
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    el.querySelectorAll(".kit-slot-top, .kit-slot-right, .kit-slot-bottom, .kit-slot-left").forEach((n) => ro?.observe(n));
    return () => { window.removeEventListener("resize", update); ro?.disconnect(); };
  }, [mode, wide, bare, !!children, !!left, !!above, !!assistant?.open, !!panels?.open]); // eslint-disable-line react-hooks/exhaustive-deps
  // Cmd+K: the same actions the buttons on screen already do, findable by typing.
  const commands: PaletteCommand[] = [
    ...(locked ? [] : ALL.map((t) => ({ id: `mode-${t.id}`, group: "Go to", title: t.label, run: () => onModeChange(t.id) }))),
    ...(onFit ? [{ id: "fit", group: "View", title: "Fit the robot in view", run: onFit }] : []),
    ...(graph ? [{ id: "graph", group: "View", title: graph.on ? "Hide the graph" : "Show the graph", run: graph.onToggle }] : []),
    ...(locked || center ? [] : [{ id: "panels-hide", group: "View", title: bare ? "Show panels" : "Hide panels", run: () => setHide(!bare) }]),
    { id: "theme", group: "View", title: currentTheme() === "dark" ? "Switch to the light theme" : "Switch to the dark theme", run: () => document.querySelector<HTMLButtonElement>(".theme-toggle")?.click() },
    ...(assistant?.onToggle ? [{ id: "assistant", group: "Panels", title: assistant.open ? "Close the Assistant" : "Open the Assistant", run: assistant.onToggle }] : []),
    ...(panels ? [{ id: "panels", group: "Panels", title: panels.open ? "Close the data panels" : "Open the data panels", run: panels.onToggle }] : []),
  ];
  return (
    <CommandCtx.Provider value={setPageCommand}>
    <div className="shell" ref={ref} data-wide={wide || undefined}>
      <CommandPalette commands={commands} />
      <div className="app-drag shell-drag" />
      <Workspace
        topLeft={robotName && !bare ? onRobot
          ? <button type="button" className="shell-chip shell-chip-btn app-no-drag" title={`Show all of ${robotName}`} onClick={onRobot}>{robotName}</button>
          : <span className="shell-chip app-no-drag" title={robotName}>{robotName}</span> : undefined}
        top={locked || bare ? undefined : <div className="app-no-drag shell-tabs"><PillTabs tabs={TABS} value={mode} onChange={onModeChange} label="Mode" />
          <MoreMenu items={MORE} mode={mode} onPick={onModeChange} /><ModeInfo mode={mode} /></div>}
        topRight={<div className="app-no-drag"><ToolPill label="App">{graph && !bare && <>{graph.isolate && <Button variant="quiet" className="kit-btn kit-btn-quiet gv-toggle" aria-pressed={graph.isolate.on} onClick={graph.isolate.onToggle}>Only what this check uses</Button>}<Button variant="quiet" className="kit-btn kit-btn-quiet gv-toggle" aria-pressed={graph.on} onClick={graph.onToggle}>Graph</Button></>}{assistant && !bare && <Button variant="quiet" aria-expanded={assistant.open} aria-controls="side-drawer" disabled={!assistant.onToggle} title={assistant.onToggle ? undefined : "Open a robot first: the Assistant works on the robot you have open."} onClick={assistant.onToggle}>Assistant</Button>}{panels && !bare && <Button variant="quiet" aria-expanded={panels.open} aria-controls="side-drawer" onClick={panels.onToggle}>Panels</Button>}<ThemeToggle /></ToolPill></div>}
        left={bare ? undefined : left}
        right={bare ? undefined : children}
        bottomLeft={locked && connected ? undefined : <div className="app-no-drag"><StatusPill status={connected ? "go" : "bad"}>{connected ? "Connected" : "Not connected"}</StatusPill></div>}
        bottom={locked && !onOverwrite ? undefined : (
          <div className="app-no-drag shell-bottom">
            {!bare && above}
            <ToolPill label="View">
              <IconButton icon="fit" label="Fit view" onClick={onFit} disabled={!onFit} />
              {!bare && onFit && <ViewMenu design={mode === "Design"} name={robotName} />}
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
        {!locked && !noView && !bare && !hinted && !graph?.on && <div className="shell-hint" style={{ left: `calc(50% + ${(sides.left - sides.right) / 2}px)` }}>{VIEW_HINT}</div>}
        {center && <div className="shell-center">{center}</div>}
        {/* same uncovered canvas: a notice never sits on the side panel's verdict */}
        {notices && <div className="shell-notices" style={{ left: `calc(50% + ${(sides.left - sides.right) / 2}px)`, maxWidth: `calc(100% - ${sides.left + sides.right}px)` }}>{notices}</div>}
      </Workspace>
      {drawer}
    </div>
    </CommandCtx.Provider>
  );
}
