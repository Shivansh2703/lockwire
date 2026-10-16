/** One Fix for several parts that share it: "Pick a lower power mode for Gripper Servo or shed other load"
 * + the same for "Shoulder Lift Servo" -> "... for Gripper Servo and Shoulder Lift Servo or shed ...".
 * Texts differing only by the part name merge (names beyond two become "N parts"); null when they differ otherwise. */
export function mergeFix(texts: string[]): string | null {
  const w = texts.map((t) => t.split(" "));
  const n = Math.min(...w.map((x) => x.length));
  let p = 0, s = 0;
  while (p < n && w.every((x) => x[p] === w[0]![p])) p++;
  while (s < n - p && w.every((x) => x[x.length - 1 - s] === w[0]![w[0]!.length - 1 - s])) s++;
  while (s > 0 && /^[A-Z0-9]/.test(w[0]![w[0]!.length - s]!)) s--; // a name's own tail word ("Servo") stays in the name
  const mids = w.map((x) => x.slice(p, x.length - s).join(" "));
  if (p === 0 || mids.some((m) => !m)) return null;
  const names = [...new Set(mids)];
  const who = names.length <= 2 ? names.join(" and ") : `${names.length} parts`;
  return [...w[0]!.slice(0, p), who, ...w[0]!.slice(w[0]!.length - s)].join(" ");
}
