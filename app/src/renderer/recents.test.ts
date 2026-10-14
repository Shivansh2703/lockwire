import { beforeEach, expect, it } from "vitest";
import { pushRecent, readRecents } from "./recents";

beforeEach(() => window.localStorage.clear());

it("keeps the newest 5 opens, one row per path", () => {
  for (let i = 0; i < 7; i++) pushRecent({ path: `/r/${i}.yaml`, name: `r${i}` });
  pushRecent({ path: "/r/5.yaml", name: "r5" });
  expect(readRecents().map((r) => r.name)).toEqual(["r5", "r6", "r4", "r3", "r2"]);
});
