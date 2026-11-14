import { beforeEach, describe, expect, it, vi } from "vitest";

const send = vi.fn();
const typing = { value: false };
const undo = vi.fn();
const redo = vi.fn();
const ran = vi.fn();
const win = { webContents: { send, undo, redo, executeJavaScript: (s: string) => { ran(s); return Promise.resolve(typing.value); } } };
const openRobotFromMenu = vi.fn();
const openRecentFromMenu = vi.fn();
const stepFromMenu = vi.fn();
const saveCopy = vi.fn();
const exportRobot = vi.fn();
const openExternal = vi.fn();
const showMessageBox = vi.fn();
const state = { robot: null as string | null, recent: [] as string[], names: {} as Record<string, string> };
const closeRobot = vi.fn();
vi.mock("./ipc", () => ({ openRobotFromMenu: (w: unknown) => openRobotFromMenu(w), openRecentFromMenu: (...a: unknown[]) => openRecentFromMenu(...a), stepFromMenu: (...a: unknown[]) => stepFromMenu(...a) }));
vi.mock("./exports", async () => ({ EXPORTS: (await vi.importActual<typeof import("./exports")>("./exports")).EXPORTS, saveCopy: (p: string) => saveCopy(p), exportRobot: (...a: unknown[]) => exportRobot(...a) }));
vi.mock("./menuState", () => ({ currentRobot: () => state.robot, recentRobots: () => state.recent, recentName: (p: string) => state.names[p], closeRobot: () => closeRobot(), clearRecent: vi.fn() }));
vi.mock("./log", () => ({ logFilePath: () => "/tmp/x.log" }));
vi.mock("electron", () => ({
  BrowserWindow: { getFocusedWindow: () => win, getAllWindows: () => [] },
  shell: { showItemInFolder: vi.fn(), openExternal: (u: string) => openExternal(u) },
  dialog: { showMessageBox: (o: unknown) => showMessageBox(o) },
  app: { getVersion: () => "9.9.9" },
  Menu: { buildFromTemplate: (t: unknown) => t },
}));

import { DOCS_URL, OFFLINE_LINE, buildMenu, findScript } from "./menu";

type Item = Electron.MenuItemConstructorOptions;
const MINE = "/home/me/robots/mine/lockwire.yaml";
const top = (): Item[] => buildMenu() as unknown as Item[];
const sub = (item: Item | undefined): Item[] => (item?.submenu ?? []) as Item[];
const menu = (label: string) => sub(top().find((m) => m.label === label));
const find = (items: Item[], label: string) => items.find((i) => i.label === label)!;
const click = (item: Item) => (item.click as () => void)();
const walk = (items: Item[]): Item[] => items.flatMap((i) => [i, ...(Array.isArray(i.submenu) ? walk(i.submenu as Item[]) : [])]);

beforeEach(() => {
  vi.clearAllMocks();
  state.robot = MINE;
  typing.value = false;
  state.recent = [];
  state.names = {};
});

describe("File menu", () => {
  it("Open Robot is Cmd+O, Reveal Logs stays", () => {
    const open = find(menu("File"), "Open Robot…");
    expect(open.accelerator).toBe("CmdOrCtrl+O");
    click(open);
    expect(openRobotFromMenu).toHaveBeenCalledWith(win);
    expect(menu("File").some((i) => i.label === "Reveal Logs")).toBe(true);
  });

  it("Open Recent lists each robot and opens the one clicked; empty says so", () => {
    state.recent = ["/r/b/lockwire.yaml", "/r/a/a.yaml"];
    const items = sub(find(menu("File"), "Open Recent"));
    expect(items.slice(0, 2).map((i) => i.label)).toEqual(["b", "a.yaml"]);
    click(items[1]!);
    expect(openRecentFromMenu).toHaveBeenCalledWith(win, "/r/a/a.yaml");
    state.recent = [];
    expect(sub(find(menu("File"), "Open Recent"))[0]).toMatchObject({ label: "No recent robots", enabled: false });
  });

  it("Open Recent shows the name the start screen shows, not the folder", () => {
    state.recent = ["/r/rover3/lockwire.yaml"];
    state.names = { "/r/rover3/lockwire.yaml": "Resizable rover" };
    expect(sub(find(menu("File"), "Open Recent"))[0]!.label).toBe("Resizable rover");
  });

  it("Close robot sends the window back to the start screen, and is disabled with no robot open", () => {
    click(find(menu("File"), "Close robot"));
    expect(closeRobot).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("lockwire:close-robot");
    state.robot = null;
    expect(find(menu("File"), "Close robot")).toMatchObject({ enabled: false });
  });

  it("Save a Copy works for the user's own robot, not only examples", () => {
    const save = find(menu("File"), "Save a Copy…");
    expect(save.enabled).not.toBe(false);
    click(save);
    expect(saveCopy).toHaveBeenCalledWith(MINE);
  });

  it("Export lists the ten formats that exist, each wired to its own format", () => {
    const items = sub(find(menu("File"), "Export"));
    expect(items.map((i) => i.label)).toEqual(["URDF…", "SDF…", "MJCF…", "STL…", "OBJ…", "COLLADA…", "Parts list (CSV)…", "Power budget (CSV)…", "Report (HTML)…", "Report (PDF)…"]);
    items.forEach(click);
    expect(exportRobot.mock.calls.map(([robot, kind]) => [robot, kind.fmt])).toEqual(["urdf", "sdf", "mjcf", "stl", "obj", "dae", "bom", "budget", "report", "pdf"].map((f) => [MINE, f]));
  });

  it("with no robot open, Save a Copy, Export and its items are disabled with the reason", () => {
    state.robot = null;
    const file = menu("File");
    const gated = [find(file, "Save a Copy…"), find(file, "Export"), ...sub(find(file, "Export"))];
    expect(gated.map((i) => i.enabled)).toEqual(gated.map(() => false));
    expect(find(file, "Export").sublabel).toBe("Open a robot first");
  });
});

