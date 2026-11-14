import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddPartForm, attachAfterDevice, structureFields } from "./AddPartForm";
import { setUnits } from "./units";

let root: Root;
let container: HTMLDivElement;

afterEach(() => { act(() => root.unmount()); container.remove(); });

function type(label: string, value: string) {
  const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => { set.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
}

const field = (label: string) => container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
const choose = (label: string, value: string) => act(() => { const el = field(label); el.value = value; el.dispatchEvent(new Event("change", { bubbles: true })); });
const addButton = () => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Add part")!;
const press = async () => act(async () => { addButton().dispatchEvent(new MouseEvent("click", { bubbles: true })); });
const mountForm = (catalog: { rails: string[]; parts: never[] }, onAdd = vi.fn().mockResolvedValue({ written: true }), witness = "") => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<AddPartForm witness={witness} catalog={catalog} onAdd={onAdd} />));
  return onAdd;
};

describe("AddPartForm", () => {
  it("submits name, draw, rail and witness with source defaulting to datasheet", async () => {
    const onAdd = vi.fn().mockResolvedValue({ written: true });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<AddPartForm witness="" catalog={{ rails: ["rail.servo-5v"], parts: [] }} onAdd={onAdd} />));
    const button = () => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Add part")!;
    expect(button().disabled).toBe(false);
    type("part name", "dev.servo5");
    type("power draw", "7");
    const rail = container.querySelector<HTMLSelectElement>('[aria-label="rail"]')!;
    act(() => { rail.value = "rail.servo-5v"; rail.dispatchEvent(new Event("change", { bubbles: true })); });
    type("part checked by", "Maya");
    await act(async () => { button().dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(onAdd).toHaveBeenCalledWith({ id: "dev.servo5", draw: "7", rail: "rail.servo-5v", source: "datasheet", witness: "Maya" });
    expect(container.textContent).toContain("Part added");
  });
});

describe("AddPartForm catalog picker", () => {
  const num = (key: string, value: number, unit: string, kind: "datasheet" | "assumed" = "datasheet") => ({ key, value, unit, kind, source: `src ${key}` });
  const catalog = {
    rails: ["rail.5v", "rail.12v"],
    parts: [
      { id: "cat.servo", name: "Servo", numbers: [num("draw_max", 7, "W", "assumed")], fits: ["rail.5v"] },
      { id: "cat.mystery", name: "Mystery", numbers: [num("draw_max", 3, "W")], fits: [] },
    ],
  };
  const pickPart = (value: string) => {
    const select = container.querySelector<HTMLSelectElement>('[aria-label="catalog part"]')!;
    act(() => { select.value = value; select.dispatchEvent(new Event("change", { bubbles: true })); });
  };
  const railValue = () => container.querySelector<HTMLSelectElement>('[aria-label="rail"]')!.value;

  it("preselects the rail whose voltage fits, badges assumed numbers, and never guesses", () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<AddPartForm witness="" catalog={catalog} onAdd={vi.fn()} />));
    pickPart("cat.servo");
    expect(railValue()).toBe("rail.5v"); // not rail.12v
    expect(container.textContent).toContain("Power draw 7 W (assumed) · src source"); // one plain line; the raw note sits under Sources
    expect(container.textContent).not.toContain("No rail matches");
    pickPart("cat.mystery");
    expect(railValue()).toBe("");
    expect(container.textContent).toContain("No rail matches this part's voltage — pick one");
  });
});

