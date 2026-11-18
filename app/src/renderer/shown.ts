import { createContext, useContext, useMemo } from "react";

import type { Finding, FindingLabel } from "../shared/api.gen";
import { inFolder } from "../shared/paths";

/** The open robot's lockfile path, for display only: App provides it around everything it draws. */
export const RobotFile = createContext<string | undefined>(undefined);

/** Engine text as the screen shows it: a path in the robot's folder reads relative, any other absolute path as its file name.
 * Display only: what Copy copies, and what goes back to the engine, keeps the engine's own text. */
export const useShown = () => {
  const contract = useContext(RobotFile);
  return useMemo(() => inFolder(contract ?? ""), [contract]);
};

/** A finding's (and its plain label's) printed words through `shown`; numbers, paths and jumps untouched. */
export const shownFinding = (f: Finding, shown: (t: string) => string): Finding => ({
  ...f,
  observed: shown(f.observed),
  expected: shown(f.expected),
  action: shown(f.action),
  source: f.source && shown(f.source),
  witness: f.witness && shown(f.witness),
  next_command: f.next_command && shown(f.next_command),
  alternatives: f.alternatives?.map((a) => ({ ...a, text: shown(a.text) })),
});
export const shownLabel = (l: FindingLabel | undefined, shown: (t: string) => string): FindingLabel | undefined =>
  l && { ...l, fix: l.fix && shown(l.fix), instead: l.instead?.map((a) => ({ ...a, text: shown(a.text) })) };
