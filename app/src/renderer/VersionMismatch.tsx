// one line on the Check face when the running app's version differs from the version
// pinned in the team's CI workflow. Presentational only -- the main process decides
// `pinnedVersion` (see `../main/pinned-version.ts`); this component never touches the
// filesystem and never updates anything. `pinnedVersion === null` covers every "say
// nothing" case at once: no workflow file yet, or the contract sits outside a repository.
export interface VersionMismatchProps {
  appVersion: string;
  pinnedVersion: string | null;
}

export function VersionMismatch({ appVersion, pinnedVersion }: VersionMismatchProps) {
  if (pinnedVersion === null || pinnedVersion === appVersion) return null;
  return (
    <p className="version-mismatch" role="status">
      This app is v{appVersion}; the team&apos;s CI pins v{pinnedVersion}.
    </p>
  );
}
