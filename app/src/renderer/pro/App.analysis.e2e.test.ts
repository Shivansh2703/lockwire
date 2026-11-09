import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The paid structural-analysis proof, split from ../App.e2e.test.ts. Same driver, same launch gate:
// scripts/e2e-drive.mjs drives the built app from outside; the app carries no test code.
async function drive(
  options: { args?: string[]; env?: NodeJS.ProcessEnv } & Record<string, unknown>,
  body: "driveOpen",
): Promise<Record<string, any>> { // eslint-disable-line @typescript-eslint/no-explicit-any
  const driver = await import(/* @vite-ignore */ pathToFileURL(join(process.cwd(), "scripts", "e2e-drive.mjs")).href);
  const electronBin = createRequire(import.meta.url)("electron") as unknown as string;
  const { args, env, ...rest } = options;
  const launched = await driver.launchApp({ executablePath: electronBin, args: args ?? ["."], cwd: process.cwd(), env });
  try {
    return await driver[body]({ ...launched, ...rest });
  } finally {
    await launched.app.close().catch(() => {});
  }
}

function expectNoTestHooksInMain(mainEntry: string): void {
  expect(readFileSync(mainEntry, "utf8").includes("LOCKWIRE_E2E"), `the built main entry still reads LOCKWIRE_E2E_*: ${mainEntry}`).toBe(false);
}

describe("the app carries a real B31 analysis Job through IPC to the rendered result", () => {
  it(
    "test_hidden_real_pinned_b31_job_reaches_typed_design_result",
    async () => {
      const cwd = process.cwd();
      const repoRoot = join(cwd, "..");
      const requiredArtifacts = [join(cwd, "out", "main", "index.js"), join(cwd, "out", "preload", "index.js"), join(cwd, "out", "renderer", "index.html")];
      const missingArtifacts = requiredArtifacts.filter((path) => !existsSync(path));
      if (missingArtifacts.length > 0) {
        throw new Error(`Built app artifacts required before this test. Run npm run build. Missing: ${missingArtifacts.join(", ")}`);
      }
      expectNoTestHooksInMain(requiredArtifacts[0]!);
      const solver = process.env.LOCKWIRE_CCX;
      if (!solver || !solver.startsWith("/") || !solver.endsWith("/ccx_2.23") || !existsSync(solver)) {
        throw new Error("LOCKWIRE_CCX must name the absolute pinned ccx_2.23 executable");
      }

      const fixtureDir = join(cwd, "node_modules", ".cache", "lockwire-b31-analysis-e2e");
      rmSync(fixtureDir, { recursive: true, force: true });
      mkdirSync(fixtureDir, { recursive: true });
      const setup = spawnSync("uv", ["run", "python", "-c", [
        "import json, sys",
        "from pathlib import Path",
        "sys.path.insert(0, 'tests')",
        "from test_structural_analysis import _prepared_declared_b31_cantilever",
        "contract, evidence, manifest = _prepared_declared_b31_cantilever(Path(sys.argv[1]), limit_metric='b31_tip_uy')",
        "print(json.dumps({'contract': str(contract), 'evidence': str(evidence), 'manifest': str(manifest)}))",
      ].join("\n"), fixtureDir], { cwd: repoRoot, encoding: "utf8", timeout: 30_000 });
      if (setup.status !== 0) {
        throw new Error(`could not prepare the existing declared B31 fixture: ${setup.stderr}`);
      }
      const fixture = JSON.parse(setup.stdout.trim()) as { contract: string; evidence: string; manifest: string };
      const contractRevision = createHash("sha256").update(readFileSync(fixture.contract)).digest("hex");
      const manifestSha256 = createHash("sha256").update(readFileSync(fixture.manifest)).digest("hex");
      const result = await drive({
        openPath: fixture.contract,
        analysis: { procedure: "static", evidence: fixture.evidence, manifest: fixture.manifest },
      }, "driveOpen") as {
        visible: boolean;
        structuralAnalysis?: {
          status: string;
          contractRevision: string;
          packageManifestSha256: string;
          solverBinarySha256: string;
          outputSha256: string;
          physicalConformance: string;
          simulationValidity: string;
          visibleText: string;
          parsedResult: string;
          pollContractRevision: string;
          pollPackageManifestSha256: string;
          pollSolverBinarySha256: string;
          pollOutputSha256: string;
          pollSimulationValidity: string;
          pollPhysicalConformance: string;
          pollContainsOutputText: boolean;
          pollOutput: string;
          pollErrors: string;
        };
      };
      expect(result.visible).toBe(false);
      expect(result.structuralAnalysis).toMatchObject({
        status: "Analysis job: completed",
        contractRevision,
        packageManifestSha256: manifestSha256,
        solverBinarySha256: "393c0764e95bb9e6aea5b8de45387a81ebf729587a3650063e2c7c3fa74bb8aa",
        physicalConformance: "Physical conformance: NOT_SCORED",
        simulationValidity: "Simulation validity: GO",
        pollContractRevision: contractRevision,
        pollPackageManifestSha256: manifestSha256,
        pollSolverBinarySha256: "393c0764e95bb9e6aea5b8de45387a81ebf729587a3650063e2c7c3fa74bb8aa",
        pollSimulationValidity: "GO",
        pollPhysicalConformance: "NOT_SCORED",
        pollContainsOutputText: false,
        pollOutput: "",
        pollErrors: "",
      });
      expect(result.structuralAnalysis?.outputSha256).toMatch(/^[a-f0-9]{64}$/);
      expect(result.structuralAnalysis?.pollOutputSha256).toBe(result.structuralAnalysis?.outputSha256);
      expect(result.structuralAnalysis?.parsedResult).toContain("b31_tip_uy");
      expect(result.structuralAnalysis?.parsedResult).toContain("b31_root_force_balance");
      expect(result.structuralAnalysis?.visibleText).not.toContain("output_text");
      expect(result.structuralAnalysis?.visibleText).not.toContain("displacements (vx,vy,vz)");
      const parsed = JSON.parse(result.structuralAnalysis!.parsedResult) as {
        results: { kind: string; node_set: string; node: number; values: number[] }[];
        comparisons: { metric: string; passed: boolean }[];
      };
      const tip = parsed.results.find((row) => row.kind === "U" && row.node_set === "TIP" && row.node === 5);
      const root = parsed.results.find((row) => row.kind === "RF" && row.node_set === "ROOT" && row.node === 1);
      expect(tip).toBeTruthy();
      expect(root).toBeTruthy();
      expect(Math.abs(tip!.values[1]! + 0.19047619047619047)).toBeLessThanOrEqual(0.19047619047619047 * 0.02);
      expect(Math.abs(root!.values[1]! - 100)).toBeLessThanOrEqual(2);
      expect(parsed.comparisons).toEqual(expect.arrayContaining([
        expect.objectContaining({ metric: "b31_tip_uy", passed: true }),
        expect.objectContaining({ metric: "b31_root_force_balance", passed: true }),
      ]));
    },
    90_000,
  );
});
