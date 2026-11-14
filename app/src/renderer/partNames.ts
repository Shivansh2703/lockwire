import type { RobotGraph } from "../shared/api.gen";

/** The one place a part id ("rail:rail.5v") becomes words: a drawn label if there is one, else kind plus id. */
export const partNames = (labels: Iterable<readonly [string, string]>) => {
  const names = new Map(labels);
  return (id: string) => names.get(id) ?? names.get(id.split(":").pop()!) ?? id.replace(":", " ");
};

export const graphLabels = (graph?: RobotGraph | null) => (graph?.nodes ?? []).map((n) => [n.id, n.label] as const);
