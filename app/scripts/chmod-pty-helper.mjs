// node-pty 1.1.0 ships its macOS prebuilt spawn-helper without the executable bit, and its own
// install script does not set it: spawn() then fails with "posix_spawnp failed". Run from
// postinstall, so dev, CI and the packaged app (electron-builder keeps file modes) all get it.
import { chmodSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";

const root = new URL("../node_modules/node-pty/prebuilds", import.meta.url).pathname;
if (existsSync(root)) {
  for (const dir of readdirSync(root)) {
    const helper = join(root, dir, "spawn-helper");
    if (existsSync(helper)) chmodSync(helper, 0o755);
  }
}
