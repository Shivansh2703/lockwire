/** Sided limb parts ("Left Hip Pitch Link", "Front Left Upper Leg") belong to one limb: "Left leg". Shared by Design and Declare. */
export function limbOf(name: string): { label: string; rest: string } | null {
  const m = /^((?:(?:front|hind|rear|left|right) ){1,2})(.+)$/i.exec(name);
  if (!m) return null;
  const side = m[1]!.trim().toLowerCase();
  const kind = /\b(hip|thigh|shank|foot|knee|ankle|leg|toe)\b/i.test(m[2]!) || (/shoulder/i.test(m[2]!) && /front|hind|rear/.test(side)) ? "leg" : /\b(shoulder|elbow|wrist|hand|arm|forearm|finger)\b/i.test(m[2]!) ? "arm" : null;
  return kind && { label: `${side[0]!.toUpperCase()}${side.slice(1)} ${kind}`, rest: m[2]! };
}
