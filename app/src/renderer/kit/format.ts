/** The one human display of a number (mirror of `lockwire.report.format_display`): at most 1 decimal from
 * 10 up, 3 significant figures below 10, no trailing zeros, g/mm/mA steps, "≈0" under a millionth. */
const PREFIX: Record<string, string[]> = { kg: ["kg", "g", "mg"], m: ["m", "mm"], A: ["A", "mA"] };

function show(value: number, unit: string, extra: number): string {
  if (value !== 0 && Math.abs(value) < 1e-6) return "≈0";
  const steps = PREFIX[unit];
  if (steps) {
    let n = 0;
    while (n + 1 < steps.length && value !== 0 && Math.abs(value) < 1) { value *= 1000; n++; }
    unit = steps[n]!;
  }
  const decimals = value === 0 ? 0 : Math.abs(value) >= 10 ? 1 : 2 - Math.floor(Math.log10(Math.abs(value)));
  const text = value.toFixed(Math.min(decimals + extra, 100)).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  return `${text} ${unit}`.trim();
}

/** `vs` is the number it is compared with: digits are added until the two read differently. */
export function fmtQty(value: number, unit = "", vs?: number): string {
  let text = "";
  for (let extra = 0; extra < 7; extra++) {
    text = show(value, unit, extra);
    if (vs === undefined || vs === value || text !== show(vs, unit, extra)) break;
  }
  return text;
}

// Only what the engine over-prints (2+ decimals, or e-notation): "USB 2.0 A" and mid-word numbers stay as written.
const TOKEN = /(?<![\w.])(-?\d+(?:\.\d{2,}(?:e-?\d+)?|e-?\d+)) (kg|W|V|A|m|N\*m)(?![\w*])/g;

/** Engine prose with its "275.52 W" numbers shown in the one format; `vs` guards the first number. */
export const tidyNumbers = (text: string, vs?: number): string =>
  text.replace(TOKEN, (_, n: string, u: string) => fmtQty(Number(n), u, vs));
