// The file dialog, main process only (plan v8 line 430: "Open robot"). Electron's native dialog never runs in the renderer -- this module is the only
// caller of `dialog.showOpenDialog`, so a picked path always comes from the OS chooser, never
// from a string the renderer made up.
import { dialog } from "electron";

const ROBOT_FILTERS = [
  { name: "Robot model or lockfile", extensions: ["urdf", "sdf", "xacro", "yaml", "yml"] },
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
