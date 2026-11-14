// macOS takes the menu-bar and Dock name of a dev run (`npm run dev`) from the stock Electron
// bundle's Info.plist, so it reads "Electron". Run from postinstall: stamp the product name on it.
// The packaged app is named by electron-builder and never passes through here.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";

const plist = new URL("../node_modules/electron/dist/Electron.app/Contents/Info.plist", import.meta.url).pathname;
if (process.platform === "darwin" && existsSync(plist)) {
  const name = JSON.parse(readFileSync(new URL("../package.json", import.meta.url))).build.productName;
  for (const key of ["CFBundleName", "CFBundleDisplayName"]) execFileSync("plutil", ["-replace", key, "-string", name, plist]);
}
