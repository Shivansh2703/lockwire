// The paid preload methods, found by glob: a tree without preload/pro/ exposes only the free API.
// Each method in preload/pro/index.ts still hardcodes its own command class (no generic invoke).
export type ProPreload = (run: <T>(commandClass: string, args: unknown) => Promise<T>) => object;

const found = import.meta.glob<ProPreload>("./pro/index.ts", { eager: true, import: "default" });
export const proApi: ProPreload | null = Object.values(found)[0] ?? null;
