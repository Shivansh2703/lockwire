// The Workbench kit: small parts every screen reuses. Styling lives in
// theme.css under `.kit-*`; nothing here carries a colour of its own.
import {
  createContext,
  useContext,
  useEffect,
  useId,
  useState,
  type ButtonHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { createPortal } from "react-dom";

import { Icon, type IconName } from "./icons";

export type Status = "go" | "warn" | "bad" | "muted";
const cx = (...c: (string | false | undefined)[]) => c.filter(Boolean).join(" ");

/* ---------- layout ---------- */

type Slot = "top" | "topLeft" | "topRight" | "left" | "right" | "bottom" | "bottomLeft";
const SLOTS: Slot[] = ["top", "topLeft", "topRight", "left", "right", "bottom", "bottomLeft"];

export function Workspace({
  children,
  ...slots
}: { children?: ReactNode } & Partial<Record<Slot, ReactNode>>) {
  return (
    <div className="kit-workspace">
      <div className="kit-canvas">{children}</div>
      {SLOTS.map((s) => (slots[s] ? <div key={s} className={`kit-slot kit-slot-${s}`}>{slots[s]}</div> : null))}
    </div>
  );
}

const FooterCtx = createContext<HTMLElement | null>(null);

/** Primary actions that live in the Panel's pinned footer even when a deep child owns them (a sibling of the scroll body, so it never overlaps rows). Inline when there is no Panel. */
export function PanelFooter({ children }: { children?: ReactNode }) {
  const slot = useContext(FooterCtx);
  return slot ? createPortal(children, slot) : <>{children}</>;
}

export function Panel({
  title,
  actions,
  collapsible,
  footer,
  children,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  collapsible?: boolean;
  /** Pinned below the scrolling body; for the page's primary actions. */
  footer?: ReactNode;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(true);
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  return (
    <section className="kit-panel">
      {(title || actions) && (
        <header className="kit-panel-head">
          {collapsible ? (
            <button type="button" className="kit-panel-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
              <Icon name="chevron" />
              {title}
            </button>
          ) : (
            <h2 className="kit-panel-title">{title}</h2>
          )}
          {actions}
        </header>
      )}
      {open && <FooterCtx.Provider value={slot}><div className="kit-panel-body">{children}</div></FooterCtx.Provider>}
      <div className="kit-panel-footer" ref={setSlot}>{footer}</div>
    </section>
  );
}

/* ---------- controls ---------- */

export function PillTabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
  disabled,
}: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  label?: string;
  disabled?: boolean;
}) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = tabs.findIndex((t) => t.id === value);
    const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (j === undefined) return;
    e.preventDefault();
    const next = tabs[(j + tabs.length) % tabs.length];
    if (!next) return;
    onChange(next.id);
    e.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]')[(j + tabs.length) % tabs.length]?.focus();
  };
  return (
    <div className="kit-tabs" role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={t.id === value}
          tabIndex={t.id === value ? 0 : -1}
          disabled={disabled}
          className="kit-tab"
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function IconButton({
  icon,
  label,
  pressed,
  ...rest
}: { icon: IconName; label: string; pressed?: boolean } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type="button" title={label} aria-pressed={pressed} className="kit-iconbtn" {...rest}>
      <Icon name={icon} />
      <span className="kit-sr">{label}</span>
    </button>
  );
}

export function ToolPill({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <div className="kit-toolpill" role="toolbar" aria-label={label}>
      {children}
    </div>
  );
}

export function Button({
  variant = "secondary",
  ...rest
}: { variant?: "primary" | "secondary" | "quiet" } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button type="button" className={`kit-btn kit-btn-${variant}`} {...rest} />;
}

type FieldProps = {
  label: ReactNode;
  hint?: string;
  error?: string;
  control?: "input" | "textarea" | "select";
  children?: ReactNode;
} & Record<string, unknown>;

