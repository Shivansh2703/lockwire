/** A typed power spec ("4S, 5 V 3 A, 12 V 1 A"): a cell count and at least one rail's volts then amps, and nothing else but "rail", "and" and punctuation. Any other word makes it a plain Agent question. */
export const isPowerSpec = (text: string): boolean => {
  const t = text.toLowerCase();
  if (!/\b\d+\s*s\b/.test(t) || !/\d(\.\d+)?\s*v\s*\d(\.\d+)?\s*a\b/.test(t)) return false;
  return !/[a-z]/.test(t.replace(/\b\d+\s*s\b|\d+(\.\d+)?\s*[va]\b|\b(rails?|and)\b/g, "")); // ponytail: English-only filler words, add more if people type them
};
