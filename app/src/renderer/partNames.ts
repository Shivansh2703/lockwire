import type { RobotGraph } from "../shared/api.gen";

/** The one place a part id ("rail:rail.5v") becomes words: the engine's label if there is one, else the
 * engine's own id as written ("rail.5v"). The app never builds or re-cases a name. */
export const partNames = (labels: Iterable<readonly [string, string]>) => {
  const names = new Map(labels);
  return (id: string) => {
    const tail = id.split(":").pop()!;
    return names.get(id) ?? names.get(tail) ?? tail;
  };
};

export const graphLabels = (graph?: RobotGraph | null) => (graph?.nodes ?? []).map((n) => [n.id, n.label] as const);
