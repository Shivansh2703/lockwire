import { describe, expect, it, vi } from "vitest";

const showOpenDialog = vi.fn();
vi.mock("electron", () => ({ dialog: { showOpenDialog: (...a: unknown[]) => showOpenDialog(...a) } }));

import { pickRobot } from "./dialogs";

describe("dialogs (main process file picker)", () => {
  it("test_pick_robot_takes_models_and_lockfiles_and_returns_the_chosen_path", async () => {
    showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: ["/tmp/robot/arm.urdf"] });
    const path = await pickRobot();
    expect(path).toBe("/tmp/robot/arm.urdf");
    const opts = showOpenDialog.mock.calls[0]![0] as { filters: { extensions: string[] }[] };
    expect(opts.filters[0]!.extensions).toContain("urdf");
    expect(opts.filters[0]!.extensions).toContain("yaml");
    expect(opts.filters[0]!.extensions).toEqual(expect.arrayContaining(["glb", "gltf"]));
  });

  it("test_a_cancelled_dialog_returns_null_not_an_empty_string", async () => {
    showOpenDialog.mockResolvedValueOnce({ canceled: true, filePaths: [] });
    expect(await pickRobot()).toBeNull();
  });

  it("test_LOCKWIRE_E2E_env_never_replaces_the_native_dialog", async () => {
    Object.assign(process.env, { LOCKWIRE_E2E_OPEN_PATH: "/tmp/e2e/open.yaml", LOCKWIRE_E2E_URDF_PATH: "/tmp/e2e/model.urdf" });
    try {
      showOpenDialog.mockResolvedValue({ canceled: true, filePaths: [] });
      expect(await pickRobot()).toBeNull();
    } finally {
      delete process.env.LOCKWIRE_E2E_OPEN_PATH;
      delete process.env.LOCKWIRE_E2E_URDF_PATH;
    }
  });
});
