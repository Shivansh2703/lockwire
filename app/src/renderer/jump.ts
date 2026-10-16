import type { Target } from "../shared/api.gen";

/** What a click on a named thing asks the App to show (almost everything in the tabs is
 * clickable). App resolves it: a declarable `path` opens that Declare row; else `findingIdx`
 * opens that Check card; else the parts light (a second click on the same parts opens Design). */
export interface Jump {
  targets?: Target[];
  /** A name the sheets print (a group label or an id), resolved against the scene. */
  name?: string;
  parts?: string[];
  path?: string | null;
  /** Open the Declare row's Review section (where its source and witness live). */
  review?: boolean;
  findingIdx?: number;
  /** Clear the selection and frame the whole robot. */
  whole?: boolean;
  design?: boolean;
  /** Open Declare, on the row about the first part named when there is one. */
  declare?: boolean;
}
/** Go somewhere; `can` says whether the jump lands anywhere, so a name that leads nowhere is
 * drawn as plain text, never a button that does nothing. */
export type OnJump = { (jump: Jump): void; can: (jump: Jump) => boolean };

/** An "Instead" row's destination, from the engine's typed `rail`: a move selects the rail to move
 * to; a regulator opens the overloaded rail's capacity (or selects that rail). No rail: nowhere. */
export function insteadJump(row: { kind: string; rail?: string | null } | undefined): Jump {
  if (!row?.rail) return {};
  const targets = [{ kind: "rail" as const, id: row.rail, label: row.rail }];
  return row.kind === "projected" ? { path: `topology/rails/${row.rail}/capacity`, targets } : { targets };
}

/** The Declare row a quantity names: itself, else its first child row (a budget's `/limit`),
 * else the longest row it sits under. Null when the lockfile has no such row. */
export function declarePathOf(quantity: string | null | undefined, rows: { path: string }[]): string | null {
  if (!quantity) return null;
  if (rows.some((r) => r.path === quantity)) return quantity;
  const child = rows.find((r) => r.path.startsWith(`${quantity}/`));
  if (child) return child.path;
  const under = rows.filter((r) => quantity.startsWith(`${r.path}/`)).sort((a, b) => b.path.length - a.path.length)[0];
  return under?.path ?? null;
}
