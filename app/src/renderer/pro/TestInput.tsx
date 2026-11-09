import { useState } from "react";

import type { RobotJoint } from "../../shared/api.gen";
import type { PreflightPayload } from "../../shared/pro/types";
import { verdictPill } from "../FindingList";
import { Button, FilePick, Notice, Panel, StatusPill } from "../kit";
import { TestFace } from "./TestFace";

interface TestRequest {
  contract: string;
  recording: string;
  context?: string;
}

export interface TestInputProps {
  contract: string;
  context?: string;
  payload: PreflightPayload | null;
  onPayload: (payload: PreflightPayload | null) => void;
  /** The router supplies the existing native picker through preload. */
  onChooseRecording: () => Promise<string | null>;
  /** The router sends this complete request to the served preflight door. */
  onTest: (request: TestRequest) => Promise<PreflightPayload>;
  joints?: RobotJoint[];
  selected?: number | null;
  onSelect?: (index: number | null) => void;
}

const baseName = (path: string) => path.split(/[\\/]/).pop() || path;

/** One right-hand Panel: pick a recording, score it, read the findings. */
export function TestInput({ contract, context, payload, onPayload, onChooseRecording, onTest, joints, selected, onSelect }: TestInputProps) {
  const [recording, setRecording] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function chooseRecording() {
    setError(null);
    try {
      const path = await onChooseRecording();
      if (path) setRecording(path);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }

  async function runTest() {
    if (!recording) return;
    setError(null);
    try {
      const next = await onTest({ contract, recording, ...(context ? { context } : {}) });
      onSelect?.(null);
      onPayload(next);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <Panel
      title="Test"
      footer={recording ? <div className="test-score"><Button variant="primary" onClick={runTest}>Score recording</Button></div> : undefined}
      actions={payload && <span className="verdict-word" role="status">{verdictPill(payload.verdict)}</span>}
    >
      <div className="test-input">
        <FilePick label="Choose recording" path={recording ? baseName(recording) : undefined} onPick={chooseRecording} onRemove={() => { setRecording(null); onSelect?.(null); onPayload(null); }} />
        {payload && <span className="test-mode" title="Robot off (recording only): this checks a recording; the robot stays off"><StatusPill status="muted">Robot off</StatusPill></span>}
      </div>
      {error && <Notice kind="error">Test failed: {error}</Notice>}
      {payload && (payload.verdict === "NOT_SCORED"
        ? <p className="test-ready" role="status">Nothing in this recording could be compared to {payload.robot}</p>
        : <div className="test-rows"><TestFace payload={payload} joints={joints} selected={selected} onSelect={onSelect} /></div>)}
    </Panel>
  );
}
