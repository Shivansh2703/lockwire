// Theme follows the OS by default; a user's explicit choice overrides it and
// persists. All colour VALUES live in theme.css — this file only ever
// reads/writes the `data-theme` attribute and the stored preference, never a
// colour.
export type Theme = "light" | "dark";

const STORAGE_KEY = "lockwire-theme";

function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null; // storage unavailable (private mode, disabled) — fall back to OS
  }
}

function osTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The theme actually in effect right now: the user's stored choice, else the OS preference. */
export function currentTheme(): Theme {
  return storedTheme() ?? osTheme();
}

/** Call once at startup. A prior explicit choice is re-applied; otherwise the
 * OS preference is left to `prefers-color-scheme` in theme.css — no attribute
 * is written, so a later OS change keeps following live. */
export function initTheme(): void {
  const saved = storedTheme();
  if (saved) {
    document.documentElement.setAttribute("data-theme", saved);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // no persistence available; the choice still applies for this session
  }
}

/** Flips the theme and makes it the user's persisted choice, overriding the OS either way. */
export function toggleTheme(): Theme {
  const next: Theme = currentTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  return next;
}
