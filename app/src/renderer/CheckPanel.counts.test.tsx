import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { CheckPanel } from "./CheckPanel";
import FIXTURES from "./checkCounts.fixture.json";

// Real payloads, joined the way App.tsx joins them (check counts and findings, page cards):
// `lockwire start --example <name> --no-check`, then the free engine's `check` and `page`
// replies for that lockfile, machine paths cut to the file name.

let root: Root;
let container: HTMLDivElement;
afterEach(() => { act(() => root.unmount()); container.remove(); });

const screen = (name: keyof typeof FIXTURES) => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<CheckPanel payload={FIXTURES[name] as never} selected={null} onSelect={() => {}} />));
  const notes = [...container.querySelectorAll(".check-list .kit-finding-note")].map((n) => n.textContent);
  return {
    count: container.querySelector(".check-count")!.textContent,
    summary: container.querySelector(".check-list > .kit-disclosure summary")?.textContent,
    advice: notes.filter((n) => n === "Advice, not yet sourced").length,
    outside: [...container.querySelectorAll(".check-list > .kit-disclosure summary")].filter((n) => n.textContent === "Not part of the check").length,
    rows: (kind: string) => container.querySelectorAll(`.check-list .finding-card.severity-${kind}`).length,
  };
};

// What each of the nine examples' count line says, beside the command line's numbers (v/w/ns) it carries.
const LINES: Record<keyof typeof FIXTURES, string> = {
  rover: "1 warning · 1 advice · 3 not scored", // 0/1/3
  quadruped: "1 warning · 2 advice · 13 not scored", // 0/1/13
  "hexapod-param": "1 advice · 1 not scored", // 0/0/1
  arm: "no findings · 1 not scored · 3 accepted", // 0/0/1
  humanoid: "no findings · 3 not scored · 8 accepted", // 0/0/3
  "arm-power": "2 violations · 2 warnings · 1 not scored", // 2/2/1
  drone: "no findings · 8 not scored", // 0/0/8
  "rover-param": "1 advice", // 0/0/0
  "servo-bench": "2 violations · 2 not scored", // 2/0/2
};

// The one rule: every number on the count line has rows that show it, and "no findings" sits beside no
// violation, warning or advice row.
it.each(Object.keys(LINES) as (keyof typeof FIXTURES)[])("%s: the count line never contradicts the lists", (name) => {
  const s = screen(name);
  expect(s.count).toBe(LINES[name]);
  const said = (word: string) => Number(new RegExp(`(\\d+) ${word}`).exec(s.count!)?.[1] ?? 0);
  expect(said("violations?")).toBe(FIXTURES[name].violations);
  expect(said("warnings?")).toBe(FIXTURES[name].warnings);
  expect([said("violations?") > 0, said("warnings?") > 0]).toEqual([s.rows("violation") > 0, s.rows("warning") > 0]);
  expect(said("advice")).toBe(s.advice);
  expect(s.rows("advice")).toBe(s.advice);
  expect(s.count!.startsWith("no findings")).toBe(s.rows("violation") + s.rows("warning") + s.rows("advice") === 0);
  expect(said("not scored")).toBe(Number(/\((\d+)\)/.exec(s.summary ?? "(0)")![1]));
});

it("an advice card wears the quiet info mark, never the warning triangle or its Check word", () => {
  screen("rover");
  const card = container.querySelector(".check-list .finding-card.severity-advice")!;
  expect([card.querySelector(".kit-mark .finding-info")?.textContent, card.querySelector(".kit-icon"), card.querySelector(".kit-finding-word"), card.querySelector(".kit-sr")!.textContent]).toEqual(["i", null, null, "Advice: "]);
});

it("rover: the counted warning, frames without mass, is a row in the list", () => {
  screen("rover");
  expect([...container.querySelectorAll(".check-list .finding-card.severity-warning .kit-finding-title")].map((t) => t.textContent)).toEqual(["1 frame without mass, normal for sensor and mount frames"]);
  expect(container.querySelector(".check-note")).toBeNull();
});

it("rover filtered to the battery: the title counts only the rows the filter keeps", () => {
  screen("rover");
  const part = container.querySelectorAll<HTMLSelectElement>(".check-filter select")[1]!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(part, "battery");
    part.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelector(".check-list > .kit-disclosure summary")!.textContent).toBe("Not scored (2)");
  expect(container.querySelectorAll(".check-list > .kit-disclosure .kit-finding")).toHaveLength(2);
});

