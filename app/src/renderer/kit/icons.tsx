// One in-house line-icon set, 20x20 grid, 1.7 stroke, currentColor.
const PATHS = {
  open: "M3 6h5l2 2h7v8H3z",
  fit: "M3 7V3h4M13 3h4v4M17 13v4h-4M7 17H3v-4",
  labels: "M3 4h9l5 6-5 6H3z",
  recheck: "M16 10a6 6 0 1 1-2-4.5M16 3v4h-4",
  moon: "M15 12.5A6 6 0 0 1 7.5 5a6 6 0 1 0 7.5 7.5z",
  sun: "M10 6.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zM10 2v2M10 16v2M2 10h2M16 10h2M4.3 4.3l1.4 1.4M14.3 14.3l1.4 1.4M4.3 15.7l1.4-1.4M14.3 5.7l1.4-1.4",
  layers: "M10 3l7 4-7 4-7-4zM3 11l7 4 7-4M3 14l7 4 7-4",
  terminal: "M4 6l4 4-4 4M10 14h6",
  history: "M3 10a7 7 0 1 0 2-5M3 3v4h4M10 6v4l3 2",
  search: "M9 3.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11zM13 13l4 4",
  close: "M5 5l10 10M15 5L5 15",
  chevron: "M7 4l6 6-6 6",
  play: "M6 4l10 6-10 6z",
  stop: "M5 5h10v10H5z",
  cube: "M10 2l7 4v8l-7 4-7-4V6zM10 18v-8M3 6l7 4 7-4",
  upload: "M10 13V3M6 7l4-4 4 4M3 13v4h14v-4",
  copy: "M7 7h9v10H7zM4 13V3h9",
  ok: "M4 10.5l4 4 8-9",
  alert: "M10 3l8 14H2zM10 8v4M10 14.5v.5",
  fail: "M5 5l10 10M15 5L5 15",
} as const;

export type IconName = keyof typeof PATHS;
export const ICON_NAMES = Object.keys(PATHS) as IconName[];

export function Icon({ name }: { name: IconName }) {
  return (
    <svg className="kit-icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d={PATHS[name]} />
    </svg>
  );
}
