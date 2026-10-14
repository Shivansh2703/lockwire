import { useState } from "react";

// `lockwire start --package-root` is repeatable (measured, E0 landed it) -- so the control
// that feeds it must add rows, never overwrite a single field (plan v8 line 458).
export interface PackageRootRowsProps {
  onChange?: (roots: string[]) => void;
}

export function PackageRootRows({ onChange }: PackageRootRowsProps) {
  const [roots, setRoots] = useState<string[]>([""]);

  function update(next: string[]) {
    setRoots(next);
    onChange?.(next.filter((r) => r.trim() !== ""));
  }

  function addRow() {
    update([...roots, ""]);
  }

  function setRow(index: number, value: string) {
    const next = roots.slice();
    next[index] = value;
    update(next);
  }

  function removeRow(index: number) {
    const next = roots.filter((_, i) => i !== index);
    update(next.length > 0 ? next : [""]);
  }

  return (
    <div className="package-root-rows">
      {roots.map((value, index) => (
        <div className="package-root-row" key={index}>
          <input
            type="text"
            aria-label="package root"
            value={value}
            onChange={(e) => setRow(index, e.target.value)}
          />
          {roots.length > 1 && (
            <button type="button" aria-label="remove package root" onClick={() => removeRow(index)}>
              &minus;
            </button>
          )}
        </div>
      ))}
      <button type="button" onClick={addRow}>
        Add package root
      </button>
    </div>
  );
}
