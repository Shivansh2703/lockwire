import { PackageRootRows } from "./PackageRootRows";

// The named empty-geometry state (plan v8 line 458): when the contract has no geometry, this
// panel names the unresolved prefix -- in `start`'s own words, never a second composed
// sentence -- offers the package-root control inline, and after a pick reports
// "resolved N of M meshes" using the count the command returned, not one this file computes.
export interface EmptyGeometryPanelProps {
  /** `start`'s own stderr reason, verbatim, e.g. "package://arm_description/... not resolved". */
  reason: string;
  /** Set once a resolution attempt has run; the counts are the command's, not computed here. */
  resolved?: { count: number; total: number };
  onPackageRootsChange?: (roots: string[]) => void;
}

export function EmptyGeometryPanel({ reason, resolved, onPackageRootsChange }: EmptyGeometryPanelProps) {
  return (
    <div className="empty-geometry-panel">
      <p className="empty-geometry-reason">{reason}</p>
      <PackageRootRows onChange={onPackageRootsChange} />
      {resolved && (
        <p className="empty-geometry-resolved">
          resolved {resolved.count} of {resolved.total} meshes
        </p>
      )}
    </div>
  );
}
