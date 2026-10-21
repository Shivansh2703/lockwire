// The one thing preload and main both need to agree on without either importing the other's
// process-specific module (ipcMain isn't available in the preload context). Everything else --
// the handlers, the argument shapes -- lives in main/ipc.ts, which is the only file allowed to
// read PERMITTED_COMMAND_CLASSES and turn it into working dispatch.
export const RUN_CHANNEL = "lockwire:run";

// main -> renderer push, unrelated to RUN_CHANNEL's renderer -> main invoke: the sidecar's
// state changes on its own schedule (child exit, ready line), not in response to a renderer
// call, so it can't be an invoke reply. Read-only for the renderer either way (e9-join).
export const SIDECAR_STATE_CHANGED_CHANNEL = "lockwire:sidecar-state-changed";

// main -> renderer: File > Open Robot... finished; payload { result: StartUrdfResult } | { error }.
export const MENU_OPENED_CHANNEL = "lockwire:opened-from-menu";

// main -> renderer push: Assistant tool-call progress ({ type: "tool", name }) and the final
// message ({ type: "done", turn }). Same shape as the pushes above.
export const ASSISTANT_EVENT_CHANNEL = "lockwire:assistant-event";

export const PERMITTED_COMMAND_CLASSES = [
  "contract:edit",
  "contract:write",
  "contract:undo",
  "contract:probe",
  "contract:page",
  "contract:declare-form",
  "contract:what-if",
  "contract:plan",
  "contract:diff",
  "contract:history",
  "robot:command",
  "file:open",
  "file:start-urdf",
  "file:open-dropped",
  "file:new-robot",
  "file:starters",
  "file:choose-plan",
  "sidecar:state",
  "sidecar:restart",
  "assistant:settings-get",
  "assistant:settings-set",
  "assistant:send",
  "assistant:apply",
  "assistant:cancel",
] as const;

export type CommandClass = (typeof PERMITTED_COMMAND_CLASSES)[number];

type Provenance = {
  expected_revision: string;
  source: string;
  witness: string;
  note?: string;
  locked_at?: string;
  dry_run?: boolean;
};

type Triple = [number, number, number];
type MountShape = {
  kind: "plate" | "bracket" | "tube" | "custom-mesh";
  dimensions_mm: Triple;
  fastener_pattern?: string;
  mesh_path?: string;
};

export type ContractWriteRequest =
  | {
      command: "declare";
      args: Provenance & { contract: string; path: string; value: unknown };
    }
  | {
      command: "add";
      args: Provenance & {
        contract: string;
        kind: string;
        id?: string;
        fields?: Record<string, unknown>;
      };
    }
  | {
      command: "review";
      args: Omit<Provenance, "source"> & {
        contract: string;
        path: string;
        state: "approved" | "questioned";
      };
    }
  | {
      command: "commit";
      args: { contract: string; message: string; paths: string[]; expected_revision: string; dry_run?: boolean };
    }
  | {
      command: "place";
      args: Provenance & { contract: string; target: string; on: string; offset: Triple; unit: string };
    }
  | {
      command: "move";
      args: Provenance & { contract: string; target: string; offset: Triple; unit: string };
    }
  | {
      command: "rotate";
      args: Provenance & { contract: string; target: string; rpy: Triple; unit: string };
    }
  | {
      command: "reparent";
      args: Provenance & { contract: string; target: string; to: string };
    }
  | {
      command: "mount-shape";
      args: Provenance & { contract: string; target: string; shape: MountShape };
    };

export type ContractUndoRequest = {
  contract: string;
  expected_revision: string;
  source?: string;
  apply?: boolean;
};

/** The existing sidecar `page` producer takes only a contract path. */
export type ContractPageRequest = { contract: string };

/** Replacement is bound to the URDF and raw revision returned by the native start. */
export type StartUrdfRequest = { urdf: string; expected_revision: string };

/** The existing sidecar `declare-form` producer takes only a contract path. */
export type DeclareFormRequest = { contract: string };

/** One observed proposal, compared against the exact bytes the renderer opened. */
export type ContractWhatIfRequest = {
  contract: string;
  expected_revision: string;
  change: { path: string; value: string; source: string; witness: string };
};

export type PlanRequest = { contract: string; plan: string };
export type DiffRequest = { contract: string; a?: string; b?: string };
export type HistoryRequest = { contract: string; revision_range?: string | null };

// The Assistant (main/assistant.ts). The key only ever travels renderer -> main, never back.
export type AssistantSettings = { hasKey: boolean; model: string; enabledRobots: string[] };
export type AssistantSetRequest = {
  key?: string;
  forgetKey?: boolean;
  model?: string;
  enableRobot?: string;
  disableRobot?: string;
};
export type AssistantSetResult = { settings: AssistantSettings; error?: string };
export type AssistantSendRequest = { contract: string; revision: string; mode: string; text: string };
export type AssistantProposal = { id: string; command: string; args: Record<string, unknown>; revision: string; /** The sidecar's own dry-run diff: what signing would change. */ diff: string };
export type AssistantTurn = { text: string; proposals: AssistantProposal[]; error?: string };
export type AssistantApplyResult = { ok: true } | { ok: false; error: string };
export type AssistantEvent = { type: "tool"; name: string } | { type: "done"; turn: AssistantTurn };
