// The one thing preload and main both need to agree on without either importing the other's
// process-specific module (ipcMain isn't available in the preload context). Everything else --
// the handlers, the argument shapes -- lives in main/ipc.ts, which is the only file allowed to
// read PERMITTED_COMMAND_CLASSES and turn it into working dispatch.
export const RUN_CHANNEL = "lockwire:run";

// main -> renderer push, unrelated to RUN_CHANNEL's renderer -> main invoke: the sidecar's
// state changes on its own schedule (child exit, ready line), not in response to a renderer
// call, so it can't be an invoke reply. Read-only for the renderer either way.
export const SIDECAR_STATE_CHANGED_CHANNEL = "lockwire:sidecar-state-changed";

// main -> renderer: File > Open Robot... finished; payload { result: StartUrdfResult } | { error }.
export const MENU_OPENED_CHANNEL = "lockwire:opened-from-menu";
// File > Close robot: main tells the window to go back to the start screen.
export const CLOSE_ROBOT_CHANNEL = "lockwire:close-robot";
export const SHORTCUTS_CHANNEL = "lockwire:show-shortcuts";
// View > Toggle Light/Dark: main tells the window to flip its theme.
export const THEME_CHANNEL = "lockwire:toggle-theme";

// main -> renderer push: Assistant tool-call progress ({ type: "tool", name }) and the final
// message ({ type: "done", turn }). Same shape as the pushes above.
export const ASSISTANT_EVENT_CHANNEL = "lockwire:assistant-event";
// The stand-in name every dry run carries (main/assistant.ts); the panel never shows it as a name.
export const DRY_RUN_WITNESS = "your name";
/** A dry run's diff as the panel's folded "exact change" shows it: the saved name where the stand-in was, or with none saved the stand-in left out.
 * Display only: what Apply sends is not this text. Kept here, beside the stand-in, because it rewrites the engine's own journal words, not app copy. */
export const shownDiff = (diff: string, name: string): string => {
  const at = DRY_RUN_WITNESS.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return name ? diff.replace(new RegExp(`(witness:? )${at}`, "g"), (_m, w: string) => w + name)
    : diff.replace(new RegExp(`, witness ${at}`, "g"), "").split("\n").filter((l) => !new RegExp(`^\\+\\s*witness: ${at}$`).test(l)).join("\n");
};
// main -> renderer push: a show tool of the Assistant (main/assistantShow.ts) changed what is on
// screen. View only: it never touches the robot file.
export const ASSISTANT_SHOW_CHANNEL = "lockwire:assistant-show";

// main -> renderer push: the drawer Terminal's pty output ({ type: "data", data }) and its end
// ({ type: "exit", code }).
export const TERMINAL_EVENT_CHANNEL = "lockwire:terminal-event";

