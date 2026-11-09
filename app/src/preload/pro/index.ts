import type { AnalysisStartRequest, ContractActuatorReviewDraftRequest, PreflightRequest, RunStartRequest } from "../../shared/pro/ipc-contract";
import type { AnalysisJobStatePayload, CheckPayloadPro, PreflightPayload, RunStatePayload } from "../../shared/pro/types";

type Run = <T>(commandClass: string, args: unknown) => Promise<T>;

// The paid half of window.lockwire: named methods, each hardcoding its own command class, merged
// into the free bridge object by preload/index.ts. The renderer reads it through renderer/pro/api.ts.
export default function proApi(run: Run) {
  return {
    prepareActuatorReviewDraft: (request: ContractActuatorReviewDraftRequest): Promise<{ document: CheckPayloadPro; cliLine: string }> =>
      run("contract:actuator-review-draft", request),
    checkRecording: (request: PreflightRequest): Promise<PreflightPayload> => run("contract:preflight", request),
    startRun: (request: RunStartRequest): Promise<RunStatePayload> => run("run:start", request),
    pollRun: (): Promise<RunStatePayload> => run("run:poll", undefined),
    cancelRun: (): Promise<RunStatePayload> => run("run:cancel", undefined),
    startAnalysis: (request: AnalysisStartRequest): Promise<AnalysisJobStatePayload> => run("analysis:start", request),
    pollAnalysis: (): Promise<AnalysisJobStatePayload> => run("analysis:poll", undefined),
    cancelAnalysis: (): Promise<AnalysisJobStatePayload> => run("analysis:cancel", undefined),
    chooseRecording: (): Promise<string | null> => run("file:choose-recording", undefined),
  };
}

export type ProPreloadApi = ReturnType<typeof proApi>;
