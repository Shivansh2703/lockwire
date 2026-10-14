// The last few robots opened, kept in localStorage like the theme (theme.ts).
export interface Recent { path: string; name: string }

const KEY = "lockwire.recent";

export function readRecents(): Recent[] {
  try {
    const list = JSON.parse(window.localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(list) ? list.filter((r) => typeof r?.path === "string" && typeof r?.name === "string").slice(0, 5) : [];
  } catch {
    return [];
  }
}

/** Newest first, no duplicate paths, at most 5. Returns the new list. */
export function pushRecent(item: Recent): Recent[] {
  const next = [item, ...readRecents().filter((r) => r.path !== item.path)].slice(0, 5);
  try {
    window.localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // no persistence; the list still shows for this session
  }
  return next;
}