export const PERMITTED_COMMAND_CLASSES = [
  "contract:edit",
  "contract:write",
  "contract:undo",
  "contract:probe",
  "contract:page",
  "contract:declare-form",
  "contract:what-if",
  "contract:swap-preview",
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
  "file:save-picture",
  "file:print-part",
  "file:choose-package-folder",
  "sidecar:state",
  "sidecar:restart",
  "assistant:settings-get",
  "assistant:settings-set",
  "assistant:send",
  "assistant:apply",
  "assistant:cancel",
  "assistant:show-finding",
  "terminal:start",
  "terminal:write",
  "terminal:resize",
  "terminal:kill",
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
      args: Omit<Provenance, "witness"> & { witness?: string | null; formal?: boolean; contract: string; path: string; value: unknown };
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
    }
  | {
      command: "delete";
      args: Provenance & { contract: string; kind: "link" | "joint" | "device"; name: string };
    }
  /** One step of the engine's edit journal (not the old whole-file undo): exactly the last write goes back or forward. */
  | { command: "undo-edit" | "redo-edit"; args: { contract: string } }
  | { command: "set-witness"; args: { name: string } }
  | {
      command: "swap-apply";
      args: { contract: string; joint: string; part_id: string; expected_revision: string };
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

/** Part Swap: the ranked servos for one joint, and the knock-on for `part_id` (default: the top one). */
export type SwapPreviewRequest = { contract: string; joint: string; part_id?: string };

export type PlanRequest = { contract: string; plan: string };
export type DiffRequest = { contract: string; a?: string; b?: string };
export type HistoryRequest = { contract: string; revision_range?: string | null };

// The Assistant (main/assistant.ts). The key only ever travels renderer -> main, never back.
export type AssistantSettings = { hasKey: boolean; model: string; enabledRobots: string[]; /** True when the tool table has the file tools, so the allow screen says the Assistant can read files in the robot's folder. */ files?: boolean };
export type AssistantSetRequest = {
  key?: string;
  forgetKey?: boolean;
  model?: string;
  enableRobot?: string;
  disableRobot?: string;
};
export type AssistantSetResult = { settings: AssistantSettings; error?: string };
/** What is on screen when a question is sent: the selected part and the open view. Nothing else about the screen is sent. */
export type AssistantContext = { selection?: { id: string; name: string }; view?: "graph" | "power" | "data" | "mechanical" };
export type AssistantSendRequest = { contract: string; revision: string; mode: string; text: string; context?: AssistantContext };
/** One finding a proposal adds, clears or alters, in the engine's own words; `was` is what a changed one read before. */
export type AssistantFindingChange = { kind: "new" | "cleared" | "changed"; severity: string; quantity: string; observed: string; expected: string; was?: string };
/** The engine's check before and after a proposal, run on a temporary copy (main/proposalCheck.ts); `error` when it could not be compared. */
export type AssistantComparison = { before: string; after: string; changes: AssistantFindingChange[] } | { error: string };
export type AssistantProposal = { id: string; command: string; args: Record<string, unknown>; revision: string; /** The sidecar's own dry-run diff: what signing would change. */ diff: string; comparison?: AssistantComparison; /** True when this exact change is a fix `suggest-fixes` returned for this same file earlier in the conversation: the engine searched it and proved it. */ proved?: boolean;
  note?: string; // main's own plain words shown with the card
  /** propose-part only: each number main ACCEPTED under its number rule (the quote shows the value and unit), for the card's face. Page and quote are the assistant's reading, not checked against the file. */
  cites?: { name: string; value: number; unit: string; page: number; quote: string }[];
  /** A swap only: the old and the new servo, each by its catalog id and the catalog's own name (`catalog-parts`), for the card's words; absent when the catalog has none. */
  swap?: { from?: { id: string; name: string }; to: { id: string; name: string } };
};
/** The engine's own word from a `check` of THIS turn (GO, NO-GO, ...), or "unknown" with the reason when no check gave one, for any reason. Never the model's words, never a remembered verdict. */
export type AssistantVerdict = { word: string; reason?: string; /** ISO time, main's clock, when this verdict was taken: the panel keeps the newest of overlapping turns. */ at: string };
export type AssistantTurn = {
  text: string; proposals: AssistantProposal[]; error?: string;
  /** On every turn, a failed one too: no check result this turn means "unknown", never a missing field. */
  verdict: AssistantVerdict;
  /** Numbers with a unit in `text` that no tool result of this conversation, the robot brief or the person's own messages holds with that value and unit, and verdict claims that are not `verdict`. The panel marks them unverified; `text` is never changed. */
  ungrounded?: string[];
  /** Verdict words in `text` that only a proposal of this turn supports (a what-if's proposed verdict, a fix search's verdict after, a card's After), not the robot's own check: the robot after the proposed change, not the robot now. Same item shape as `ungrounded`, never in it. */
  proposedOnly?: string[];
  /** One line per show tool that changed the screen this turn. */
  shown?: string[];
  /** The findings of the check `verdict` came from: each one's path and severity, the engine's own. Absent when `verdict` is unknown. The panel counts the faults and lists the rows from it. */
  findings?: { quantity: string; severity: string }[];
};
/** `comparison`: nothing was written because the card's Before and After came out different at Apply: the card shows these and stays open. */
export type AssistantApplyResult = { ok: true } | { ok: false; error: string; comparison?: AssistantComparison };
export type AssistantEvent = { type: "tool"; name: string } | { type: "done"; turn: AssistantTurn };
/** One change to what is on screen, asked for by a show tool. `part` and `parts` are 3D part ids (see `partOf`); `finding` is a card's place in the page's `cards`. */
export type AssistantShow = { contract: string } & (
  | { action: "select-part"; part: string }
  | { action: "show-only"; parts: string[]; finding?: number }
  | { action: "show-all" }
  | { action: "show-connections"; lens: "power" | "data" | "mechanical" }
  | { action: "open-graph" }
);
/** A robot-graph node id ("joint:elbow") as the 3D view's part id ("elbow"); null for a node with no body (a rail, a wire, the battery). */
export const partOf = (id: string): string | null => (/^(link|joint|device):/.test(id) ? id.slice(id.indexOf(":") + 1) : null);
/** The part ids the 3D view has a mesh for (scene/RobotView.tsx): a robot with scene parts draws those; one without draws its joints and the links that have solids.
 * Main and the page both use it, so a show-only list never hides everything. */
export const meshIds = (sceneParts: string[], joints: string[], solidLinks: string[]): string[] => (sceneParts.length ? sceneParts : [...joints, ...solidLinks]);

export type TerminalStartRequest = { cols: number; rows: number; /** The open robot's folder; main falls back to home. */ cwd?: string };
export type TerminalEvent = { type: "data"; data: string } | { type: "exit"; code: number };
