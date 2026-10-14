import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import type {
  CheckPayload,
  DescribePayload,
  EditPayload,
  PlanPayload,
} from "./api.gen";
import { runCommand, type CommandDispatcher } from "./client";

const SHARED = import.meta.dirname;
const SKIP = new Set(["node_modules"]);

/** Every `.ts`/`.tsx` file under `app/src`, generated file excluded -- the same tree
 * `branding.test.ts` sweeps for the product name, restricted to source. */
function appSourceFiles(): string[] {
  const src = join(SHARED, "..");
  return readdirSync(src, { recursive: true, withFileTypes: true })
    .filter(
      (e) =>
        e.isFile() &&
        /\.tsx?$/.test(e.name) &&
        e.name !== "api.gen.ts" &&
        !e.parentPath.split("/").some((p) => SKIP.has(p)),
    )
    .map((e) => join(e.parentPath, e.name));
}

describe("client.ts is typed only off api.gen.ts (e2-b's charter, finished by e2-b2)", () => {
  it("test_every_union_member_is_reachable_from_the_client_type", async () => {
    const seen: Array<[string, unknown]> = [];
    const dispatch: CommandDispatcher = async (command, args) => {
      seen.push([command, args]);
      return { command };
    };

    // Each assignment below only compiles if `runCommand`'s return type narrows correctly for
    // that command's own artifact_type -- if the generated union ever drops or renames a member,
    // one of these lines stops typechecking (the paid members are checked in pro/client.pro.test.ts).
    const check: CheckPayload = (await runCommand(dispatch, "lockwire-check-report", {})) as CheckPayload;
    const plan: PlanPayload = (await runCommand(dispatch, "lockwire-plan-report", {})) as PlanPayload;
    const describe_: DescribePayload = (await runCommand(dispatch, "lockwire-describe", {})) as DescribePayload;
    const edit: EditPayload = (await runCommand(dispatch, "lockwire-edit-result", {})) as EditPayload;

    expect(
      [check, plan, describe_, edit].map((r) => (r as unknown as { command: string }).command),
    ).toEqual([
        "lockwire-check-report",
        "lockwire-plan-report",
        "lockwire-describe",
        "lockwire-edit-result",
      ],
    );
    expect(seen).toHaveLength(4);
  });

  it("test_an_unserved_command_name_does_not_typecheck", async () => {
    const dispatch: CommandDispatcher = vi.fn(async () => ({}));
    // "lockwire-does-not-exist-report" is not a member of CommandName (api.gen.ts's
    // MachineDocument["artifact_type"]). If runCommand's parameter were ever widened past that
    // union -- to `string`, or to `any` via a loosened generic -- this line would stop erroring
    // and `npm run typecheck` would report "Unused '@ts-expect-error' directive", which is the
    // bite proving this guard is load-bearing rather than a comment.
    // @ts-expect-error -- not a CommandName; see the comment above.
    await runCommand(dispatch, "lockwire-does-not-exist-report", {});
  });

  it("test_the_generated_file_carries_its_do_not_edit_sentinel", () => {
    const generated = readFileSync(join(SHARED, "api.gen.ts"), "utf8");
    expect(generated).toContain("// GENERATED — do not edit by hand.");
  });

  it("test_no_hand_written_reply_interface_exists_in_the_app", () => {
    // Every top-level `MachineDocument` member's name -- a hand-rolled reply type sharing a
    // generated document's name (or any of its siblings), declared anywhere outside api.gen.ts,
    // is exactly the drift this test exists to prevent: a second, driftable definition of
    // a shape the generator already owns. (This comment names the pattern in prose deliberately
    // without spelling "interface" next to a document name, so the sweep below never flags itself.)
    const names = ["CheckPayload", "PlanPayload", "DescribePayload", "EditPayload"];
    const offenders: string[] = [];
    for (const file of appSourceFiles()) {
      const text = readFileSync(file, "utf8");
      for (const name of names) {
        if (new RegExp(`\\binterface\\s+${name}\\b`).test(text)) offenders.push(`${file}: interface ${name}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
