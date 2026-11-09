import type { ProPlugin } from "../proRegistry";
import { CheckMore, checkPills } from "./CheckMore";
import { DesignAnalysis } from "./DesignAnalysis";
import { DesignTools } from "./DesignTools";
import { Pages } from "./Pages";
import "./pro.css";

// The paid UI, as the one object renderer/proRegistry.ts finds by glob. Delete this folder and the
// free app compiles and runs without Test, Run, the device-state pill, actuator cases or analysis.
const plugin: ProPlugin = {
  modes: [
    { id: "Test", label: "Test", needsRobot: true, result: true },
    { id: "Run", label: "Run", highlights: true },
  ],
  Pages,
  checkPills,
  CheckMore,
  DesignTools,
  DesignAnalysis,
};

export default plugin;
