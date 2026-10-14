import { describe, expect, it, vi } from "vitest";

const win = {};
const openRobotFromMenu = vi.fn();
vi.mock("./ipc", () => ({ openRobotFromMenu: (w: unknown) => openRobotFromMenu(w) }));
vi.mock("./log", () => ({ logFilePath: () => "/tmp/x.log" }));
vi.mock("electron", () => ({
  BrowserWindow: { getFocusedWindow: () => win, getAllWindows: () => [] },
  shell: { showItemInFolder: vi.fn() },
  Menu: { buildFromTemplate: (t: unknown) => t },
}));

import { buildMenu } from "./menu";

describe("buildMenu", () => {
  it("test_file_menu_has_open_robot_on_cmd_o_and_keeps_reveal_logs", () => {
    const menu = buildMenu() as unknown as Electron.MenuItemConstructorOptions[];
    const file = menu.find((m) => m.label === "File")!;
    const items = file.submenu as Electron.MenuItemConstructorOptions[];
    const open = items.find((i) => i.label?.startsWith("Open Robot"))!;
    expect(open.accelerator).toBe("CmdOrCtrl+O");
    expect(items.some((i) => i.label === "Reveal Logs")).toBe(true);
    (open.click as () => void)();
    expect(openRobotFromMenu).toHaveBeenCalledWith(win);
  });
});
