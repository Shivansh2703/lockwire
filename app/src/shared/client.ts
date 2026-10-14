// The typed front door onto anything the sidecar can hand back as a `MachineDocument`
// (`api.gen.ts`, generated from `src/lockwire/api_payload.py`). `runCommand`'s argument and
// return types come *only* from that generated union -- never a hand-typed interface -- so a
// command name the union does not describe cannot compile, and a reply type can only ever be
// one the generator actually emits.
//
// The actual wiring of a live dispatcher (over `window.lockwire`, once a box wires a generic
// command through the IPC bridge) is deliberately left to the caller: this box's charter is the
// *typing*, not a new IPC surface, and `app/src/main/ipc.ts` / `app/src/preload/index.ts` are
// outside its file list.
import type { MachineDocument } from "./api.gen";

/** Every reply shape the generator emits, keyed by its own discriminant. */
export type CommandName = MachineDocument["artifact_type"];

/** The one `MachineDocument` member whose `artifact_type` is `C`. */
export type DocumentFor<C extends CommandName> = Extract<MachineDocument, { artifact_type: C }>;

/** What actually crosses the wire: a command name plus its arguments in, a raw reply out. Supplied
 * by the caller (e.g. a `window.lockwire` method once one exists) -- `runCommand` only narrows the
 * type of what comes back. */
export type CommandDispatcher = (command: CommandName, args?: unknown) => Promise<unknown>;

/** `runCommand("lockwire-check-report", args)` resolves to `CheckPayload`, typed only off the
 * generated union -- pass a command name outside `CommandName` and the call does not compile. */
export function runCommand<C extends CommandName>(
  dispatch: CommandDispatcher,
  command: C,
  args?: unknown,
): Promise<DocumentFor<C>> {
  return dispatch(command, args) as Promise<DocumentFor<C>>;
}