describe("Edit menu", () => {
  it("Undo and Redo step the open robot; disabled with none open", async () => {
    click(find(menu("Edit"), "Undo"));
    click(find(menu("Edit"), "Redo"));
    await vi.waitFor(() => expect(stepFromMenu).toHaveBeenCalledTimes(2));
    expect(stepFromMenu.mock.calls).toEqual([[win, "undo-edit"], [win, "redo-edit"]]);
    state.robot = null;
    expect([find(menu("Edit"), "Undo").enabled, find(menu("Edit"), "Redo").enabled]).toEqual([false, false]);
  });

  it("with a text field focused, Undo and Redo act on the text and leave the robot alone", async () => {
    typing.value = true;
    click(find(menu("Edit"), "Undo"));
    click(find(menu("Edit"), "Redo"));
    await vi.waitFor(() => expect([undo.mock.calls.length, redo.mock.calls.length]).toEqual([1, 1]));
    expect(stepFromMenu).not.toHaveBeenCalled();
  });

  it("Find… is Cmd+F and hands the page the same keypress", () => {
    const item = find(menu("Edit"), "Find…");
    expect(item.accelerator).toBe("CmdOrCtrl+F");
    click(item);
    expect(ran).toHaveBeenCalledWith(findScript(process.platform === "darwin"));
  });

  it("has the standard text roles", () => {
    expect(menu("Edit").map((i) => i.role).filter(Boolean)).toEqual(["cut", "copy", "paste", "selectAll"]);
  });
});

describe("View, Window and Help menus", () => {
  it("View has zoom, actual size, full screen, and light/dark which tells the window", () => {
    expect(menu("View").map((i) => i.role).filter(Boolean)).toEqual(["zoomIn", "zoomOut", "resetZoom", "togglefullscreen"]);
    click(find(menu("View"), "Toggle Light/Dark"));
    expect(send).toHaveBeenCalledWith("lockwire:toggle-theme");
  });

  it("Window is the standard window menu", () => {
    expect(top().some((m) => m.role === "windowMenu")).toBe(true);
  });

  it("Help has Keyboard shortcuts, Documentation and About with the version", () => {
    const help = sub(top().find((m) => m.role === "help"));
    click(help[0]!);
    expect(send).toHaveBeenCalledWith("lockwire:show-shortcuts");
    click(find(help, "Documentation"));
    expect(openExternal).toHaveBeenCalledWith(DOCS_URL);
    click(help[3]!);
    expect(showMessageBox.mock.calls[0]![0].message).toContain("9.9.9");
  });
});

describe("Help states offline behaviour", () => {
  it("Help has the offline line and clicking it shows it", () => {
    const item = sub(top().find((m) => m.role === "help")).find((i) => i.label === "Works offline")!;
    click(item);
    expect(showMessageBox.mock.calls.at(-1)![0].message).toBe("Lockwire works offline: checks, edits and exports run on this computer. Only the Assistant needs the internet, using your own API key.");
    expect(OFFLINE_LINE).toContain("offline");
  });
});

describe("nothing in the menu does nothing", () => {
  it.each([MINE, null])("every enabled leaf item has a click or a role (robot: %s)", (robot) => {
    state.robot = robot;
    state.recent = ["/r/a/lockwire.yaml"];
    const leaves = walk(top()).filter((i) => i.type !== "separator" && i.enabled !== false && !i.submenu);
    expect(leaves.length).toBeGreaterThan(15);
    expect(leaves.filter((i) => !i.click && !i.role)).toEqual([]);
  });
});
