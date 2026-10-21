// The file dialog, main process only (plan v8 line 430: "Open robot"). Electron's native dialog never runs in the renderer -- this module is the only
// caller of `dialog.showOpenDialog`, so a picked path always comes from the OS chooser, never
// from a string the renderer made up.
import { homedir } from "node:os";
import { join } from "node:path";
import { dialog } from "electron";

const ROBOT_FILTERS = [
  { name: "Robot model or lockfile", extensions: ["urdf", "sdf", "xacro", "glb", "gltf", "yaml", "yml"] },
  { name: "All files", extensions: ["*"] },
];

/** Returns the chosen path, or null if the user cancelled. */
export async function pickRobot(): Promise<string | null> {
  const result = await dialog.showOpenDialog({
    title: "Open robot",
    properties: ["openFile"],
    filters: ROBOT_FILTERS,
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0]!;
}

/** Where a "New robot" writes its lockfile: a save dialog, or null if the user cancelled. */
export async function pickNewRobotLocation(name: string): Promise<string | null> {
  const result = await dialog.showSaveDialog({
    title: "Save the new robot",
    defaultPath: join(homedir(), `lockwire-${name}`, "lockwire.yaml"),
    filters: [{ name: "Lockfile", extensions: ["yaml"] }],
  });
  return result.canceled || !result.filePath ? null : result.filePath;
}
