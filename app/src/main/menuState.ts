// What the application menu needs to know: which robot is open, and the last robots opened.
// The recent list is newest first, capped, in userData/recent.json (as assistant.json): each robot's path and the name the start screen shows for it.
import { AsyncLocalStorage } from "node:async_hooks";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { app } from "electron";

export const RECENT_CAP = 10;
let current: string | null = null;
const listeners = new Set<() => void>();
// Close robot bumps `closes`; a request started before a Close carries the older count and cannot set the open robot again.
let closes = 0;
const begun = new AsyncLocalStorage<number>();
const stale = () => (begun.getStore() ?? closes) !== closes;

/** Runs one request (an IPC call or a menu open) remembering how many Closes came before it. */
export function asRequest<T>(fn: () => T): T {
  return begun.run(closes, fn);
}
const file = () => join(app.getPath("userData"), "recent.json");

type Entry = { path: string; name?: string };

function stored(): Entry[] {
  try {
    const list: unknown = JSON.parse(readFileSync(file(), "utf8"));
    if (!Array.isArray(list)) return [];
    return list.flatMap((e): Entry[] => typeof e === "string" ? [{ path: e }] : typeof e?.path === "string" ? [{ path: e.path, ...(typeof e.name === "string" ? { name: e.name } : {}) }] : []);
  } catch {
    return [];
  }
}

function save(list: Entry[]): void {
  try {
    writeFileSync(file(), JSON.stringify(list));
  } catch {
    // no persistence; the menu still shows this session's list
  }
  for (const cb of listeners) cb();
}

/** Newest first; a file that is gone is left out. */
export function recentRobots(): string[] {
  return stored().map((e) => e.path).filter((p) => existsSync(p));
}

/** The name the start screen shows for this robot, when it was recorded. */
export function recentName(path: string): string | undefined {
  return stored().find((e) => e.path === path)?.name;
}

/** The robot just opened: current, and first in the recent list. */
export function noteOpened(path: string, name?: string): void {
  if (stale()) return; // the window was closed while this open ran: it shows the start screen, not this robot
  current = path;
  save([{ path, ...(name ? { name } : {}) }, ...stored().filter((e) => e.path !== path)].slice(0, RECENT_CAP));
}

/** The window now shows this robot (not an open: the recent list is untouched). */
export function setCurrentRobot(path: string): void {
  if (path === current || stale()) return;
  current = path;
  for (const cb of listeners) cb();
}

/** File > Close robot: the window is back on the start screen, so no robot is open. */
export function closeRobot(): void {
  closes++;
  if (current === null) return;
  current = null;
  for (const cb of listeners) cb();
}

export function clearRecent(): void {
  save([]);
}

export function currentRobot(): string | null {
  return current;
}

export function onMenuState(cb: () => void): void {
  listeners.add(cb);
}
