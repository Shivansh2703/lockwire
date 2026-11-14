import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let dir = "";
vi.mock("electron", () => ({ app: { getPath: () => dir } }));

import { RECENT_CAP, clearRecent, closeRobot, currentRobot, noteOpened, onMenuState, recentName, recentRobots } from "./menuState";

const robot = (name: string) => { const p = join(dir, `${name}.yaml`); writeFileSync(p, "x"); return p; };
beforeEach(() => { dir = mkdtempSync(join(tmpdir(), "recent-")); });
afterEach(() => { clearRecent(); rmSync(dir, { recursive: true, force: true }); });

describe("recent robots", () => {
  it("after opening A then B lists B then A, and deleting A removes it", () => {
    const [a, b] = [robot("a"), robot("b")];
    noteOpened(a);
    noteOpened(b);
    expect(recentRobots()).toEqual([b, a]);
    rmSync(a);
    expect(recentRobots()).toEqual([b]);
  });

  it("opening A again moves it to the front once", () => {
    const [a, b] = [robot("a"), robot("b")];
    [a, b, a].forEach((p) => noteOpened(p));
    expect(recentRobots()).toEqual([a, b]);
  });

  it("remembers the name given at open, and an older paths-only file still reads", () => {
    const [a, b] = [robot("a"), robot("b")];
    noteOpened(a, "Resizable rover");
    expect(recentName(a)).toBe("Resizable rover");
    writeFileSync(join(dir, "recent.json"), JSON.stringify([b]));
    expect(recentRobots()).toEqual([b]);
    expect(recentName(b)).toBeUndefined();
  });

  it("closing the robot leaves none open and tells the menu once", () => {
    const rebuilt = vi.fn();
    onMenuState(rebuilt);
    noteOpened(robot("a"));
    rebuilt.mockClear();
    closeRobot();
    expect(currentRobot()).toBeNull();
    expect(rebuilt).toHaveBeenCalledTimes(1);
  });

  it("keeps at most ten", () => {
    const paths = Array.from({ length: 12 }, (_, i) => robot(`r${i}`));
    paths.forEach((p) => noteOpened(p));
    expect(recentRobots()).toEqual(paths.slice(2).reverse());
    expect(RECENT_CAP).toBe(10);
    expect(JSON.parse(readFileSync(join(dir, "recent.json"), "utf8"))).toEqual(paths.slice(2).reverse().map((path) => ({ path })));
  });

  it("the open robot is the one just opened, and a change tells the menu to rebuild", () => {
    const rebuilt = vi.fn();
    onMenuState(rebuilt);
    noteOpened(robot("c"));
    expect(currentRobot()).toBe(join(dir, "c.yaml"));
    expect(rebuilt).toHaveBeenCalledTimes(1);
  });

  it("Clear Menu empties the list", () => {
    noteOpened(robot("d"));
    clearRecent();
    expect(recentRobots()).toEqual([]);
  });
});
