import { useDismiss } from "./MoreMenu";
import { Button } from "./kit";
import { onMac, shortcutRows } from "./shortcuts";
import { lenUnit, useUnits } from "./units";

/** The keyboard shortcuts that exist, in a small sheet over the app. Esc, a click away or Close shuts it. */
export function ShortcutsSheet({ onClose }: { onClose: () => void }) {
  const ref = useDismiss(true, onClose);
  useUnits();
  return (
    <div className="shortcuts-sheet" role="dialog" aria-label="Keyboard shortcuts" ref={ref}>
      <h2>Keyboard shortcuts</h2>
      <dl>
        {shortcutRows(onMac(), lenUnit()).map(([keys, what]) => <div key={keys}><dt><kbd>{keys}</kbd></dt><dd>{what}</dd></div>)}
      </dl>
      <Button variant="quiet" onClick={onClose} autoFocus>Close</Button>
    </div>
  );
}