describe("AddPartForm with no rail to sit on", () => {
  const blank = { rails: [] as string[], parts: [{ id: "cat.servo", name: "Servo", numbers: [{ key: "draw_max", value: 7, unit: "W", kind: "datasheet" as const, source: "s" }], fits: [] as string[] }] };
  const mount = (onAdd = vi.fn().mockResolvedValue({ written: true }), catalog = blank) => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<AddPartForm witness="Maya" catalog={catalog} onAdd={onAdd} />));
    const select = container.querySelector<HTMLSelectElement>('[aria-label="catalog part"]')!;
    act(() => {
      select.value = "cat.servo";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    return onAdd;
  };
  const add = () => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Add part")!;
  const press = async () => act(async () => { add().dispatchEvent(new MouseEvent("click", { bubbles: true })); });

  it("shows the power block, blocks Add until both numbers are typed, then sends the rail with the part", async () => {
    const onAdd = mount();
    expect(container.textContent).toContain("Power for this part");
    expect(container.textContent).not.toContain("Type the");
    expect(container.textContent).toContain("volts × amp-hours × C rating");
    expect((container.querySelector('[aria-label="rail name"]') as HTMLInputElement).value).toBe("main");
    await press(); // no feedback until pressed: the lines show now, the first bad field is focused, nothing is sent
    expect(container.textContent).toContain("Type the voltage.");
    expect(container.textContent).toContain("Type the most it can deliver.");
    expect(document.activeElement).toBe(container.querySelector('[aria-label="rail voltage"]'));
    expect(onAdd).not.toHaveBeenCalled();
    type("rail voltage", "12");
    type("rail capacity", "20");
    await press();
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({ rail: "rail.main", newRail: { id: "rail.main", voltage: "12 V", capacity: "20 W" } }));
  });

  it.each([["1e3", false], ["0x10", false], ["Infinity", false], ["1e400", false], ["-1", false], ["0", false], ["7,4", false], ["12V", false], ["", false], ["12", true], ["7.4", true], [" 12 ", true]])("voltage %j: valid is %s", async (value, valid) => {
    const onAdd = mount();
    type("rail capacity", "20");
    type("rail voltage", value);
    await press();
    expect(onAdd.mock.calls.length).toBe(valid ? 1 : 0);
    expect(container.textContent?.includes("Type the voltage.")).toBe(!valid);
  });

  it("keeps the made rail selectable when the part is refused after it", async () => {
    mount(vi.fn().mockResolvedValue({ written: false, refusal: "nope", railKept: true }));
    type("rail voltage", "12");
    type("rail capacity", "20");
    await press();
    expect(container.textContent).toContain("nope");
    expect(container.textContent).not.toContain("Power for this part"); // the rail is picked now, never asked for again
  });

  it("shows the normal select, not the power block, when several rails fit and none is picked", () => {
    mount(undefined, { rails: ["rail.5v", "rail.6v"], parts: [{ ...blank.parts[0]!, fits: ["rail.5v", "rail.6v"] }] });
    expect(container.textContent).not.toContain("Power for this part");
    expect(container.querySelector<HTMLSelectElement>('[aria-label="rail"]')!.value).toBe("");
  });

  it("is unchanged when a rail fits", () => {
    mount(undefined, { rails: ["rail.5v"], parts: [{ ...blank.parts[0]!, fits: ["rail.5v"] }] });
    expect(container.textContent).not.toContain("Power for this part");
  });
});

