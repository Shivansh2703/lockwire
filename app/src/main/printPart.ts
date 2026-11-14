// Print this part: export one link's STL in millimetres and hand it to the system's slicer (Lockwire never talks to
// a printer). The free `export` command writes the folder; we open one file.
import { basename } from "node:path";

import { shell } from "electron";

import { runSidecarCommand } from "./ipcKit";

/** One plain line for the person: what happened. */
export async function printPart(contract: string, link: string): Promise<string> {
  const reply = await runSidecarCommand({ command: "export", args: { contract, format: "stl", units: "mm", force: true } });
  if (reply.refusal !== null) return reply.refusal;
  const written = (reply.payload as { written?: string[] } | null)?.written ?? [];
  const file = written.find((p) => basename(p) === `${link.replace(/[^\w.-]/g, "_")}.stl`);
  if (file === undefined) return `${link} has no shape to print.`;
  if ((await shell.openPath(file)) === "") return `Opened ${basename(file)} in millimetres.`;
  shell.showItemInFolder(file);
  return `No app is set up to open .stl files, so the file is shown in its folder instead: ${basename(file)}.`;
}
