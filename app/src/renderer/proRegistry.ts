import type { ComponentType, ReactNode } from "react";

import type { CheckPayload, PagePayload, PlanPayload, RobotJoint } from "../shared/api.gen";
import type { Highlight } from "./scene/robotScene";

// The seam between the free app and the optional paid UI in ./pro/. Free code only ever sees these
// shapes; ./pro/index.tsx fills them. No ./pro/ folder = `pro` is null, every slot is empty, and
// the app is the free app. Nothing paid is named here.

/** What a pro page hands back so the robot view can light the parts a result names. */
export type ProResult = Pick<PlanPayload, "findings" | "finding_labels">;

export interface ProMode {
  id: string;
  label: string;
  /** Needs an open robot; without one the app shows its "No robot open" empty state. */
  needsRobot?: boolean;
  /** The page's result drives the robot-view highlight and the finding selection. */
  result?: boolean;
  /** The page publishes its own highlight through onHighlight. */
  highlights?: boolean;
}

export interface ProPagesProps {
  mode: string;
  contract?: string;
  joints: RobotJoint[];
  sceneIds: string[];
  result: ProResult | null;
  onResult: (result: ProResult | null) => void;
  selected: number | null;
  onSelect: (index: number | null) => void;
  onHighlight: (highlights: Highlight[]) => void;
}

export interface ProPlugin {
  modes: ProMode[];
  /** Mounted for the whole session beside the free pages (a running job survives a tab switch). */
  Pages: ComponentType<ProPagesProps>;
  /** Extra pills in the Check page header, or null. */
  checkPills?: (page: PagePayload | null) => ReactNode;
  /** Extra section at the foot of the Check page. */
  CheckMore?: ComponentType<{ contract?: string; expectedRevision?: string; check: CheckPayload | null }>;
  /** Extra buttons in the Design tool pill. */
  DesignTools?: ComponentType<{ canAnalyse: boolean }>;
  /** Extra panel on the Design page, shown when the design view asks for it. */
  DesignAnalysis?: ComponentType<{ contract: string; expectedRevision: string }>;
}

const found = import.meta.glob<ProPlugin>("./pro/index.tsx", { eager: true, import: "default" });
export const pro: ProPlugin | null = Object.values(found)[0] ?? null;