describe("AddPartForm when Add is pressed with something missing", () => {
  const withRail = { rails: ["rail.a"], parts: [] };
  const noRail = { rails: [], parts: [] };
  const fill = (rails: string[]) => {
    if (rails.length) choose("rail", "rail.a");
    else { type("rail voltage", "12"); type("rail capacity", "20"); }
    type("part name", "dev.x"); type("power draw", "7"); type("part checked by", "Maya");
  };
  it.each([
    ["rail", withRail, "rail", "", "Pick a rail."],
    ["part name", withRail, "part name", " ", "Give the part a name."],
    ["draw blank", withRail, "power draw", "", "Type how much power it draws."],
    ["draw 1e3", withRail, "power draw", "1e3", "Type how much power it draws."],
    ["source", withRail, "part source", "", "Say where the numbers came from."],
    ["witness", withRail, "part checked by", "", "Type who checked it."],
    ["rail name", noRail, "rail name", " !! ", "Give the rail a name."],
    ["voltage", noRail, "rail voltage", "", "Type the voltage."],
    ["capacity", noRail, "rail capacity", "", "Type the most it can deliver."],
  ])("%s: sends nothing, says so under the field, marks it and focuses it", async (_n, catalog, label, value, line) => {
    const onAdd = mountForm(catalog);
    fill(catalog.rails);
    if (label === "rail") choose("rail", ""); else type(label, value);
    await press();
    expect(onAdd).not.toHaveBeenCalled();
    expect(field(label).getAttribute("aria-invalid")).toBe("true");
    expect(container.querySelector(`#${CSS.escape(field(label).getAttribute("aria-describedby")!)}`)?.textContent).toBe(line);
    expect(document.activeElement).toBe(field(label));
  });

  it("shows no line on a fresh form, then every missing line (source defaults) with focus on the first field in order", async () => {
    mountForm(withRail);
    expect(container.querySelector("[aria-invalid]")).toBeNull();
    expect(container.textContent).not.toContain("Pick a rail.");
    await press();
    expect(container.querySelectorAll("[aria-invalid]").length).toBe(4);
    expect(document.activeElement).toBe(field("rail"));
  });

  it("shows a line when its field is left, and drops it once the field is valid", () => {
    mountForm(withRail);
    act(() => { field("power draw").focus(); field("power draw").blur(); });
    expect(container.textContent).toContain("Type how much power it draws.");
    type("power draw", "7");
    expect(container.textContent).not.toContain("Type how much power it draws.");
  });

  it("clears the form and its lines after a part is added", async () => {
    mountForm(withRail, undefined, "Maya");
    await press(); // every line shows
    expect(container.querySelector("[aria-invalid]")).not.toBeNull();
    fill(withRail.rails);
    await press();
    expect(container.textContent).toContain("Part added");
    expect(container.querySelector('[aria-invalid="true"]')).toBeNull();
  });

  it("opens Details when the first bad field sits inside it", async () => {
    const part = { id: "cat.servo", name: "Servo", numbers: [{ key: "draw_max", value: 7, unit: "W", kind: "datasheet" as const, source: "s" }], fits: ["rail.a"] };
    mountForm({ rails: ["rail.a"], parts: [part] as never[] }, undefined, "Maya");
    const select = container.querySelector<HTMLSelectElement>('[aria-label="catalog part"]')!;
    act(() => {
      select.value = "cat.servo";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const details = container.querySelector("details")!;
    expect(details.open).toBe(false);
    type("part source", "");
    await press();
    expect(details.open).toBe(true);
    expect(container.textContent).toContain("Say where the numbers came from.");
  });

  it("after a refused part on a kept rail, Add again sends the part alone on that rail", async () => {
    const onAdd = mountForm(noRail, vi.fn().mockResolvedValueOnce({ written: false, refusal: "nope", railKept: true }).mockResolvedValue({ written: true }), "Maya");
    fill(noRail.rails);
    await press();
    await press();
    expect(onAdd).toHaveBeenCalledTimes(2);
    expect(onAdd.mock.calls[1]![0]).toMatchObject({ rail: "rail.main", id: "dev.x" });
    expect(onAdd.mock.calls[1]![0].newRail).toBeUndefined();
  });
});

describe("AddPartForm Structure tab (LIB-4)", () => {
  const part = { id: "cat.slotted", name: "Slotted servo", numbers: [{ key: "draw_max", value: 7, unit: "W", kind: "datasheet" as const, source: "src" }], fits: ["rail.5v"] };
  const catalog = {
    rails: ["rail.5v"], parts: [part],
    stock: [
      { id: "stock.plate", name: "Plate", dimensions: ["length", "width", "thickness"], mass_from: "material" as const, outline_only: false },
      { id: "tslot.20x20", name: "T-slot profile 20x20", dimensions: ["length"], mass_from: "mass_per_length" as const, outline_only: true },
    ],
    kit: [{ id: "gobilda.1501", name: "Standoff 6mm", outline_only: true }, { id: "gobilda.1120", name: "U-Channel", outline_only: false }],
  };
  const mount = (onAddStructure = vi.fn().mockResolvedValue({ written: true }), onAdd = vi.fn().mockResolvedValue({ written: true })) => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<AddPartForm witness="Maya" catalog={catalog} onAdd={onAdd} onAddStructure={onAddStructure} links={["base", "arm"]} materials={[{ name: "Aluminium 6061-T6", density_kg_m3: 2700 }]} />));
    return { onAdd, onAddStructure };
  };
  const tab = (name: string) => Array.from(container.querySelectorAll<HTMLButtonElement>('[role="tab"]')).find((b) => b.textContent!.startsWith(name))!;
  const optionNames = (label: string) => Array.from(field(label).querySelectorAll("option")).map((o) => o.textContent);

  it("one search finds a component and a stock shape", () => {
    mount();
    type("search parts", "slot");
    expect(tab("Parts").textContent).toBe("Parts (1)");
    expect(tab("Structure").textContent).toBe("Structure (1)");
    expect(optionNames("catalog part")).toContain("Slotted servo");
    act(() => tab("Structure").dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(optionNames("structure part")).toEqual(["Pick one", "T-slot profile 20x20"]);
  });

  it("adds a plate through the stock route with its dimensions in metres and its material", async () => {
    const { onAddStructure, onAdd } = mount();
    act(() => tab("Structure").dispatchEvent(new MouseEvent("click", { bubbles: true })));
    choose("structure part", "stock.plate");
    type("size length", "300"); type("size width", "200"); type("size thickness", "3");
    choose("structure material", "Aluminium 6061-T6");
    await press();
    expect(onAddStructure).toHaveBeenCalledWith({ kind: "stock", part: "stock.plate", name: "plate", dimensions: { length: 0.3, width: 0.2, thickness: 0.003 }, material: "Aluminium 6061-T6", parent: "base", xyz: [0, 0, 0], witness: "Maya" });
    expect(onAdd).not.toHaveBeenCalled();
  });

  it("a plate without a material is not sent", async () => {
    const { onAddStructure } = mount();
    act(() => tab("Structure").dispatchEvent(new MouseEvent("click", { bubbles: true })));
    choose("structure part", "stock.plate");
    type("size length", "300"); type("size width", "200"); type("size thickness", "3");
    await press();
    expect(onAddStructure).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Pick a material.");
  });

  it("shows Outline only on an outline-body kit part and not on a plain one", () => {
    mount();
    act(() => tab("Structure").dispatchEvent(new MouseEvent("click", { bubbles: true })));
    choose("structure part", "gobilda.1501");
    expect(container.querySelector('[aria-label="outline only"]')!.textContent).toContain("Outline only");
    choose("structure part", "gobilda.1120");
    expect(container.querySelector('[aria-label="outline only"]')).toBeNull();
  });

  it("derived mass is said for a material shape, the maker's figure for a T-slot", () => {
    mount();
    act(() => tab("Structure").dispatchEvent(new MouseEvent("click", { bubbles: true })));
    choose("structure part", "stock.plate");
    expect(container.querySelector('[aria-label="mass source"]')!.textContent).toContain("derived");
    choose("structure part", "tslot.20x20");
    expect(container.querySelector('[aria-label="mass source"]')!.textContent).toContain("maker's");
    expect(field("structure material")).toBeNull();
  });

  it("sends the link picked in Attach to, not the first one", async () => {
    const { onAddStructure } = mount();
    act(() => tab("Structure").dispatchEvent(new MouseEvent("click", { bubbles: true })));
    choose("structure part", "tslot.20x20");
    type("size length", "500");
    choose("structure attach to", "arm");
    await press();
    expect(onAddStructure).toHaveBeenCalledWith(expect.objectContaining({ parent: "arm", dimensions: { length: 0.5 } }));
  });

  it("a component still takes two taps after Add part opens: pick it, press Add", async () => {
    const { onAdd } = mount();
    choose("catalog part", "cat.slotted"); // tap 1, on the default Parts tab
    await press(); // tap 2
    expect(onAdd).toHaveBeenCalledTimes(1);
  });
});

