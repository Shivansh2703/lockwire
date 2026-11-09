import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RunFace } from "./RunFace";
import { CLEAN_RUN_PAYLOAD, DAY_ONE_RUN_PAYLOAD } from "./runFace.fixture";

function mockMatchMedia(): void {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  mockMatchMedia();
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("RunFace", () => {
  it("test_absent_devices_are_quiet_rows_not_loud_ones", () => {
    // a declared device that recorded nothing is "declared, not seen" -- not measured, not a fault.
    const gone = { kind: "device", state: "absent", word: "declared, not seen", run: null, reason: "no samples: /imu/data", runs: 1 } as const;
    root = createRoot(container);
    act(() => root.render(<RunFace payload={{ ...CLEAN_RUN_PAYLOAD, devices: [{ id: "dev.a", ...gone }, { id: "dev.b", ...gone }] }} />));
    expect(container.querySelectorAll(".device-row").length).toBe(1); // folded into the quiet group
    expect(container.querySelector(".device-id")?.textContent).toBe("2 devices");
    expect(container.querySelector(".device-word")?.textContent).toBe("Declared, not seen");
  });

  it("test_a_populated_ledger_shows_the_latest_row_per_device_never_the_day_one_text", () => {
    root = createRoot(container);
    act(() => root.render(<RunFace payload={CLEAN_RUN_PAYLOAD} />));

    const rows = [...container.querySelectorAll(".device-row")];
    const quiet = CLEAN_RUN_PAYLOAD.devices.filter((d) => d.state === "not_watched").length;
    expect(rows.length).toBe(CLEAN_RUN_PAYLOAD.devices.length - quiet + 1); // identical quiet devices fold into one "N devices" row
    expect(container.querySelector(".frame")).toBeNull();
    expect(container.querySelector(".device-row .device-id")?.textContent).toBe("dev.barometer");
    expect(container.querySelector(".device-row .device-word")?.textContent).toBe("Inside its bands");
    expect(container.querySelector(".device-updated")).toBeNull(); // the run time is the JobBar's, once
    expect(container.querySelector(".device-run")).toBeNull();
    expect(container.querySelector(".day-one-notice")).toBeNull();
  });

  it("test_a_silent_device_reads_No_data_never_over_a_limit_or_its_id", () => {
    root = createRoot(container);
    const silent = { id: "adis_imu", name: "IMU", kind: "device", state: "faulty" as const, word: "over a limit", run: null, runs: 1,
      reason: "no samples: /imu/data", coverage: { reason_code: "silent" } };
    act(() => root.render(<RunFace payload={{ ...CLEAN_RUN_PAYLOAD, devices: [silent] }} />));
    const row = container.querySelector(".device-row")!;
    expect(row.textContent).toBe("IMUNo data");
  });

  it("test_a_running_empty_ledger_says_waiting_for_data", () => {
    root = createRoot(container);
    act(() => root.render(<RunFace payload={DAY_ONE_RUN_PAYLOAD} running />));
    expect(container.querySelector(".day-one-notice")?.textContent).toBe("Waiting for data");
  });

  it("test_an_empty_ledger_shows_the_day_one_sentence_verbatim_and_no_chart_furniture", () => {
    root = createRoot(container);
    act(() => root.render(<RunFace payload={DAY_ONE_RUN_PAYLOAD} />));

    expect(container.querySelectorAll(".device-row").length).toBe(0);
    const notice = container.querySelector(".day-one-notice");
    expect(notice?.textContent).toBe("Nothing compared yet");
    expect(container.querySelector(".device-rows")).toBeNull();
  });

  it("leaves verdict and cancellation to the root frame", () => {
    const onCancel = vi.fn();
    root = createRoot(container);
    act(() => root.render(<RunFace payload={CLEAN_RUN_PAYLOAD} running onCancel={onCancel} />));

    const button = container.querySelector<HTMLButtonElement>("button.cancel-job");
    expect(button).toBeNull();
    expect(container.querySelector(".headline")).toBeNull();

    act(() => root.render(<RunFace payload={CLEAN_RUN_PAYLOAD} running={false} onCancel={onCancel} />));
    expect(container.querySelector("button.cancel-job")).toBeNull();
  });

  it("does not render application navigation", () => {
    root = createRoot(container);
    act(() => root.render(<RunFace payload={CLEAN_RUN_PAYLOAD} />));
    const selected = container.querySelector('[role="tab"][aria-selected="true"]');
    expect(selected).toBeNull();
  });
});
