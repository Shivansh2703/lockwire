#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { FuseV1Options, FuseVersion, flipFuses } from "@electron/fuses";
import { driveFresh, driveOpen, driveSidecarFailure, driveUrdf, launchApp } from "./e2e-drive.mjs";
import { specPath } from "./stage-sidecar.mjs";

if (process.platform !== "linux" && process.platform !== "darwin") {
  throw new Error("package-linux-e2e requires a Linux or macOS packaged executable");
}

const appRoot = process.cwd();
const repoRoot = join(appRoot, "..");
const packageConfig = JSON.parse(readFileSync(join(appRoot, "package.json"), "utf8"));
const packageName = packageConfig.name;
const productName = packageConfig.build.productName;
const packaged = process.platform === "linux"
  ? join(appRoot, "dist", "linux-unpacked", packageName)
  : join(appRoot, "dist", "mac-arm64", `${productName}.app`, "Contents", "MacOS", productName);
if (!existsSync(packaged)) throw new Error(`packaged app executable missing: ${packaged}`);


// the app under test is driven from outside (scripts/e2e-drive.mjs); it carries no test
// code. Playwright needs Node's inspector, which the shipped build turns off by fuse
// (package.json electronFuses.enableNodeCliInspectArguments=false). So the E2E runs a COPY of the
// packaged app with exactly that one fuse flipped; dist/ itself is never modified.
const fixtureDir = join(appRoot, "node_modules", ".cache", "lockwire-linux-package-e2e");
rmSync(fixtureDir, { recursive: true, force: true });
mkdirSync(fixtureDir, { recursive: true });
mkdirSync(join(fixtureDir, "app-under-test"));
const copyRoot = join(fixtureDir, "app-under-test", process.platform === "linux" ? "linux-unpacked" : `${productName}.app`);
const sourceRoot = process.platform === "linux" ? dirname(packaged) : join(dirname(packaged), "..", "..");
const copied = spawnSync("cp", ["-a", sourceRoot, copyRoot]);
if (copied.status !== 0) throw new Error(`could not copy the packaged app: ${copied.stderr}`);
const executable = process.platform === "linux" ? join(copyRoot, packageName) : join(copyRoot, "Contents", "MacOS", productName);
await flipFuses(copyRoot + (process.platform === "linux" ? `/${packageName}` : ""), {
  version: FuseVersion.V1,
  resetAdHocDarwinSignature: process.platform === "darwin",
  [FuseV1Options.EnableNodeCliInspectArguments]: true,
});
if (process.platform === "linux" && statSync(join(dirname(packaged), "chrome-sandbox")).mode & 0o4000) {
  // The copy lost the sandbox helper's root-owned setuid bit; restore it so the sandbox stays on.
  for (const cmd of [["chown", "root:root"], ["chmod", "4755"]]) {
    const r = spawnSync("sudo", ["-n", ...cmd, join(copyRoot, "chrome-sandbox")]);
    if (r.status !== 0) throw new Error(`could not restore chrome-sandbox: ${r.stderr}`);
  }
}

cpSync(join(repoRoot, "tests/fixtures/x500/contract_corrected.yaml"), join(fixtureDir, "robot.yaml"));
cpSync(join(repoRoot, "tests/fixtures/x500/x500.glb"), join(fixtureDir, "robot.glb"));
const planPath = join(fixtureDir, "workflow-plan.yaml");
writeFileSync(planPath, "steps:\n  - kind: rail_load\n    rail: rail.servo-motor\n    draw: {value: 1, unit: W}\n");
const runRecordingPath = join(fixtureDir, "workflow-run-recording");
cpSync(join(repoRoot, "tests/fixtures/bags/clean"), runRecordingPath, { recursive: true });
const workflow = { planPath, recordingPath: runRecordingPath, run: true };

const sha256 = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");

// One launch per scenario: fresh app, hidden, closed afterwards.
async function session(body) {
  const launched = await launchApp({ executablePath: executable, args: process.platform === "linux" ? ["--use-gl=angle", "--use-angle=swiftshader"] : [],
    cwd: dirname(executable) });
  try {
    return await body(launched);
  } finally {
    await launched.app.close().catch(() => {});
  }
}

