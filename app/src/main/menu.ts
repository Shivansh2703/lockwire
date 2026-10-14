// Application menu: macOS app/edit/window roles, File > Open Robot... (Cmd+O), and Reveal Logs.
// `shell.showItemInFolder` opens the platform file browser at the log file, selecting it; it
// makes no network call.
import { BrowserWindow, Menu, shell } from "electron";

import { openRobotFromMenu } from "./ipc";
import { logFilePath } from "./log";

export function revealLogs(): void {
  shell.showItemInFolder(logFilePath());
}

export function buildRevealLogsMenuItem(): Electron.MenuItemConstructorOptions {
  return { label: "Reveal Logs", click: () => revealLogs() };
}

export function buildOpenRobotMenuItem(): Electron.MenuItemConstructorOptions {
  return {
    label: "Open Robot\u2026",
    accelerator: "CmdOrCtrl+O",
    click: () => {
      const target = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0];
      if (target) void openRobotFromMenu(target);
    },
  };
}

export function buildMenu(): Menu {
  const mac = process.platform === "darwin";
  return Menu.buildFromTemplate([
    ...(mac ? [{ role: "appMenu" as const }] : []),
    { label: "File", submenu: [buildOpenRobotMenuItem(), buildRevealLogsMenuItem(), { type: "separator" }, { role: mac ? "close" : "quit" }] },
    { role: "editMenu" },
    { role: "windowMenu" },
  ]);
}
