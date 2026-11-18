// The exam's own one-page datasheet for a made-up servo, built at run time: no real maker's document is downloaded or committed.
// LINES is the one source of truth: the PDF prints them, the scorer reads the numbers a card may cite from them, and
// verify-questions checks a real PDF reader gets them back out of the file.
export const DATASHEET_FILE = "lw-t1-datasheet.pdf";
export const LINES = [
  "Lockwire test servo LW-T1",
  "A made-up part for the Lockwire assistant exam. Not a real product.",
  "Weight 57 g",
  "Stall torque 0.52 N*m at 6 V",
  "Stall current 1.2 A at 6 V",
  "Operating voltage 4.8 V to 6 V",
  "No-load current 150 mA at 6 V",
];
// Every number printed as String(value) would print it (no trailing zero, no comma, no range dash), so propose-part's `shows` accepts it.

/** A unit as one token: N*m, N·m, N.m and Nm are one unit (as propose-part's `shows` reads them). */
export const unitKey = (u: string) => u.replace(/[\s*·.]/g, "");
/** Every number-and-unit the datasheet prints, conditions such as "at 6 V" included: what a card may cite. */
export const PRINTED = LINES.flatMap((l) => [...l.matchAll(/(?<![\d.])(\d+(?:\.\d+)?)\s*(N\*m|mA|g|A|V)(?![A-Za-z])/g)].map((m) => `${Number(m[1])} ${unitKey(m[2]!)}`));
/** The weight, the one number that becomes a declared value (the card's physical_mass). */
export const WEIGHT = "57 g";

/** The PDF's bytes: PDF 1.4, one Letter page, Helvetica, one line of text per LINES entry. ASCII only, so a byte is a character. */
export function datasheetPdf(): Buffer {
  const esc = (s: string) => s.replace(/[\\()]/g, "\\$&");
  const stream = `BT /F1 12 Tf 16 TL 72 720 Td ${LINES.map((l) => `(${esc(l)}) Tj T*`).join(" ")} ET`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
  ];
  let out = "%PDF-1.4\n";
  const at: number[] = [];
  objects.forEach((o, i) => { at.push(out.length); out += `${i + 1} 0 obj\n${o}\nendobj\n`; });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${at.map((n) => `${String(n).padStart(10, "0")} 00000 n \n`).join("")}`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}
