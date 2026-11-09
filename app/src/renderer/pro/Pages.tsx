import type { PreflightPayload } from "../../shared/pro/types";
import type { ProPagesProps } from "../proRegistry";
import { proApi } from "./api";
import { RunPanel } from "./RunPanel";
import { TestInput } from "./TestInput";

/** The Test page (when its tab is up) and the always-mounted Run page. */
export function Pages({ mode, contract, joints, sceneIds, result, onResult, selected, onSelect, onHighlight }: ProPagesProps) {
  return (
    <>
      {mode === "Test" && contract ? (
        <TestInput
          key={contract}
          contract={contract}
          payload={result as PreflightPayload | null}
          onPayload={onResult}
          onChooseRecording={proApi().chooseRecording}
          onTest={proApi().checkRecording}
          joints={joints}
          selected={selected}
          onSelect={onSelect}
        />
      ) : null}
      <RunPanel active={mode === "Run"} contract={contract} joints={joints} sceneIds={sceneIds} onHighlight={onHighlight} />
    </>
  );
}