export function Field({ label, hint, error, control = "input", children, ...rest }: FieldProps) {
  const C = control;
  const errId = useId();
  return (
    <label className="kit-field" title={hint}>
      <span className="kit-field-label">{label}</span>
      <C className="kit-control" aria-invalid={error ? true : undefined} aria-describedby={error ? errId : undefined} {...rest}>
        {children as never}
      </C>
      {error && (
        <span id={errId} className="kit-field-error" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  label?: string;
}) {
  const name = useId();
  return (
    <div className="kit-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <label key={o.value} className="kit-seg-opt">
          <input type="radio" name={name} checked={o.value === value} onChange={() => onChange(o.value)} />
          <span>{o.label}</span>
        </label>
      ))}
    </div>
  );
}

export function StatusPill({ status, children }: { status: Status; children: ReactNode }) {
  // Status never by colour alone: an empty pill still says its state.
  return <span className={`kit-pill kit-s-${status}`}>{children || { go: "OK", warn: "Check", bad: "Problem", muted: "None" }[status]}</span>;
}

/* ---------- findings and numbers ---------- */

export function FindingRow({
  severity,
  title,
  chips,
  note,
  selected,
  onSelect,
}: {
  severity: Status;
  title: ReactNode;
  /** A chip is its short label; `title` carries the full id for the tooltip. */
  chips?: (string | { label: string; title?: string })[];
  /** One muted line after the chips (e.g. the result when there are no chips). */
  note?: ReactNode;
  selected?: boolean;
  onSelect?: () => void;
}) {
  return (
    <button type="button" className={cx("kit-finding", selected && "is-selected")} aria-pressed={!!selected} onClick={onSelect}>
      <span className={`kit-mark kit-s-${severity}`} aria-hidden="true">{MARK[severity] ? <Icon name={MARK[severity]} /> : <i className="kit-dot" />}</span>
      <span className="kit-sr">{SEVERITY_WORD[severity]}: </span>
      <span className="kit-finding-head">
        <span className="kit-finding-title">{title}</span>
        {VISIBLE_WORD[severity] && <small className={`kit-finding-word kit-s-${severity}`} aria-hidden="true">{VISIBLE_WORD[severity]}</small>}
      </span>
      {chips && chips.length > 0 && (
        <span className="kit-chips">
          {chips.map((c) => {
            const chip = typeof c === "string" ? { label: c } : c;
            return <b key={chip.title ?? chip.label} className="kit-chip" title={chip.title}>{chip.label}</b>;
          })}
        </span>
      )}
      {note && <span className="kit-finding-note">{note}</span>}
    </button>
  );
}

// Status never by colour alone: bad and warn get an icon and a word; a grey row says why it is grey.
const MARK: Partial<Record<Status, IconName>> = { go: "ok", warn: "alert", bad: "fail" };
const VISIBLE_WORD: Partial<Record<Status, string>> = { warn: "Check", bad: "Fails", muted: "Not checked" };
const SEVERITY_WORD: Record<Status, string> = { go: "Pass", warn: "Warning", bad: "Problem", muted: "Info" };

export type FigureKind = "declared" | "measured" | "derived" | "catalog" | "projected";

/** Round to 4 significant figures, no trailing zeros; integers and strings are left exact. */
export const fmtNum = (v: string | number): string =>
  typeof v === "number" && !Number.isInteger(v) ? String(Number(v.toPrecision(4))) : String(v);

/** a number always carries its kind word; catalog and projected also get the warn mark. */
export function Figure({ value, unit, kind }: { value: string | number; unit?: string; kind: FigureKind }) {
  const unverified = kind === "catalog" || kind === "projected";
  return (
    <span className={cx("kit-figure", unverified && "is-unverified")}>
      <span className="kit-num">
        {fmtNum(value)}
        {unit ? ` ${unit}` : ""}
      </span>
      <span className="kit-kind">
        {unverified && <span aria-hidden="true">{"▲ "}</span>}
        {kind}
      </span>
    </span>
  );
}

