import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";

import type { PagePayload } from "../../shared/api.gen";
import { checkPills } from "./CheckMore";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const page = (health: unknown, health_words: Record<string, string>) => ({ health, health_words }) as unknown as PagePayload;

describe("the Check page's Device state pill (pro slot)", () => {
  it("shows the page payload's own word for the worst device state", () => {
    const box = document.createElement("div");
    const root = createRoot(box);
    act(() => root.render(<>{checkPills(page({ worst: "degrading" }, { degrading: "drifting" }))}</>));
    expect(box.textContent).toBe("Device state: drifting");
    act(() => root.unmount());
  });

  it("shows nothing when no device is watched", () => {
    expect(checkPills(page(null, {}))).toBeNull();
    expect(checkPills(null)).toBeNull();
    expect(checkPills(page({ worst: null }, { healthy: "inside its bands" }))).toBeNull();
  });
});