async function runOpen(options) {
  const result = await session((launched) => driveOpen({ ...launched, ...options }));
  if (result.visible !== false) throw new Error("packaged app E2E window was visible");
  if (typeof result.headlineText !== "string" || result.headlineText.trim().length === 0) {
    throw new Error("packaged app returned no real check headline");
  }
  const screenshotPaths = result.screenshots.map((item) => item.path);
  result.screenshotEvidence = screenshotPaths.map((path) => {
    const bytes = readFileSync(path);
    if (bytes.length < 8 || !bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
      throw new Error("checkpoint screenshot is not a PNG: " + path);
    }
    return { path, bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") };
  });
  return result;
}

const original = await runOpen({ openPath: join(fixtureDir, "robot.yaml"), ...workflow, screenshotBase: join(fixtureDir, "x500.png") });
if (original.verdictClass !== "verdict-go") throw new Error(`expected exact GO verdict, got ${original.verdictClass}`);

// The actuator review is paid (packaging/pro/ present, as stage-sidecar decides): the free build skips this leg.
const paid = specPath(repoRoot) !== "packaging/lockwire.spec";
let actuatorResult, screenshot, screenshotPath;
if (paid) {
  const actuatorFixture = join(repoRoot, "tests/fixtures/actuator_review_e2e");
  const actuatorDir = join(fixtureDir, "actuator");
  cpSync(actuatorFixture, actuatorDir, { recursive: true });
  const manifest = JSON.parse(readFileSync(join(actuatorDir, "manifest.json"), "utf8"));
  const actuatorPlanPath = join(actuatorDir, "plan.yaml");
  writeFileSync(actuatorPlanPath, "steps:\n  - kind: rail_load\n    rail: rail.12v\n    draw: {value: 500, unit: W}\n");
  screenshotPath = join(fixtureDir, "actuator-proof.png");
  function snapshotTree(root, relative = "") {
    const entries = [];
    for (const name of readdirSync(join(root, relative)).sort()) {
      const path = join(relative, name);
      const stat = statSync(join(root, path));
      if (stat.isDirectory()) entries.push(...snapshotTree(root, path));
      else entries.push([path, createHash("sha256").update(readFileSync(join(root, path))).digest("hex")]);
    }
    return entries;
  }
  const snapshotBefore = snapshotTree(actuatorDir);
  const contractPath = join(actuatorDir, "contract.yaml");
  const evidenceRoot = join(actuatorDir, manifest.evidence_root);
  actuatorResult = await runOpen({
    openPath: contractPath,
    planPath: actuatorPlanPath,
    planStepPath: "plan/steps/0/rail_load/draw",
    screenshotBase: join(fixtureDir, "actuator.png"),
    actuator: {
      caseId: manifest.case_id,
      revision: manifest.contract_revision,
      evidenceRoot,
      evidencePath: manifest.evidence_path,
      evidenceHash: manifest.evidence_sha256,
      captureHash: sha256(join(evidenceRoot, "evidence/capture.json")),
      predictionHash: sha256(join(evidenceRoot, "prediction/run.json")),
      gaps: manifest.expected_simulation_gaps,
      initialGaps: manifest.expected_initial_evidence_gaps,
      screenshotPath,
    },
  });
  screenshot = readFileSync(screenshotPath);
  if (screenshot.length < 8 || !screenshot.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
    throw new Error("actuator screenshot is not a PNG");
  }
  const snapshotAfter = snapshotTree(actuatorDir);
  if (JSON.stringify(snapshotBefore) !== JSON.stringify(snapshotAfter)) throw new Error("packaged actuator review modified its contract or evidence fixture");
  if (!actuatorResult.actuatorProof?.draftReadOnly) throw new Error("packaged actuator review proof missing");
  if (!actuatorResult.actuatorProof.planFinding?.quantity || actuatorResult.actuatorProof.planFinding.quantity !== "plan/steps/0/rail_load/draw") {
    throw new Error("packaged Plan cited finding proof missing");
  }
}
// E2.6: open ANYmal through its display-only same-stem URDF and the checked-in mesh package.
// The contract's selected model URDF remains separate, so display geometry does not bypass the
// contract's external-asset identity checks.
const anymalDir = join(fixtureDir, "anymal");
mkdirSync(anymalDir, { recursive: true });
const anymalContractPath = join(anymalDir, "contract.yaml");
cpSync(join(repoRoot, "tests/fixtures/anymal/contract.yaml"), anymalContractPath);
cpSync(join(repoRoot, "tests/fixtures/anymal/contract.urdf"), join(anymalDir, "contract.urdf"));
// `lockwire init` stamped model.source as `../anymal_d.urdf` (relative to the
// contract) -- mirror that same layout here so `check`'s structure-hash comparison
// finds the URDF instead of reporting drift as not-scored for the wrong reason.
cpSync(join(repoRoot, "tests/fixtures/anymal_d.urdf"), join(fixtureDir, "anymal_d.urdf"));
cpSync(join(repoRoot, "tests/fixtures/e5_10_real_scenarios/official/anymal-d"),
  join(fixtureDir, "e5_10_real_scenarios/official/anymal-d"), { recursive: true });
const anymalShaBefore = sha256(anymalContractPath);
const anymalOpen = await runOpen({ openPath: anymalContractPath, ...workflow, screenshotBase: join(fixtureDir, "anymal.png") });
if (!anymalOpen.headlineText.trim()) throw new Error("ANYmal packaged open returned no headline");
// A freshly-inited contract honestly is GO with notes (32 zero-mass-link warnings,
// `lockwire init`'s own report) -- not exact GO like X500's hand-corrected fixture.
if (anymalOpen.verdictClass !== "verdict-go-with-notes") {
  throw new Error(`expected ANYmal's real GO-with-notes verdict, got ${anymalOpen.verdictClass}`);
}
if (!anymalOpen.designPixel || !anymalOpen.design?.selectedPart) {
  throw new Error("ANYmal packaged open did not render its mesh scene");
}
const anymalReopen = await runOpen({ openPath: anymalContractPath });
if (anymalReopen.headlineText !== anymalOpen.headlineText) {
  throw new Error(`ANYmal reopen headline changed: ${anymalOpen.headlineText} -> ${anymalReopen.headlineText}`);
}
if (sha256(anymalContractPath) !== anymalShaBefore) throw new Error("ANYmal contract fixture changed sha256 across packaged opens");

// "Open robot" on a model: cancel, reopen (no prompt), overwrite, stale. Proven by what the user sees and by the
// bytes left on disk. (The old in-process witness of the IPC argument expected_revision is gone
// with the test hooks; a stale write is proven by the refusal text plus the external bytes surviving.)
const urdfSource = join(repoRoot, "tests/fixtures/anymal/contract.urdf");
const urdfExternalBytes = "# external writer changed these bytes after the app observed its revision\n";
function stageUrdfScenario(name) {
  const scenarioDir = join(fixtureDir, `urdf-${name}`);
  mkdirSync(scenarioDir, { recursive: true });
  const urdfPath = join(scenarioDir, "robot.urdf");
  const contract = join(scenarioDir, "lockwire.yaml");
  cpSync(urdfSource, urdfPath);
  cpSync(join(repoRoot, "tests/fixtures/anymal/contract.yaml"), contract);
  return { urdfPath, contract, before: readFileSync(contract) };
}
async function runUrdf(name) {
  const staged = stageUrdfScenario(name);
  const result = await session((launched) => driveUrdf({ ...launched, scenario: name, urdfPath: staged.urdfPath,
    externalWrite: () => writeFileSync(staged.contract, urdfExternalBytes), screenshotBase: join(fixtureDir, `urdf-${name}.png`) }));
  if (result.visible !== false) throw new Error("packaged app E2E window was visible");
  return { result, staged, after: readFileSync(staged.contract) };
}
const cancelRun = await session((launched) => driveUrdf({ ...launched, scenario: "cancel", screenshotBase: join(fixtureDir, "urdf-cancel.png") }));
if (!cancelRun.urdfCancel || !cancelRun.openFace || cancelRun.check || cancelRun.error || cancelRun.visible !== false) {
  throw new Error(`packaged URDF cancel proof failed: ${JSON.stringify(cancelRun)}`);
}
const urdfKeep = await runUrdf("reopen");
if (!urdfKeep.staged.before.equals(urdfKeep.after)) throw new Error("packaged URDF reopen changed the existing lockfile bytes");
const urdfOverwrite = await runUrdf("overwrite");
if (urdfOverwrite.staged.before.equals(urdfOverwrite.after)) throw new Error("packaged URDF overwrite did not change the existing lockfile revision");
const urdfStale = await runUrdf("stale");
if (!urdfStale.after.equals(Buffer.from(urdfExternalBytes)) || !urdfStale.result.renderedError?.includes("changed since")) {
  throw new Error(`packaged stale URDF overwrite was not refused or wrote after the external byte change: ${JSON.stringify(urdfStale.result)}`);
}

// The aha distance: no lockfile, one "Open robot" click, findings on screen with no more input.
async function runFresh(name, sourceDir, urdfRel, expectText) {
  const dir = join(fixtureDir, `fresh-${name}`);
  cpSync(sourceDir, dir, { recursive: true });
  return session((launched) => driveFresh({ ...launched, urdfPath: join(dir, urdfRel), expectText }));
}
const freshQarm = await runFresh("qarm", join(repoRoot, "tests/fixtures/qarm"), "QARM.urdf", "joint effort limit from the datasheet");
const freshAnymal = await runFresh("anymal", join(repoRoot, "tests/fixtures/e5_10_real_scenarios/official/anymal-d"), "urdf/anymal.urdf");
if (freshQarm.visible !== false || freshAnymal.visible !== false) throw new Error("packaged app E2E window was visible");

const resourceDir = process.platform === "linux"
  ? join(dirname(executable), "resources", `sidecar-${process.platform}-${process.arch}`)
  : join(dirname(executable), "..", "Resources", `sidecar-${process.platform}-${process.arch}`);
const missingSidecarBackup = `${resourceDir}.package-e2e-backup`;
if (!existsSync(resourceDir) || existsSync(missingSidecarBackup)) throw new Error(`cannot isolate packaged sidecar for missing-sidecar proof: ${resourceDir}`);
let missingSidecar;
try {
  renameSync(resourceDir, missingSidecarBackup);
  missingSidecar = await session((launched) => driveSidecarFailure({ ...launched, screenshotBase: join(fixtureDir, "failure.png") }));
} finally {
  if (existsSync(missingSidecarBackup)) renameSync(missingSidecarBackup, resourceDir);
}
if (missingSidecar.visible !== false) throw new Error("packaged app E2E window was visible");

console.log(JSON.stringify({ platform: process.platform, executable, basicHiddenGo: original.verdictClass,
  actuatorReview: actuatorResult?.actuatorProof, screenshot: { path: screenshotPath, bytes: screenshot?.length },
  fixtureUnchanged: true, visible: actuatorResult?.visible,
  x500Screenshots: original.screenshotEvidence, anymalScreenshots: anymalOpen.screenshotEvidence,
  x500Workflows: { plan: original.planVerdict?.verdict, test: original.testVerdict, run: original.runProof, declare: original.declareProof, reference: original.referenceProof },
  anymalHeadline: anymalOpen.headlineText, anymalVerdictClass: anymalOpen.verdictClass,
  anymalWorkflows: { plan: anymalOpen.planVerdict?.verdict, test: anymalOpen.testVerdict, run: anymalOpen.runProof, declare: anymalOpen.declareProof, reference: anymalOpen.referenceProof },
  anymalReopenHeadlineUnchanged: true, anymalShaUnchanged: true,
  freshQarm, freshAnymal,
  urdfCancel: cancelRun,
  urdfKeep: { ...urdfKeep.result, bytesUnchanged: true },
  urdfOverwrite: { ...urdfOverwrite.result, bytesChanged: true },
  urdfStale: { ...urdfStale.result, bytesEqualExternalWrite: true },
  missingSidecar: missingSidecar.failure }));