export function FindingDetail({ rows }: { rows: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="kit-kv">
      {rows.map((r, i) => (
        <div key={`${r.label}${i}`} className="kit-kv-row">
          <dt>{r.label}</dt>
          <dd>{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export const Inspector = FindingDetail;

type TreePart = { id: string; name: string; state?: Status; depth?: number; tip?: string; sel?: string; sels?: string[]; group?: { id: string; label: string; kind?: string } };
const KIND_RANK: Record<string, number> = { device: 1, rail: 2, bus: 3, link: 4, other: 5 };
const WORST_FIRST: Status[] = ["bad", "warn", "go", "muted"];

/** The part list. Rows carrying a `group` render as collapsible drop-downs: chevron, label,
 * count, worst-state dot. A row's `id` is its unique key; `sel` (default `id`) is what it selects,
 * so several rows may select the same engine part. A group opens by default when a member has a
 * finding (bad/warn) or holds the selected part; a search opens every group that matches. */
export function PartTree({
  parts,
  selected,
  onSelect,
  selectedGroup,
  onSelectGroup,
  placeholder = "Search parts",
}: {
  parts: TreePart[];
  selected?: string;
  onSelect?: (id: string) => void;
  selectedGroup?: string;
  /** Header click: the group id and every part it selects. Without it a header only toggles. */
  onSelectGroup?: (id: string, members: string[]) => void;
  placeholder?: string;
}) {
  const [q, setQ] = useState("");
  const [override, setOverride] = useState<Record<string, boolean>>({});
  const needle = q.trim().toLowerCase();
  const pick = (p: TreePart) => p.sel ?? p.id;
  const holds = (p: TreePart) => selected !== undefined && (p.sels ?? [pick(p)]).includes(selected); // a folded row holds every member
  const selectedGroupOfPart = parts.find(holds)?.group?.id;
  useEffect(() => {
    if (selectedGroupOfPart) setOverride((o) => ({ ...o, [selectedGroupOfPart]: true }));
  }, [selectedGroupOfPart]);
  const row = (p: TreePart) => (
    <li key={p.id}>
      <button type="button" className={cx("kit-tree-row", holds(p) && "is-selected")} aria-pressed={holds(p)} data-part-id={pick(p)} title={p.tip} style={p.depth ? { paddingLeft: `calc(var(--sp-2) + ${p.depth} * var(--sp-3))` } : undefined} onClick={() => onSelect?.(pick(p))}>
        <span>{p.name}</span>
        {p.state && p.state !== "go" && p.state !== "muted" && <i className={`kit-dot kit-s-${p.state}`} title={SEVERITY_WORD[p.state]} />}
      </button>
    </li>
  );
  const search = <input className="kit-control" type="search" placeholder={placeholder} aria-label={placeholder} value={q} onChange={(e) => setQ(e.target.value)} />;
  if (!parts.some((p) => p.group)) {
    return (
      <div className="kit-tree">
        {search}
        <ul>{parts.filter((p) => p.name.toLowerCase().includes(needle)).map(row)}</ul>
      </div>
    );
  }
  const groups = new Map<string, { label: string; kind?: string; all: TreePart[]; shown: TreePart[] }>();
  for (const p of parts) {
    const g = p.group ?? { id: "other", label: "Other", kind: "other" };
    const entry = groups.get(g.id) ?? { label: g.label, kind: g.kind, all: [], shown: [] };
    entry.all.push(p);
    if (!needle || p.name.toLowerCase().includes(needle) || g.label.toLowerCase().includes(needle)) entry.shown.push(p);
    groups.set(g.id, entry);
  }
  const ordered = [...groups.entries()].filter(([, g]) => g.shown.length).sort(([, a], [, b]) => (KIND_RANK[a.kind ?? ""] ?? 0) - (KIND_RANK[b.kind ?? ""] ?? 0));
  const isOpen = (id: string, g: { all: TreePart[] }) =>
    needle ? true : override[id] ?? g.all.some((p) => p.state === "bad" || p.state === "warn" || holds(p));
  return (
    <div className="kit-tree">
      {search}
      <ul>
        {ordered.map(([id, g]) => {
          // one-row groups (Declare) and a single part named like its group (Design) are just the row
          const solo = g.kind === "solo" || (g.all.length === 1 && g.all[0]!.name === g.label); if (solo) return row(g.all[0]!);
          const open = isOpen(id, g);
          const worst = WORST_FIRST.find((st) => g.all.some((p) => p.state === st));
          const members = [...new Set(g.all.map(pick))];
          const toggle = (to = !open) => setOverride((o) => ({ ...o, [id]: to }));
          const keys = (e: KeyboardEvent) => {
            if (e.key === "ArrowRight") { e.preventDefault(); toggle(true); }
            if (e.key === "ArrowLeft") { e.preventDefault(); toggle(false); }
          };
          return (
            <li key={id} className="kit-tree-group" data-open={open}>
              <div className={cx("kit-tree-row", "kit-tree-head", selectedGroup === id && "is-selected")}>
                <button type="button" className="kit-tree-chev" aria-expanded={open} aria-label={`${open ? "Collapse" : "Expand"} ${g.label}`} onClick={() => toggle()} onKeyDown={keys}>
                  <span aria-hidden="true">{open ? "▾" : "▸"}</span>
                </button>
                <button type="button" className="kit-tree-label" aria-pressed={selectedGroup === id} data-group-id={id} onClick={() => { toggle(); onSelectGroup?.(id, members); }} onKeyDown={keys}>
                  <span>{g.label}</span>
                  <span className="kit-tree-count">{g.all.length}</span>
                  {worst && worst !== "go" && worst !== "muted" && <i className={`kit-dot kit-s-${worst}`} title={SEVERITY_WORD[worst]} />}
                </button>
              </div>
              {open && <ul>{g.shown.map(row)}</ul>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ---------- the rest ---------- */

export function Disclosure({ summary, children, open }: { summary: ReactNode; children?: ReactNode; open?: boolean }) {
  return (
    <details className="kit-disclosure" open={open}>
      <summary>{summary}</summary>
      {children}
    </details>
  );
}

export function FilePick({
  label,
  path,
  onPick,
  onRemove,
}: {
  label: string;
  path?: string;
  onPick: () => void;
  onRemove: () => void;
}) {
  return (
    <span className="kit-filepick">
      <Button onClick={onPick}>{label}</Button>
      {path && (
        <span className="kit-cli">
          <span className="kit-cli-text">{path}</span>
          <IconButton icon="close" label="Remove file" onClick={onRemove} />
        </span>
      )}
    </span>
  );
}

export function JobBar({
  status,
  word,
  progress,
  onCancel,
}: {
  status: Status;
  word: string;
  progress?: number;
  onCancel?: () => void;
}) {
  return (
    <div className="kit-jobbar">
      <StatusPill status={status}>{word}</StatusPill>
      <div
        className={cx("kit-bar", progress === undefined && "is-indeterminate")}
        role="progressbar"
        aria-label="Progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress === undefined ? undefined : Math.round(progress * 100)}
      >
        <i style={progress === undefined ? undefined : { width: `${Math.round(progress * 100)}%` }} />
      </div>
      {onCancel && (
        <Button variant="quiet" onClick={onCancel}>
          Cancel
        </Button>
      )}
    </div>
  );
}

export function Notice({
  kind,
  children,
  action,
}: {
  kind: "error" | "info";
  children: ReactNode;
  action?: { label: string; onClick: () => void };
}) {
  return (
    <div className={`kit-notice kit-notice-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <span>{children}</span>
      {action && <Button onClick={action.onClick}>{action.label}</Button>}
    </div>
  );
}

/** Display only: an absolute path in a command shows as `…/parent/file` (middle ellipsis). */
export const shortCommand = (c: string): string =>
  c.split(" ").map((t) => t.replace(/^\/(?:[^/\s]+\/)+([^/\s]+\/[^/\s]+)$/, "…/$1")).join(" ");

// `display` overrides what shows; copy and the title always take the full `command`.
export function CommandLine({ command, display }: { command: string; display?: string }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
    } catch {
      // No clipboard (insecure context, denied): the text stays selectable.
    }
  };
  return (
    <span className="kit-cli" title={command}>
      <code className="kit-cli-text">{display ?? shortCommand(command)}</code>
      <IconButton icon="copy" label="Copy" onClick={copy} />
    </span>
  );
}

export function EmptyState({
  title,
  action,
}: {
  title: string;
  action?: { label: string; onClick: () => void };
}) {
  return (
    <div className="kit-empty">
      <p>{title}</p>
      {action && (
        <Button variant="primary" onClick={action.onClick}>
          {action.label}
        </Button>
      )}
    </div>
  );
}
