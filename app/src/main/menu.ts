// Application menu. Every enabled item works; items that need an open robot are disabled until one is.
// `shell.showItemInFolder` opens the platform file browser at the log file, selecting it; it
// makes no network call.
import { basename, dirname } from "node:path";
import { BrowserWindow, Menu, app, dialog, shell } from "electron";

import { PRODUCT_NAME } from "../shared/branding";
import { CLOSE_ROBOT_CHANNEL, SHORTCUTS_CHANNEL, THEME_CHANNEL } from "../shared/ipc-contract";
import { EXPORTS, exportRobot, saveCopy } from "./exports";
import { openRecentFromMenu, openRobotFromMenu, stepFromMenu } from "./ipc";
import { logFilePath } from "./log";
import { clearRecent, closeRobot, currentRobot, recentName, recentRobots } from "./menuState";

type Item = Electron.MenuItemConstructorOptions;
export const OFFLINE_LINE = "Lockwire works offline: checks, edits and exports run on this computer. Only the Assistant needs the internet, using your own API key.";
export const DOCS_URL = "https://github.com/Shivansh2703/lockwire#readme";
const focused = () => BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0];

const EDITABLE = "(() => { const e = document.activeElement; return !!e && (e.isContentEditable || /^(INPUT|TEXTAREA)$/.test(e.tagName)); })()";

/** Cmd+Z in a text field undoes the typing; anywhere else it steps the robot's lockfile. */
async function undoOrRedo(win: Electron.BrowserWindow, command: "undo-edit" | "redo-edit"): Promise<void> {
  const typing = await win.webContents.executeJavaScript(EDITABLE).catch(() => false);
  if (typing) return command === "undo-edit" ? win.webContents.undo() : win.webContents.redo();
  await stepFromMenu(win, command);
}

/** Edit > Find…: the menu eats Cmd/Ctrl+F before the page sees it, so it hands the page the same keypress. */
export const findScript = (mac: boolean) => `window.dispatchEvent(new KeyboardEvent("keydown", { key: "f", ${mac ? "metaKey" : "ctrlKey"}: true, cancelable: true }))`;

export function revealLogs(): void {
  shell.showItemInFolder(logFilePath());
}

export function buildRevealLogsMenuItem(): Item {
  return { label: "Reveal Logs", click: () => revealLogs() };
}

export function buildOpenRobotMenuItem(): Item {
  return {
    label: "Open Robot…",
    accelerator: "CmdOrCtrl+O",
    click: () => {
      const target = focused();
      if (target) void openRobotFromMenu(target);
    },
  };
}

function recentMenu(): Item[] {
  const list = recentRobots();
  if (list.length === 0) return [{ label: "No recent robots", enabled: false }];
  return [
    ...list.map((path): Item => ({
      label: recentName(path) ?? (basename(path) === "lockwire.yaml" ? basename(dirname(path)) : basename(path)),
      sublabel: dirname(path),
      click: () => {
        const target = focused();
        if (target) void openRecentFromMenu(target, path);
      },
    })),
    { type: "separator" },
    { label: "Clear Menu", click: () => clearRecent() },
  ];
}

export function buildMenu(): Menu {
  const mac = process.platform === "darwin";
  const robot = currentRobot();
  // Needs an open robot: disabled, with the reason beside it, until one is open.
  const needsRobot = robot === null ? { enabled: false, sublabel: "Open a robot first" } : {};
  const step = (label: string, command: "undo-edit" | "redo-edit", accelerator: string): Item => ({
    label, accelerator, ...needsRobot, click: () => { const w = focused(); if (w) void undoOrRedo(w, command); },
  });
  return Menu.buildFromTemplate([
    ...(mac ? [{ role: "appMenu" as const }] : []),
    { label: "File", submenu: [
      buildOpenRobotMenuItem(),
      { label: "Open Recent", submenu: recentMenu() },
      { label: "Close robot", ...needsRobot, click: () => { closeRobot(); focused()?.webContents.send(CLOSE_ROBOT_CHANNEL); } },
      { type: "separator" },
      { label: "Save a Copy…", accelerator: "CmdOrCtrl+Shift+S", ...needsRobot, click: () => void saveCopy(robot!) },
      { label: "Export", ...needsRobot, submenu: EXPORTS.map((kind): Item => ({ label: kind.label, ...needsRobot, click: () => void exportRobot(robot!, kind) })) },
      { type: "separator" },
      buildRevealLogsMenuItem(),
      { type: "separator" },
      { role: mac ? "close" : "quit" },
    ] },
    { label: "Edit", submenu: [
      step("Undo", "undo-edit", "CmdOrCtrl+Z"),
      step("Redo", "redo-edit", "CmdOrCtrl+Shift+Z"),
      { type: "separator" },
      { label: "Find…", accelerator: "CmdOrCtrl+F", click: () => void focused()?.webContents.executeJavaScript(findScript(mac)).catch(() => undefined) },
      { type: "separator" },
      { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" },
    ] },
    { label: "View", submenu: [
      { role: "zoomIn" }, { role: "zoomOut" }, { role: "resetZoom" },
      { type: "separator" },
      { role: "togglefullscreen" },
      { type: "separator" },
      { label: "Toggle Light/Dark", click: () => focused()?.webContents.send(THEME_CHANNEL) },
    ] },
    { role: "windowMenu" },
    { role: "help", submenu: [
      { label: "Keyboard shortcuts", click: () => focused()?.webContents.send(SHORTCUTS_CHANNEL) },
      { label: "Documentation", click: () => void shell.openExternal(DOCS_URL) },
      { label: "Works offline", click: () => void dialog.showMessageBox({ type: "info", message: OFFLINE_LINE }) },
      { label: `About ${PRODUCT_NAME}`, click: () => void dialog.showMessageBox({ type: "info", message: `${PRODUCT_NAME} ${app.getVersion()}` }) },
    ] },
  ]);
}
