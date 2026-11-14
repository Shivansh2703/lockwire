import type { GraphNode, PagePayload } from "../shared/api.gen";
import { Button, Panel } from "./kit";
import { registerPanel } from "./panelRegistry";
import { pro } from "./proRegistry";

const KINDS: { kind: GraphNode["kind"]; word: string }[] = [{ kind: "device", word: "Part" }, { kind: "rail", word: "Power rail" }, { kind: "bus", word: "Data bus" }];

/** What the Parts tree calls each thing: a part by its declared name, a rail by its power-stage label; the id only when no name exists. */
const nameOf = (page: PagePayload, node: GraphNode) =>
  (node.kind === "device" ? page.faces?.reference?.devices.find((d) => d.id === node.id)?.name : node.kind === "rail" ? page.power_stages?.find((s) => s.rail === node.id)?.label : undefined) ?? node.label;

/** Parts first, then power rails and data buses, one list. A part shows once even when several lenses name it. */
export function bomRows(page: PagePayload): { node: GraphNode; word: string }[] {
  const nodes = Object.values(page.graph ?? {}).flatMap((lens) => lens.nodes);
  return KINDS.flatMap(({ kind, word }) => [...new Map(nodes.filter((n) => n.kind === kind).map((n) => [n.id, n])).values()].map((node) => ({ node, word })));
}

const cell = (v: string) => (/[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v);
export function bomCsv(page: PagePayload): string {
  const extra = pro?.bomColumn;
  const head = ["Item", "Kind", "Detail", "Id", ...(extra ? [extra.label] : [])];
  const lines = bomRows(page).map(({ node, word }) => [nameOf(page, node), word, node.sub, node.id, ...(extra ? [extra.cell(node) ?? ""] : [])]);
  return [head, ...lines].map((r) => r.map(cell).join(",")).join("\n") + "\n";
}

function saveCsv(page: PagePayload) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([bomCsv(page)], { type: "text/csv" }));
  a.download = "parts-list.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

export function BomPanel({ page }: { page: PagePayload }) {
  const rows = bomRows(page);
  const extra = pro?.bomColumn;
  const showCost = !!extra && rows.some((r) => extra.cell(r.node) !== null); // Pro column appears only when a price exists
  return (
    <Panel title="Parts list" actions={<Button variant="quiet" onClick={() => saveCsv(page)}>Save CSV</Button>}>
      <table className="bom-table" style={{ width: "100%", borderCollapse: "collapse", font: "400 13px var(--sans)" }}>
        <thead><tr style={{ textAlign: "left" }}><th>Item</th><th>Kind</th><th>Detail</th>{showCost && <th>{extra!.label}</th>}</tr></thead>
        <tbody>
          {rows.map(({ node, word }) => (
            <tr key={node.kind + ":" + node.id}><td>{nameOf(page, node)}</td><td>{word}</td><td>{node.sub}</td>{showCost && <td>{extra!.cell(node) ?? ""}</td>}</tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

registerPanel({ id: "parts", label: "Parts", hasData: (p) => bomRows(p).length > 0, Body: ({ page }) => <BomPanel page={page} /> });
