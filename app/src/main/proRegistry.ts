import type { CheckPayload, PagePayload, WhatIfPayload } from "../shared/api.gen";

// The one place main/ learns whether the paid handlers are present: main/pro/index.ts, found by
// glob so a tree without pro/ still compiles and simply has the free IPC surface. The classes
// stay an explicit list the pro module exports; ipc.ts registers exactly those names.
export interface ProMain {
  classes: readonly string[];
  handlers: Record<string, (args: unknown) => Promise<unknown>>;
  /** Replies the paid sidecar extends (page, check, what-if) validate here instead of the free schema. */
  validators: {
    page: (payload: unknown) => PagePayload;
    check: (payload: unknown) => CheckPayload;
    whatIf: (payload: unknown) => WhatIfPayload;
  };
}

const found = import.meta.glob<ProMain>("./pro/index.ts", { eager: true, import: "default" });
export const proMain: ProMain | null = Object.values(found)[0] ?? null;
