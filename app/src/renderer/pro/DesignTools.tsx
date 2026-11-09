import { IconButton } from "../kit";
import { setDesignView, useDesignView } from "../scene/designView";

/** The Analysis toggle in the Design tool pill; its panel is DesignAnalysis. */
export function DesignTools({ canAnalyse }: { canAnalyse: boolean }) {
  const view = useDesignView();
  return (
    <IconButton icon="cube" label="Analysis" disabled={!canAnalyse} {...(!canAnalyse && { title: "Analysis needs a scene", "aria-label": "Analysis needs a scene" })} pressed={view.analysis} onClick={() => setDesignView({ analysis: !view.analysis })} />
  );
}