describe("AddPartForm attaches a component at a typed position (CAD-05)", () => {
  const mk = (id: string, frames?: string[]) => ({ id, name: id, numbers: [{ key: "draw_max", value: 7, unit: "W", kind: "datasheet" as const, source: "s" }], fits: ["rail.5v"], ...(frames && { frames }) });
  const mount = (onAdd = vi.fn().mockResolvedValue({ written: true })) => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<AddPartForm witness="Maya" catalog={{ rails: ["rail.5v"], parts: [mk("dyn.ax-12a", ["horn"]), mk("dyn.bare", [])], stock: [], kit: [{ id: "k.1", name: "Kit", outline_only: false }] }} onAdd={onAdd} onAddStructure={vi.fn()} links={["base", "arm"]} />));
    return onAdd;
  };
  const pick = (id: string) => choose("catalog part", id);
  const snapBox = () => container.querySelector<HTMLInputElement>('[aria-label="snap to mounting point"]');
  const attachTo = (link: string) => choose("structure attach to", link);
  const sent = (onAdd: ReturnType<typeof mount>) => onAdd.mock.calls[0]![0];

  it("sends the device with its rail and draw, and the attach with the link, metres and frame id", async () => {
    const onAdd = mount();
    pick("dyn.ax-12a"); attachTo("arm");
    type("position x", "10"); type("position y", "-20"); type("position z", "5");
    act(() => snapBox()!.click());
    await press();
    expect(onAdd).toHaveBeenCalledWith({ id: "dev.ax-12a", draw: "7", rail: "rail.5v", source: "s", witness: "Maya", attach: { kind: "attach", part: "dyn.ax-12a", name: "ax_12a", dimensions: {}, material: "", parent: "arm", xyz: [0.01, -0.02, 0.005], witness: "Maya", frame: "horn" } });
  });

  it("sends no frame unless Snap is ticked", async () => {
    const onAdd = mount();
    pick("dyn.ax-12a"); attachTo("base");
    await press();
    expect(sent(onAdd).attach).not.toHaveProperty("frame");
  });

  it("a part with no frame says so and sends none", async () => {
    const onAdd = mount();
    pick("dyn.bare"); attachTo("base");
    expect(snapBox()).toBeNull();
    expect(container.textContent).toContain("No mounting frame on record for this part.");
    await press();
    expect(sent(onAdd).attach).not.toHaveProperty("frame");
  });

  it("follows the Units setting: typed metres go out as metres", async () => {
    setUnits({ length: "m" });
    try {
      const onAdd = mount();
      pick("dyn.bare"); attachTo("base"); type("position x", "0.04");
      expect(container.textContent).toContain("X (m)");
      await press();
      expect(sent(onAdd).attach.xyz[0]).toBe(0.04);
    } finally { setUnits({ length: "mm" }); }
  });

  it("without a link chosen it is the plain add and offers nothing extra", async () => {
    const onAdd = mount();
    pick("dyn.ax-12a");
    expect(container.textContent).not.toContain("No mounting frame");
    expect(field("position x")).toBeNull();
    await press();
    expect(sent(onAdd)).not.toHaveProperty("attach");
  });

  it("a position that is not numbers sends nothing", async () => {
    const onAdd = mount();
    pick("dyn.ax-12a"); attachTo("base"); type("position x", "ten");
    await press();
    expect(onAdd).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Type the position as numbers.");
  });

  it("an attached part still needs its draw: the rail and power fields are checked as before", async () => {
    const onAdd = mount();
    pick("dyn.ax-12a"); attachTo("base"); type("power draw", "");
    await press();
    expect(onAdd).not.toHaveBeenCalled();
  });
});

