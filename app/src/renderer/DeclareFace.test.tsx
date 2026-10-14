import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  DeclareFace,
  DeclareValues,
  type DeclareFormPayload,
  type DeclareSubmitResult,
  type ReviewSubmission,
} from "./DeclareFace";

const shellCommand = vi.hoisted(() => vi.fn((_c: string | null) => false));
vi.mock("./Shell", () => ({ useShellCommand: shellCommand }));

let container: HTMLDivElement;
let root: Root;

const PAYLOAD: DeclareFormPayload = {
  rows: [
    {
      path: "devices/dev.x500/physical/mass",
      label: "x500 mass",
      group: "x500",
      short: "mass",
      unit: "kg",
      kind: "declared",
      value: 0.5,
      provenance: {
        witness: "Maya",
        source: "datasheet",
        locked_at: "2026-09-09",
      },
    },
    {
      path: "devices/dev.x500/physical/mount_offset",
      label: "x500 mount offset",
      group: "x500",
      short: "mount offset",
      unit: "m",
      kind: "declared",
      value: [0.1, 0.2, 0.3],
      provenance: null,
    },
    {
      path: "model/links/base_link/mass",
      label: "base_link mass",
      group: "base_link",
      short: "mass",
      unit: "kg",
      kind: "declared",
      value: null,
      provenance: null,
    },
    {
      path: "metadata/revision",
      label: "contract revision",
      group: "contract",
      short: "revision",
      unit: null,
      kind: "declared",
      value: null,
      provenance: null,
    },
  ],
  witness: { prefill: "Shivansh" },
};

function Harness({ payload, onSubmit, onReview }: { payload: DeclareFormPayload; onSubmit: never; onReview: (s: ReviewSubmission) => Promise<DeclareSubmitResult> }) {
  const [path, setPath] = useState(payload.rows[0]!.path);
  return (
    <>
      <DeclareValues payload={payload} selectedPath={path} onSelect={setPath} />
      <DeclareFace selectedPath={path} contractPath="lockwire.yaml" payload={payload} onSubmit={onSubmit} onReview={onReview} />
    </>
  );
}

function render(
  payload: DeclareFormPayload,
  onSubmit: (s: unknown) => Promise<DeclareSubmitResult>,
  onReview: (s: ReviewSubmission) => Promise<DeclareSubmitResult> = vi.fn(),
) {
  act(() => {
    root.render(<Harness payload={payload} onSubmit={onSubmit as never} onReview={onReview} />);
  });
}

