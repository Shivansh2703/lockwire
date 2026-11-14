import type { ComponentType } from "react";

import type { PagePayload } from "../shared/api.gen";

// The side-panel slot (F8). A feature box registers its panel by name here; SideDrawer shows a
// tab for it only while `hasData(page)` is true, so nothing new appears until the robot has
// something to show. Nothing is registered today. Power, Controls and Wiring are the planned names.
export interface PanelEntry {
  id: string;
  label: string;
  /** True once the opened robot has something for this panel. */
  hasData: (page: PagePayload) => boolean;
  Body: ComponentType<{ page: PagePayload; contract?: string; revision?: string; /** Writes one declared fact (path, value, source), reloads the robot, returns the refusal or null. */ onDeclare?: (path: string, value: string, source: string) => Promise<string | null>; /** Writes one pin-to-pin wire through the engine (id, from pin key, to pin key); returns the refusal or null. */ onAddWire?: (id: string, from: string, to: string) => Promise<string | null> }>;
}

export const panels: PanelEntry[] = [];
export const registerPanel = (p: PanelEntry) => { if (!panels.some((q) => q.id === p.id)) panels.push(p); };
/** The panels with something to show for this robot, in registration order. */
export const visiblePanels = (page: PagePayload | null | undefined) => (page ? panels.filter((p) => p.hasData(page)) : []);
