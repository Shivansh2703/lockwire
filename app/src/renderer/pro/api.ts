import type { ProPreloadApi } from "../../preload/pro";

/** The paid half of window.lockwire (preload/pro). Absent in a free build, where nothing here is mounted. */
export const proApi = (): ProPreloadApi => window.lockwire as unknown as ProPreloadApi;