// The rows' own counts ("12 checks couldn't run" is 12), read off the screen.
const rowCount = (r: Element) => Number(/^(\d+)/.exec(r.querySelector(".kit-finding-title, .kit-finding")?.textContent ?? "")?.[1] ?? 1);
const notScoredRows = () => {
  const lists = [...container.querySelectorAll(".check-list")];
  const ns = lists.flatMap((l) => [...l.querySelectorAll(":scope > .kit-disclosure")]).find((d) => /^Not scored/.test(d.querySelector("summary")!.textContent!))!;
  const rows = ns ? [...ns.querySelectorAll(".kit-finding")] : [];
  return { n: ns ? Number(/\((\d+)\)/.exec(ns.querySelector("summary")!.textContent!)![1]) : 0, sum: rows.reduce((a, r) => a + rowCount(r), 0), text: rows.map((r) => r.textContent) };
};

it.each(["rover", "quadruped"] as const)("%s: the rows under Not scored add up to its N; the placement row sits outside", (name) => {
  screen(name);
  const { n, sum, text } = notScoredRows();
  expect(n).toBeGreaterThan(0);
  expect(sum).toBe(n);
  expect(text.some((t) => /positions not declared/.test(t!))).toBe(false);
  const placement = [...container.querySelectorAll(".check-list > .kit-disclosure")].find((d) => d.querySelector("summary")!.textContent === "Not part of the check")!;
  expect(placement.textContent).toMatch(/positions not declared/);
  expect(placement.textContent).not.toMatch(/Not part of the check.*Not part of the check/);
});

it("quadruped with the Not scored severity filter: still adds up, placement row still apart", () => {
  screen("quadruped");
  const sel = container.querySelectorAll<HTMLSelectElement>(".check-filter select")[0]!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(sel, "not_scored");
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const { n, sum, text } = notScoredRows();
  expect(n).toBe(13);
  expect(sum).toBe(n);
  expect(text.some((t) => /positions not declared/.test(t!))).toBe(false);
});

// `lockwire check` prints "N accepted · M other accepted" (the scored comparisons it matched, then the rest);
// the screen's one number is the two added, and its Accepted list holds that many rows, each with a spoken witness.
it.each([["arm", 2, 1], ["humanoid", 2, 6]] as const)("%s: the accepted count is the command line's two numbers added", (name, accepted, other) => {
  const { count } = screen(name);
  expect(count).toContain(`${accepted + other} accepted`);
  expect(container.querySelectorAll(".check-list > .kit-disclosure").length).toBeGreaterThan(0);
  const notes = [...container.querySelectorAll(".check-note")].filter((n) => /Witness:/.test(n.textContent!));
  expect(notes).toHaveLength(accepted + other);
  expect(notes[0]!.textContent).toMatch(/\. Witness: none named \(declared, not measured\), 2026-11-06$/);
});

it("rover: the model-file row offers the step its finding names (lockwire start <urdf>), not Declare it; a declarable row keeps Declare it", () => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const onJump = Object.assign(() => {}, { can: () => true });
  act(() => root.render(<CheckPanel payload={FIXTURES.rover as never} selected={null} onSelect={() => {}} onJump={onJump} />));
  const row = (title: string) => [...container.querySelectorAll(".check-list .finding-card")].find((c) => c.querySelector(".kit-finding-title")?.textContent === title)!;
  const model = row("The model file was not re-checked");
  expect([model.querySelector(".kit-cli-text")?.textContent, [...model.querySelectorAll("button")].some((b) => b.textContent === "Declare it")]).toEqual(["lockwire start <urdf>", false]);
  const declarable = [...container.querySelectorAll(".check-list .finding-card.severity-not_scored")].filter((c) => !c.querySelector(".kit-cli"));
  expect(declarable.length).toBeGreaterThan(0);
  expect(declarable.every((c) => [...c.querySelectorAll("button")].some((b) => b.textContent === "Declare it"))).toBe(true);
});

it("rover: rows under Not scored say Not scored, never Not checked", () => {
  screen("rover");
  const words = [...container.querySelectorAll(".check-list .kit-finding-word")].map((w) => w.textContent);
  expect(words).toContain("Not scored");
  expect(words).not.toContain("Not checked");
});

it.each(["rover", "quadruped"] as const)("%s: the placement row, not part of the check, carries no Not scored word; every Not scored word sits under Not scored", (name) => {
  screen(name);
  const lists = [...container.querySelectorAll(".check-list > .kit-disclosure")];
  const placement = lists.find((d) => d.querySelector("summary")!.textContent === "Not part of the check")!;
  expect(placement.querySelectorAll(".kit-finding-word")).toHaveLength(0);
  const words = [...container.querySelectorAll(".kit-finding-word")].filter((w) => w.textContent === "Not scored");
  expect(words.every((w) => /^Not scored/.test(w.closest(".kit-disclosure")!.querySelector("summary")!.textContent!))).toBe(true);
});