const expandAll = () => act(() => container.querySelectorAll<HTMLButtonElement>('.kit-tree-chev[aria-expanded="false"]').forEach((c) => c.click()));
const valueRows = () => [...container.querySelectorAll<HTMLButtonElement>(".kit-tree-row:not(.kit-tree-head)")];
const full = (b: HTMLElement) => { const g = b.closest(".kit-tree-group"); return g ? `${g.querySelector(".kit-tree-label span")!.textContent} · ${b.textContent}` : b.textContent!; };
const pick = (label: string) => {
  expandAll();
  act(() => valueRows().find((b) => full(b) === label)!.click());
};
const declareButton = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Declare") as HTMLButtonElement;
const setInput = (el: Element, value: string) => {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
};
const choose = (label: string) =>
  act(() => [...container.querySelectorAll<HTMLInputElement>(".kit-seg input")].find((i) => i.parentElement!.textContent === label)!.click());

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("DeclareFace", () => {
  it("test_no_command_chip_until_there_is_something_to_declare", () => {
    shellCommand.mockClear();
    render(PAYLOAD, vi.fn());
    expect(shellCommand).toHaveBeenLastCalledWith(null);
    act(() => setInput(container.querySelector('input[aria-label="source"]')!, "datasheet"));
    expect(shellCommand.mock.lastCall![0]).toContain("lockwire declare");
  });

  it("test_source_and_witness_inputs_have_placeholders", () => {
    render(PAYLOAD, vi.fn());
    expect(container.querySelector('input[aria-label="source"]')!.getAttribute("placeholder")).toBe("e.g. datasheet p.12");
    expect(container.querySelector('input[aria-label="witness"]')!.getAttribute("placeholder")).toBe("Who checked it");
  });

  it("test_copy_command_button_has_an_accessible_name_once_value_and_source_are_set", () => {
    shellCommand.mockReturnValue(true);
    try {
      render(PAYLOAD, vi.fn());
      const find = () => [...container.querySelectorAll("button.kit-iconbtn")].find((b) => b.textContent === "Copy command");
      expect(find()).toBeUndefined();
      act(() => setInput(container.querySelector('input[aria-label="source"]')!, "datasheet"));
      const copy = find();
      expect(copy?.getAttribute("title")).toBe("Copy command");
    } finally { shellCommand.mockReturnValue(false); }
  });

  it("test_every_label_on_the_declare_face_came_from_the_payload", () => {
    render(PAYLOAD, vi.fn());
    expandAll();
    const rows = valueRows().map(full).sort();
    expect(rows).toEqual(PAYLOAD.rows.map((row) => `${row.group} · ${row.short}`).sort()); // short engine words, full label in the tooltip
    // the right panel is titled by the chosen value
    expect(container.querySelectorAll(".kit-panel-title")[1]?.textContent).toBe(`${PAYLOAD.rows[0]!.short}${PAYLOAD.rows[0]!.group}`); // short name, part as subtitle
  });

  it("test_the_values_list_is_searchable", () => {
    render(PAYLOAD, vi.fn());
    act(() => setInput(container.querySelector('input[type="search"]')!, "mount"));
    expect(valueRows().map(full)).toEqual(["x500 · mount offset"]); // a search opens the groups it matches
  });

  it("test_value_groups_are_drop_downs_closed_except_the_selected_value", () => {
    render(PAYLOAD, vi.fn());
    const groups = PAYLOAD.rows.map((r) => r.group);
    expect(container.querySelectorAll(".kit-tree-group").length).toBe(1); // x500 only; one-row groups are plain rows
    expect(container.querySelectorAll(".kit-tree-chev").length).toBe(1);
    expect(container.querySelectorAll('.kit-tree-group[data-open="true"]').length).toBe(1);
    expect(valueRows().filter((b) => b.closest(".kit-tree-group")).map(full)).toEqual(PAYLOAD.rows.filter((r) => r.group === groups[0]).map((r) => `${r.group} · ${r.short}`));
  });

  it("test_every_number_on_the_declare_face_carries_its_kind", () => {
    render(PAYLOAD, vi.fn());
    const review = container.querySelector(".declare-review")!;
    expect(review.querySelector(".kit-num")?.textContent).toBe("0.5 kg");
    expect(review.querySelector(".kit-kind")?.textContent).toBe(PAYLOAD.rows[0]!.kind);
  });

  it("test_the_witness_is_visible_changeable_and_marked_when_defaulted", () => {
    render(PAYLOAD, vi.fn());
    const input = container.querySelector('input[aria-label="witness"]') as HTMLInputElement;
    expect(input.value).toBe(PAYLOAD.witness.prefill);
    expect(input.readOnly).toBe(false);
    expect(input.closest("label")?.getAttribute("title")).toBe("Who checked it (default)");
    act(() => setInput(input, "someone else"));
    expect(container.querySelector('input[aria-label="witness"]')!.closest("label")?.getAttribute("title")).toBe("Who checked it");
  });

  it("test_an_untouched_prefill_submits_the_exact_value_not_a_rounded_one", async () => {
    const payload: DeclareFormPayload = {
      ...PAYLOAD,
      rows: [{ ...PAYLOAD.rows[0]!, value: 0.123456 }],
    };
    const onSubmit = vi.fn(async (): Promise<DeclareSubmitResult> => ({ written: true }));
    render(payload, onSubmit);
    expect((container.querySelector('input[aria-label="value"]') as HTMLInputElement).value).toBe("0.1235");
    act(() => (container.querySelector('input[aria-label="value"]') as HTMLInputElement).focus());
    expect((container.querySelector('input[aria-label="value"]') as HTMLInputElement).value).toBe("0.123456");
    await act(async () => declareButton().click());
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ value: "0.123456 kg" }));
  });

  it("an empty value reads UNSET, not its kind, and the title is the short name", () => {
    render({ ...PAYLOAD, rows: [PAYLOAD.rows[2]!] }, vi.fn(async () => ({ written: true })));
    expect(container.querySelector(".declare-face .kit-kind")?.textContent).toBe("unset");
    expect(container.querySelector(".declare-title")?.firstChild?.textContent).toBe("mass");
  });

  it("test_the_form_submits_through_the_api_and_nowhere_else", async () => {
    const onSubmit = vi.fn(async (): Promise<DeclareSubmitResult> => ({ written: true }));
    render(PAYLOAD, onSubmit);
    await act(async () => declareButton().click());
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({
      path: PAYLOAD.rows[0]!.path,
      value: "0.5 kg",
      source: "",
      witness: PAYLOAD.witness.prefill,
    });
    expect(container.querySelector(".kit-notice-info")?.textContent).toBe("Written");

    const source = readFileSync(join(process.cwd(), "src/renderer/DeclareFace.tsx"), "utf-8");
    expect(source).not.toMatch(/writeFile|node:fs|require\(["']fs["']\)|\bfs\./);
  });

  it("test_a_refusal_shows_as_an_error_notice", async () => {
    render(PAYLOAD, vi.fn(async () => ({ written: false, refusal: "stale revision" })));
    await act(async () => declareButton().click());
    expect(container.querySelector(".kit-notice-error")?.textContent).toBe("stale revision");
  });

  it("test_the_footer_cli_line_is_the_exact_declare_invocation", () => {
    render(PAYLOAD, vi.fn());
    expect(container.querySelector(".declare-face-cli-line .kit-cli-text")?.textContent).toBe(
      "lockwire declare lockwire.yaml devices/dev.x500/physical/mass '0.5 kg' --source '' --witness " + PAYLOAD.witness.prefill,
    );
  });

  it("test_bare_scalars_get_the_selected_unit_but_other_text_passes_unchanged", async () => {
    const onSubmit = vi.fn(async (): Promise<DeclareSubmitResult> => ({ written: true }));
    render(PAYLOAD, onSubmit);
    const input = container.querySelector('input[aria-label="value"]') as HTMLInputElement;
    for (const [entered, submitted] of [["610", "610 kg"], ["0", "0 kg"], ["0.5 lb", "0.5 lb"], ["1e3", "1e3"]] as const) {
      await act(async () => setInput(input, entered));
      await act(async () => declareButton().click());
      expect(onSubmit).toHaveBeenLastCalledWith(expect.objectContaining({ value: submitted }));
    }
    pick("contract · revision");
    await act(async () => setInput(container.querySelector('input[aria-label="value"]')!, "610"));
    await act(async () => declareButton().click());
    expect(onSubmit).toHaveBeenLastCalledWith(expect.objectContaining({ value: "610" }));
  });

  it("test_review_requires_declared_provenance_explicit_decision_and_question_note", async () => {
    const onReview = vi.fn(async (): Promise<DeclareSubmitResult> => ({ written: true }));
    render(PAYLOAD, vi.fn(), onReview);
    const record = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Record review") as HTMLButtonElement;
    expect(container.querySelector(".declare-review")?.textContent).toContain("datasheet");
    expect(container.querySelector(".declare-review")?.textContent).toContain("Maya");
    expect(record().disabled).toBe(true);
    choose("Question");
    expect(record().disabled).toBe(true);
    act(() => {
      setInput(container.querySelector('textarea[aria-label="Review note"]')!, "verify the mass source");
      setInput(container.querySelector('input[aria-label="Review witness"]')!, "Casey");
    });
    expect(record().disabled).toBe(false);
    await act(async () => record().click());
    expect(onReview).toHaveBeenCalledWith({
      path: PAYLOAD.rows[0]!.path,
      state: "questioned",
      witness: "Casey",
      note: "verify the mass source",
    });
    expect(container.textContent).toContain("Review recorded");

    pick("base_link · mass");
    expect(record().disabled).toBe(true);
    expect(container.querySelector(".declare-review")?.textContent).toContain("nothing to review");
    expect(onReview).toHaveBeenCalledTimes(1);
  });

  it("test_approval_records_the_selected_value_without_inventing_a_note", async () => {
    const onReview = vi.fn(async (): Promise<DeclareSubmitResult> => ({ written: true }));
    render(PAYLOAD, vi.fn(), onReview);
    choose("Approve");
    await act(async () => ([...container.querySelectorAll("button")].find((b) => b.textContent === "Record review") as HTMLButtonElement).click());
    expect(onReview).toHaveBeenCalledWith({
      path: PAYLOAD.rows[0]!.path,
      state: "approved",
      witness: PAYLOAD.witness.prefill,
    });
  });
});