describe("structureFields (CAD-05)", () => {
  const base = { kind: "attach" as const, part: "dyn.ax-12a", name: "ax_12a", dimensions: {}, material: "", parent: "arm", xyz: [0.01, 0, 0] as [number, number, number], witness: "Maya" };
  it("passes the frame id to the engine, which applies the frame's orientation", () => {
    expect(structureFields({ ...base, frame: "horn" })).toMatchObject({ part: "dyn.ax-12a", frame_id: "horn", frame: { xyz: [0.01, 0, 0] }, parent: "arm" });
  });
  it("sends no frame id without one", () => {
    expect(structureFields(base)).not.toHaveProperty("frame_id");
  });
});

describe("attachAfterDevice (CAD-05)", () => {
  const attach = { kind: "attach" as const, part: "dyn.ax-12a", name: "ax_12a", dimensions: {}, material: "", parent: "arm", xyz: [0, 0, 0] as [number, number, number], witness: "Maya" };
  const run = (written: boolean, railMade: boolean, undoOk = true) => {
    const write = vi.fn().mockResolvedValue({ written, refusal: written ? null : "No link arm" });
    const undo = vi.fn().mockResolvedValue(undoOk);
    return attachAfterDevice({ attach, revision: "r2", railMade, write, undo }).then((out) => ({ out, write, undo }));
  };
  it("attached: one attach write on the device's revision, nothing undone", async () => {
    const { out, write, undo } = await run(true, true);
    expect(out).toBeNull();
    expect(write).toHaveBeenCalledWith("r2", "attach", "ax_12a", expect.objectContaining({ part: "dyn.ax-12a" }), "picked in the app");
    expect(undo).not.toHaveBeenCalled();
  });
  it("refused: the device is taken back, and the rail too when this add made it", async () => {
    const plain = await run(false, false);
    expect(plain.undo).toHaveBeenCalledTimes(1);
    expect(plain.out).toBe("No link arm Nothing was added.");
    expect((await run(false, true)).undo).toHaveBeenCalledTimes(2);
  });
  it("says so when the undo itself fails", async () => {
    expect((await run(false, false, false)).out).toContain("could not be taken back");
  });
});
