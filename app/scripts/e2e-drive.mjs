// Drives a REAL lockwire app from outside: Playwright's Electron support attaches to the
// launched binary, native file dialogs are stubbed from this side, and everything else is the
// renderer's own page. The app carries no test code and reads no LOCKWIRE_E2E_* switch.
//
// The window stays hidden through the product's own LOCKWIRE_GATE_READY_FILE launch gate
// (scripts/production_gates.sh uses it the same way); nothing here adds an env var to the app.
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { _electron } from "playwright-core";

const PNG = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Launch the app hidden. `executablePath` = packaged binary; or pass `args: ["."], cwd` for a built tree. */
export async function launchApp({ executablePath, args = [], cwd, env = {} }) {
  const readyFile = join(mkdtempSync(join(tmpdir(), "lockwire-e2e-")), "ready");
  // Always isolate from the owner's real userData/logs unless the caller passed its own dir.
  if (!args.some((a) => a.startsWith("--user-data-dir"))) {
    args = [...args, `--user-data-dir=${mkdtempSync(join(tmpdir(), "lockwire-e2e-ud-"))}`];
  }
  const app = await _electron.launch({
    executablePath, args, cwd, timeout: 60_000,
    env: { ...process.env, ...env, LOCKWIRE_GATE_READY_FILE: readyFile },
  });
  const page = await app.firstWindow();
  await page.waitForLoadState("load");
  return { app, page };
}

/** Stub the native open dialog. `paths`: { robot, plan, recording }; unset = cancel. */
export async function stubDialogs(app, paths) {
  await app.evaluate(({ dialog }, initial) => {
    globalThis.__lwDialogPaths = initial;
    globalThis.__lwDialogCalls = 0;
    dialog.showOpenDialog = async (...args) => {
      const options = args.at(-1) ?? {};
      const key = options.title === "Open robot" ? "robot"
        : options.filters?.[0]?.name === "Plan" ? "plan" : "recording";
      globalThis.__lwDialogCalls += 1;
      const path = globalThis.__lwDialogPaths[key];
      return path ? { canceled: false, filePaths: [path] } : { canceled: true, filePaths: [] };
    };
  }, paths);
}

export async function waitForSidecar(page) {
  for (let i = 0; i < 600; i += 1) {
    if ((await page.evaluate("window.lockwire.getSidecarState()")).phase === "ready") return;
    await sleep(100);
  }
  throw new Error("sidecar never became ready");
}

const paint = (page) => page.evaluate("new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
const clickTab = (page, name) => page.evaluate(
  `Array.from(document.querySelectorAll('.shell-tabs [role="tab"]')).find((b) => b.textContent === ${JSON.stringify(name)})?.click();`);
const isVisible = (app) => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible());

// Main-process capture, as the old in-app hook did: Playwright's page.screenshot hangs on a hidden
// window under Linux xvfb (CI app-packaged, 2026-10-11), capturePage does not.
async function capture(app, path) {
  const bytes = Buffer.from(await app.evaluate(async ({ BrowserWindow }) =>
    Array.from((await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG())));
  if (bytes.length < 8 || !bytes.subarray(0, 8).equals(PNG)) throw new Error("screenshot is not a PNG: " + path);
  writeFileSync(path, bytes);
  return bytes;
}

async function checkpoint(app, base, name) {
  if (!base) return undefined;
  const path = base.replace(/\.png$/, "") + "-" + name + ".png";
  await capture(app, path);
  return path;
}

/** The sidecar is absent: the app must show an actionable failure banner, not a crashed frame. */
export async function driveSidecarFailure({ app, page, screenshotBase }) {
  const failure = await page.evaluate(`new Promise((resolve, reject) => {
    const startedAt = Date.now();
    (function poll() {
      const banner = document.querySelector('.failure-banner');
      if (banner) return resolve({ title: banner.querySelector('.kit-notice > span > span')?.textContent?.trim() ?? '',
        reason: banner.querySelector('.failure-reason')?.textContent?.trim() ?? '',
        restart: Boolean(banner.querySelector('button')), frameCount: document.querySelectorAll('.shell').length });
      if (Date.now() - startedAt > 12000) return reject(new Error('missing-sidecar failure banner did not appear'));
      setTimeout(poll, 50);
    })();
  })`);
  if (failure.title !== "the lockwire engine stopped" || !failure.reason || !failure.restart || failure.frameCount !== 1) {
    throw new Error(`missing-sidecar banner was not actionable or renderer frame crashed: ${JSON.stringify(failure)}`);
  }
  return { sidecarFailure: true, failure, screenshot: await checkpoint(app, screenshotBase, "missing-sidecar"), visible: await isVisible(app) };
}

const clickButton = (page, text) => page.getByRole("button", { name: text, exact: true }).click({ timeout: 10_000 });
const waitDialogCall = async (app) => {
  for (let i = 0; i < 400; i += 1) {
    if (await app.evaluate(() => globalThis.__lwDialogCalls) > 0) return;
    await sleep(25);
  }
  throw new Error("Open robot never asked for a file");
};
const waitSelector = (page, selector, state = "attached") => page.waitForSelector(selector, { state, timeout: 15_000 });
const noPrompt = (page) => page.evaluate(`!document.querySelector('[role="alertdialog"]')`);

/**
 * "Open robot" on a model with a lockfile already beside it. Proven from outside: the file dialog,
 * the check on screen with no prompt, the rendered error, and the bytes on disk (the caller
 * compares them). Scenarios: cancel, reopen (lockfile untouched), overwrite and stale (both via
 * the Check page's "Overwrite from model"; stale proves the refusal text plus surviving bytes).
 */
export async function driveUrdf({ app, page, scenario, urdfPath, externalWrite, screenshotBase }) {
  await stubDialogs(app, { robot: scenario === "cancel" ? undefined : urdfPath });
  await waitForSidecar(page);
  await clickButton(page, "Open robot");
  await waitDialogCall(app);
  if (scenario === "cancel") {
    await sleep(500); // the cancelled IPC call resolves asynchronously; give the renderer time to (not) react
    const state = await page.evaluate(`(() => ({
      openFace: Boolean(document.querySelector('.open-face')), check: Boolean(document.querySelector('.check-headline')),
      error: Boolean(document.querySelector('.open-face-error')) }))()`);
    if (!state.openFace || state.check || state.error) throw new Error("URDF cancel changed the first-launch screen: " + JSON.stringify(state));
    return { urdfCancel: true, ...state, screenshot: await checkpoint(app, screenshotBase, "cancel"), visible: await isVisible(app) };
  }
  await waitSelector(page, ".check-headline");
  if (!await noPrompt(page)) throw new Error("re-opening a model with a lockfile beside it asked a question");
  if (scenario === "reopen") {
    return { urdfScenario: scenario, noPrompt: true, screenshot: await checkpoint(app, screenshotBase, "reopen"), visible: await isVisible(app) };
  }
  if (scenario === "stale") externalWrite();
  await clickButton(page, "Overwrite from model");
  if (scenario === "overwrite") {
    await waitSelector(page, ".overwrite-from-model", "detached");
    return { urdfScenario: scenario, screenshot: await checkpoint(app, screenshotBase, "overwrite"), visible: await isVisible(app) };
  }
  const renderedError = await page.evaluate(`new Promise((resolve, reject) => {
    const until = Date.now() + 10000;
    (function poll() {
      const error = document.querySelector('.open-face-error')?.textContent?.trim() ?? '';
      if (error.includes('changed since')) return resolve(error);
      if (Date.now() > until) return reject(new Error('stale overwrite refusal was not rendered: ' + error + '; page text: ' + document.body.innerText.slice(0, 6000)));
      setTimeout(poll, 25);
    })();
  })`);
  return { urdfScenario: scenario, renderedError, screenshot: await checkpoint(app, screenshotBase, "stale"), visible: await isVisible(app) };
}

/**
 * The aha distance: a model with NO lockfile, one "Open robot" click (plus the dialog pick), then
 * no further input until the first finding card. Reports clicks, ms from click to first card and
 * that card's text; `expectText` must appear on screen once the first card is opened (one more click).
 */
export async function driveFresh({ app, page, urdfPath, expectText }) {
  await stubDialogs(app, { robot: urdfPath });
  await waitForSidecar(page);
  const t0 = Date.now();
  await clickButton(page, "Open robot");
  await waitSelector(page, ".check-headline");
  await waitSelector(page, ".finding-card");
  const ms = Date.now() - t0;
  const cardTop = await page.evaluate(`document.querySelector('.finding-card').getBoundingClientRect().top`);
  const viewH = await page.evaluate(`window.innerHeight`);
  if (!(cardTop >= 0 && cardTop < viewH)) throw new Error(`first finding card is off screen (top ${cardTop}, window ${viewH})`);
  // Rows show a title only; the reason is in the row's detail, so open the first row as a user would and
  // read what is on screen (innerText: the collapsed raw finding does not count).
  if (expectText) {
    await page.locator(".finding-card .kit-finding").first().click({ timeout: 8000 });
    await paint(page);
  }
  const cards = await page.evaluate(`Array.from(document.querySelectorAll('.finding-card')).map((c) => c.innerText.trim())`);
  if (expectText && !cards.some((c) => c.includes(expectText))) throw new Error(`no finding card mentions "${expectText}": ${JSON.stringify(cards.slice(0, 6))}`);
  return { clicks: expectText ? 2 : 1, dialogPicks: await app.evaluate(() => globalThis.__lwDialogCalls), msToFirstCard: ms, firstCard: cards[0],
    headline: await page.evaluate(`document.querySelector('.check-headline').textContent.trim()`), visible: await isVisible(app) };
}

/**
 * Open a lockfile through the real dialog -> IPC -> sidecar path, then tour every mode.
 * opts: openPath, planPath, planStepPath, recordingPath, run (bool; reuses recordingPath),
 * sceneless, screenshotBase, analysis {procedure, evidence, manifest}, actuator {...}.
 */
export async function driveOpen({ app, page, openPath, planPath, planStepPath, recordingPath, run = false, sceneless = false,
  screenshotBase, analysis, actuator }) {
  const analysisE2e = Boolean(analysis);
  const emptyDesignExpected = Boolean(actuator) || sceneless;
  await stubDialogs(app, { robot: openPath, plan: planPath, recording: recordingPath });
  await waitForSidecar(page);
  const screenshots = [];
  const shot = async (name) => { const path = await checkpoint(app, screenshotBase, name); if (path) screenshots.push({ checkpoint: name, path }); return path; };
  await page.evaluate(`Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "Open robot")?.click();`);
  const result = await page.evaluate(`new Promise((resolve, reject) => {
    const startedAt = Date.now();
    (function poll() {
      const el = document.querySelector(".check-headline");
      if (el) {
        const verdictClass = Array.from(el.classList).find((c) => c.startsWith("verdict-"));
        return resolve({ headlineText: el.textContent, verdictClass });
      }
      if (Date.now() - startedAt > 30000) return reject(new Error("timed out waiting for .check-headline"));
      setTimeout(poll, 50);
    })();
  })`);
  await shot("Check-result");
  await clickTab(page, "Design");
  let designPixel;
  if (analysisE2e) {
    designPixel = null;
  } else if (emptyDesignExpected) {
    const emptyDesign = await page.evaluate(`(() => ({
      activeMode: document.querySelector('.shell-tabs [role="tab"][aria-selected="true"]')?.textContent?.trim() ?? "",
      emptyText: document.querySelector(".design-face-empty")?.textContent?.trim() ?? "",
      hasPartRows: Boolean(document.querySelector(".kit-tree-row")),
    }))()`);
    // No mesh: either the named empty state, or (a robot with joints) the joint list over the skeleton.
    if (emptyDesign.activeMode !== "Design" || (emptyDesign.emptyText !== "no Design face yet" && !emptyDesign.hasPartRows)) {
      throw new Error(`fixture without a same-stem scene did not show the expected empty Design state: ${JSON.stringify(emptyDesign)}`);
    }
    designPixel = null;
  } else {
    // Canvas's mount effect and SceneCapture's effect each commit one render pass after the one
    // that first renders <Canvas>, so poll rather than count frames.
    designPixel = await page.evaluate(`new Promise((resolve, reject) => {
      const startedAt = Date.now();
      (function poll() {
        try {
          resolve(window.__lockwireReadNonBackgroundPixel());
        } catch (err) {
          if (Date.now() - startedAt > 8000) {
            const activeMode = document.querySelector('.shell-tabs [role="tab"][aria-selected="true"]')?.textContent?.trim() ?? "";
            const designFace = Boolean(document.querySelector(".design-face"));
            const designEmpty = document.querySelector(".design-face-empty")?.textContent?.trim() ?? "";
            const canvas = Boolean(document.querySelector(".shell-scene canvas"));
            return reject(new Error(
              \`timed out waiting for Design pixel probe (\${String(err)}; activeMode=\${activeMode}; designFace=\${designFace}; designEmpty=\${JSON.stringify(designEmpty)}; canvas=\${canvas})\`,
            ));
          }
          setTimeout(poll, 50);
        }
      })();
    })`);
  }

  let planVerdict;
  if (planPath) {
    await clickTab(page, "Plan");
    await clickButton(page, "Choose plan");
    planVerdict = await page.evaluate(`new Promise((resolve, reject) => {
      const startedAt = Date.now();
      (function poll() {
        const el = document.querySelector(".verdict-word");
        if (!el) {
          if (Date.now() - startedAt > 8000) return reject(new Error("timed out waiting for the Plan verdict"));
          return setTimeout(poll, 50);
        }
        // Rows show plain labels; the raw path is the step row's title and the open finding's Path.
        // A step a finding already names has no step row of its own (R5-2): the finding row carries its
        // result word ("Fails" = FAIL) and, once opened, its Path and numbers.
        const findings = Array.from(document.querySelectorAll('.plan-finding')).map((finding) => ({
          title: finding.querySelector('.kit-finding-title')?.textContent?.trim() ?? '',
          word: finding.querySelector('.kit-finding-word')?.textContent?.trim() ?? '',
          quantity: finding.querySelector('.finding-path')?.textContent?.trim() ?? '',
          text: finding.textContent?.trim() ?? '',
          row: finding.querySelector('.kit-finding'),
        }));
        const steps = Array.from(document.querySelectorAll('.plan-outcome-row')).map((step) => ({
          path: step.getAttribute('title') ?? '',
          label: step.querySelector('.plan-outcome-path')?.textContent?.trim() ?? '',
          status: step.getAttribute('data-status') ?? '',
          reason: step.querySelector('.plan-outcome-reason')?.textContent?.trim() ?? '',
        }));
        const expected = ${JSON.stringify(planStepPath ?? "")};
        if (expected) {
          const step = steps.find((item) => item.path === expected);
          const finding = findings.find((item) => item.quantity === expected);
          // The detail opens inline once a row is selected: open each finding in turn until one shows the path.
          if (!finding) {
            const next = window.__lockwirePlanTry = (window.__lockwirePlanTry ?? -1) + 1;
            if (next < findings.length) {
              findings[next].row?.click();
              return setTimeout(poll, 50);
            }
          }
          const failed = step ? step.status === 'FAIL' : finding?.word === 'Fails';
          if (!finding || !failed || !finding.text.includes('501.00 W') || !finding.text.includes('20.00 W')) {
            return reject(new Error("Plan result did not render the fixture's cited rail-load finding and failed step: "
              + JSON.stringify({ findings: findings.map(({ row, ...f }) => f), steps })));
          }
          return resolve({ verdict: el.textContent?.trim() ?? '', finding: { quantity: finding.quantity, word: finding.word, text: finding.text }, step });
        }
        return resolve({ verdict: el.textContent?.trim() ?? '', findings: findings.map(({ row, ...f }) => f), steps });
      })();
    })`);
    await shot("Plan-result");
  }

  // The free app has no Test or Run tab (they come from the optional pro/ folder): visit only the tabs present.
  const present = await page.evaluate(`[...document.querySelectorAll('.shell-tabs [role="tab"]')].map((t) => t.textContent?.trim() ?? "")`);
  let testVerdict;
  if (recordingPath && present.includes("Test")) {
    await clickTab(page, "Test");
    await clickButton(page, "Choose recording");
    await clickButton(page, "Score recording");
    testVerdict = await page.locator(".verdict-word").first().textContent({ timeout: 8000 });
    await shot("Test-result");
  }

  let runProof;
  if (run && present.includes("Run")) {
    await clickTab(page, "Run");
    await paint(page);
    await page.locator(".run-choose-recording button").click({ timeout: 8000 });
    await page.waitForSelector(".run-start:not([disabled])", { timeout: 8000 });
    await page.locator(".run-start").click();
    runProof = await page.evaluate(`(async () => {
      const startedAt = Date.now();
      while (!document.querySelector('.kit-jobbar')?.textContent?.includes('Done')) {
        if (document.querySelector('.kit-jobbar')?.textContent?.includes('Failed')) throw new Error('synthetic Run failed');
        if (Date.now() - startedAt > 60000) throw new Error('timed out waiting for completed synthetic Run');
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
      const frames = document.querySelectorAll('.shell').length;
      const runFace = Boolean(document.querySelector('.run-face'));
      if (frames !== 1 || !runFace) throw new Error('completed Run did not remain inside exactly one app frame');
      return { status: 'completed', frames, runFace };
    })()`);
    await shot("Run-result");
  }

  const canonical = ["Design", "Declare", "Check", "Plan", "Test", "Run", "Docs"].filter((n) => present.includes(n));
  const names = present;
  if (JSON.stringify(names) !== JSON.stringify(canonical)) throw new Error(`unexpected tab set: ${JSON.stringify(present)}`);
  await clickTab(page, "Design"); // arrows move from the selected pill
  await page.evaluate(`document.querySelector('.shell-tabs [role="tab"]')?.focus()`);
  const tabOrder = [await page.evaluate(`document.activeElement?.textContent?.trim() ?? ""`)];
  for (let i = 1; i < names.length; i += 1) {
    await page.keyboard.press("ArrowRight"); // roving tabindex: arrows move between pill tabs
    await paint(page);
    const focused = await page.evaluate(`document.activeElement?.textContent?.trim() ?? ""`);
    if (focused !== names[i]) throw new Error(`keyboard tab order mismatch at ${i}: ${focused}`);
    tabOrder.push(focused);
  }
  const modeStates = [];
  for (const mode of names) {
    await clickTab(page, mode);
    await paint(page);
    const state = await page.evaluate(`(() => {
      const targets = { Design: ${JSON.stringify(analysisE2e || emptyDesignExpected ? ".design-face-empty, .design-face, .kit-tree" : ".kit-tree")}, Declare: '.declare-face input, .declare-face select', Check: '.check-headline', Plan: ${JSON.stringify(planPath ? ".verdict-word" : ".plan-input")}, Test: ${JSON.stringify(recordingPath ? ".verdict-word" : ".test-input")}, Run: ${JSON.stringify(run ? ".run-face" : ".run-panel")}, Docs: '.reference-face' };
      const activeMode = document.querySelector('.shell-tabs [role="tab"][aria-selected="true"]')?.textContent?.trim() ?? '';
      const targetSelector = targets[${JSON.stringify(mode)}];
      return { activeMode, targetSelector, targetPresent: Boolean(document.querySelector(targetSelector)) };
    })()`);
    if (state.activeMode !== mode || !state.targetPresent) throw new Error(`mode mismatch: expected ${mode}; state=${JSON.stringify(state)}`);
    modeStates.push({ mode, targetSelector: state.targetSelector, targetPresent: state.targetPresent });
    await shot(mode);
  }
  const referenceProof = await page.evaluate(`(() => {
    const face = document.querySelector('.reference-face');
    return { title: face ? 'Docs' : '', // the Docs panel has no separate title: its segment is the name
      material: face?.querySelectorAll('.reference-device, .reference-drawer').length ?? 0 };
  })()`);
  if (!referenceProof.title || referenceProof.material === 0) throw new Error("Reference did not render robot-specific material");
  await clickTab(page, "Declare");
  await paint(page);
  const declareProof = await page.evaluate(`(() => ({
    controls: document.querySelectorAll('.declare-face input, .declare-face select').length,
    submit: Boolean(document.querySelector('.declare-face button')),
  }))()`);
  if (declareProof.controls === 0 || !declareProof.submit) throw new Error("Declare did not render an editable workflow for this robot");

  let design;
  if (analysisE2e) {
    await clickTab(page, "Design");
    await paint(page);
    design = await page.evaluate(`(() => ({
      selectedPart: null, canvas: null,
      hasCanvas: Boolean(document.querySelector('.shell-scene canvas')),
      emptyText: document.querySelector('button[title="Analysis"]') ? 'Structural analysis' : '',
    }))()`);
    if (design.emptyText !== "Structural analysis") {
      throw new Error(`B31 fixture did not expose the existing structural analysis controls: ${JSON.stringify(design)}`);
    }
  } else if (emptyDesignExpected) {
    await clickTab(page, "Design");
    await paint(page);
    design = await page.evaluate(`(() => ({
      selectedPart: document.querySelector('.kit-tree-row[aria-pressed="true"]')?.getAttribute('data-part-id') ?? null,
      canvas: null,
      hasPartRows: Boolean(document.querySelector('.kit-tree-row')),
      emptyText: document.querySelector('.design-face-empty')?.textContent?.trim() ?? '',
    }))()`);
    if (design.selectedPart || (design.emptyText !== "no Design face yet" && !design.hasPartRows)) {
      throw new Error(`fixture without a same-stem scene changed its expected empty Design state: ${JSON.stringify(design)}`);
    }
  } else {
    await clickTab(page, "Design");
    // Parts are grouped drop-downs, closed unless they hold a finding: open them all, then pick a part row.
    for (let n = 0; n < 40 && (await page.locator('.kit-tree-chev[aria-expanded="false"]').count()) > 0; n++) await page.locator('.kit-tree-chev[aria-expanded="false"]').first().click({ timeout: 8000 });
    await page.locator("button.kit-tree-row").first().click({ timeout: 8000 });
    await paint(page);
    design = await page.evaluate(`(() => {
      const canvas = document.querySelector('.shell-scene canvas');
      const rect = canvas?.getBoundingClientRect();
      const style = canvas ? getComputedStyle(canvas) : undefined;
      return { selectedPart: document.querySelector('.kit-tree-row[aria-pressed="true"]')?.getAttribute('data-part-id') ?? null,
        assetIdentity: document.querySelector('.scene-asset-identity')?.getAttribute('title') ?? '',
        canvas: rect && style ? { width: rect.width, height: rect.height, display: style.display, visibility: style.visibility } : null };
    })()`);
    if (!design.selectedPart || !design.canvas || design.canvas.width <= 0 || design.canvas.height <= 0 || design.canvas.display === "none" || design.canvas.visibility !== "visible") {
      throw new Error("Design selection or canvas bounds missing");
    }
  }
  const themes = [];
  for (const theme of ["dark", "light"]) {
    const state = await page.evaluate(`(() => { if (document.documentElement.getAttribute('data-theme') !== ${JSON.stringify(theme)}) document.querySelector('.theme-toggle')?.click(); const style = getComputedStyle(document.body); return { theme: document.documentElement.getAttribute('data-theme'), background: style.backgroundColor, color: style.color }; })()`);
    if (state.theme !== theme || !state.background || !state.color) throw new Error(`theme mismatch: expected ${theme}`);
    themes.push({ theme, background: state.background, color: state.color });
  }

  const structuralAnalysis = analysisE2e ? await driveAnalysis(page, analysis) : undefined;
  const actuatorProof = actuator ? await driveActuator({ app, page, actuator, planVerdict, planStepPath, screenshotBase }) : undefined;
  return { ...result, screenshots, designPixel, planVerdict, testVerdict, runProof, referenceProof, declareProof, tabOrder,
    modeStates, design, themes, actuatorProof, structuralAnalysis, visible: await isVisible(app) };
}

function driveAnalysis(page, analysis) {
  return page.evaluate(`(async () => {
    // The panel opens from the tool pill's Analysis button.
    if (!document.querySelector('.analysis-start')) document.querySelector('button[title="Analysis"]')?.click();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const fields = [
      ['Analysis procedure ID', ${JSON.stringify(analysis.procedure)}],
      ['Analysis evidence root', ${JSON.stringify(analysis.evidence)}],
      ['Analysis package manifest', ${JSON.stringify(analysis.manifest)}],
    ];
    for (const [label, value] of fields) {
      const input = document.querySelector('input[aria-label="' + label + '"]');
      if (!input || !value) throw new Error('missing B31 analysis input ' + label);
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      setter?.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const start = document.querySelector('.analysis-start');
    if (!start || start.disabled) throw new Error('B31 analysis start is unavailable');
    start.click();
    return await new Promise((resolve, reject) => {
      const startedAt = Date.now();
      (async function poll() {
        const status = document.querySelector('.analysis-status')?.textContent?.trim() ?? '';
        if (status === 'completed') {
          const result = document.querySelector('.analysis-result');
          if (!result) return reject(new Error('completed B31 result was not rendered'));
          let polled;
          try {
            polled = await window.lockwire.pollAnalysis();
          } catch (error) {
            return reject(error);
          }
          if (polled.status !== 'completed' || !polled.document) return reject(new Error('public analysis-poll did not return the completed Job document'));
          const rows = Object.fromEntries(Array.from(result.querySelectorAll('.kit-kv-row')).map((r) => [r.querySelector('dt')?.textContent?.trim() ?? '', r.querySelector('dd')?.textContent?.trim() ?? '']));
          return resolve({ status: 'Analysis job: ' + status, contractRevision: rows['Revision'] ?? '',
            packageManifestSha256: rows['Package SHA-256'] ?? '',
            solverBinarySha256: rows['Solver SHA-256'] ?? '',
            outputSha256: rows['Output SHA-256'] ?? '',
            physicalConformance: 'Physical conformance: ' + (rows['Physical conformance'] ?? ''), simulationValidity: 'Simulation validity: ' + (rows['Simulation validity'] ?? ''),
            visibleText: result.textContent ?? '', parsedResult: result.querySelector('pre')?.textContent ?? '',
            pollContractRevision: polled.document.contract_revision,
            pollPackageManifestSha256: polled.document.package_manifest_sha256,
            pollSolverBinarySha256: polled.document.solver.binary_sha256,
            pollOutputSha256: polled.document.output_sha256,
            pollSimulationValidity: polled.document.simulation_validity,
            pollPhysicalConformance: polled.document.physical_conformance,
            pollContainsOutputText: Object.prototype.hasOwnProperty.call(polled.document, 'output_text'),
            pollOutput: polled.output, pollErrors: polled.errors });
        }
        if (status === 'failed' || status === 'cancelled') {
          return reject(new Error('B31 analysis ended without a completed result: ' + (document.querySelector('.kit-notice-error')?.textContent ?? status)));
        }
        if (Date.now() - startedAt > 30000) return reject(new Error('timed out waiting for completed B31 analysis'));
        // Polling is automatic while the job runs.
        setTimeout(poll, 250);
      })();
    });
  })()`);
}

async function driveActuator({ app, page, actuator, planVerdict, planStepPath, screenshotBase }) {
  const setSize = (w, h) => app.evaluate(({ BrowserWindow }, [width, height]) => BrowserWindow.getAllWindows()[0].setSize(width, height), [w, h]);
  const { caseId: expectedCase, revision: expectedRevision, evidencePath: expectedEvidencePath, evidenceHash: expectedEvidenceHash,
    gaps: expectedGaps, initialGaps: expectedInitialGaps, evidenceRoot, captureHash, predictionHash, screenshotPath } = actuator;
  await setSize(760, 560);
  await paint(page);
  await clickTab(page, "Check");
  const beforeDraft = await page.evaluate(`new Promise((resolve, reject) => {
    const startedAt = Date.now();
    (function poll() {
      const card = document.querySelector('.actuator-case');
      // Kit Check: the exact-finding disclosure lives in the open row, the case under "More".
      const row = document.querySelector('.check-list .kit-finding');
      if (row && !document.querySelector('.check-detail')) row.click();
      const more = card?.closest('details');
      if (more && !more.open) more.querySelector(':scope > summary')?.click();
      const disclosure = document.querySelector('.check-detail .kit-disclosure > summary');
      if (card && disclosure) {
        resolve({ caseId: card.querySelector('.actuator-case-id')?.textContent?.trim() ?? '',
          evidence: Array.from(card.querySelectorAll('.actuator-case-evidence code')).map((el) => el.textContent?.trim() ?? ''),
          gaps: Array.from(card.querySelectorAll('.actuator-claim-gaps code')).map((el) => el.textContent?.trim() ?? '') });
        return;
      }
      if (Date.now() - startedAt > 8000) return reject(new Error('timed out waiting for packaged actuator evidence and native disclosure'));
      setTimeout(poll, 50);
    })();
  })`);
  if (beforeDraft.caseId !== expectedCase) throw new Error(`actuator case mismatch: ${beforeDraft.caseId}`);
  if (beforeDraft.evidence.length !== 0) throw new Error("unadmitted actuator evidence appeared before the engineer selected it");
  for (const gap of expectedInitialGaps) if (!beforeDraft.gaps.includes(gap)) throw new Error(`initial actuator evidence gap missing: ${gap}`);
  const disclosure = await page.evaluate(`(() => {
    const summary = document.querySelector('.check-detail .kit-disclosure > summary');
    const details = summary?.closest('details');
    summary?.focus();
    return { text: summary?.textContent?.trim() ?? '', open: details?.open ?? false,
      accessible: summary instanceof HTMLElement && summary.tabIndex >= 0 && summary.getAttribute('role') !== 'presentation' };
  })()`);
  if (disclosure.text !== "Show the exact finding" || !disclosure.accessible) throw new Error("native finding disclosure is not keyboard-accessible");
  await page.keyboard.press("Space");
  await paint(page);
  if (!(await page.evaluate("document.querySelector('.check-detail .kit-disclosure')?.open === true"))) throw new Error("native finding disclosure did not open with Space");
  await page.keyboard.press("Space");
  await paint(page);
  if (!(await page.evaluate("document.querySelector('.check-detail .kit-disclosure')?.open === false"))) throw new Error("native finding disclosure did not close with Space");
  const draftState = await page.evaluate(`new Promise((resolve, reject) => {
    const set = (labelText, value) => {
      const label = Array.from(document.querySelectorAll('.actuator-review-draft label')).find((item) => item.textContent.trim().startsWith(labelText));
      const control = label?.querySelector('input,textarea,select');
      if (!control) throw new Error('missing actuator draft control: ' + labelText);
      const proto = Object.getPrototypeOf(control);
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(control, value); else control.value = value;
      control.dispatchEvent(new Event('input', { bubbles: true }));
      control.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const values = [
      ['Declared actuator case', ${JSON.stringify(expectedCase)}],
      ['Evidence root', ${JSON.stringify(evidenceRoot)}],
      ['Artifact relative path', ${JSON.stringify(expectedEvidencePath)}],
      ['Artifact SHA-256', ${JSON.stringify(expectedEvidenceHash)}],
      ['Provenance', 'packaged actuator evidence fixture'],
      ['Discrepancy', 'simulation residual exceeds the declared tolerance'],
      ['Alternatives (one per line)', 'repeat the simulation\\nreview the model'],
      ['Proposed target', 'model'],
      ['Proposed patch', 'review the model calibration'],
      ['Rationale', 'The hash-bound evidence leaves simulation validity unresolved.'],
      ['Correction category', 'model_calibration'],
      ['Required rechecks (one per line)', 'rerun actuator simulation\\nreview physical conformance remains separate'],
    ];
    for (const [label, value] of values) set(label, value);
    document.querySelector('.actuator-review-draft button[type="submit"]')?.click();
    const startedAt = Date.now();
    (function poll() {
      const article = document.querySelector('.actuator-review-draft-result');
      if (article && document.querySelectorAll('.actuator-claim-draft-binding').length === 2) {
        for (const detail of document.querySelectorAll('.actuator-claim-dependencies')) detail.open = true;
        return resolve({
          revision: article.querySelector('p code')?.textContent?.trim() ?? '',
          evidence: Array.from(document.querySelectorAll('.actuator-case-evidence code')).map((el) => el.textContent?.trim() ?? ''),
          gaps: Array.from(document.querySelectorAll('.actuator-claim-gaps code')).map((el) => el.textContent?.trim() ?? ''),
          claims: Array.from(document.querySelectorAll('.actuator-claim')).map((el) => el.innerText ?? ''),
        });
      }
      const alert = document.querySelector('.actuator-review-draft [role="alert"]');
      if (alert) return reject(new Error('actuator review draft refused: ' + alert.textContent));
      if (Date.now() - startedAt > 15000) return reject(new Error('timed out waiting for read-only actuator review draft'));
      setTimeout(poll, 50);
    })();
  })`);
  if (draftState.revision !== expectedRevision) throw new Error(`draft base revision mismatch: ${draftState.revision}`);
  if (!draftState.evidence.includes(expectedEvidencePath) || !draftState.evidence.includes(expectedEvidenceHash)) throw new Error("prepared draft lost its evidence identity");
  for (const gap of expectedGaps) if (!draftState.gaps.includes(gap)) throw new Error(`prepared evidence gap missing: ${gap}`);
  if (!draftState.claims.some((claim) => claim.includes("physical conformance") && claim.includes("GO"))) throw new Error("physical GO claim was not preserved");
  if (!draftState.claims.some((claim) => claim.includes("simulation validity") && claim.includes("NO-GO"))) throw new Error("simulation NO-GO claim was not preserved");
  const physicalClaim = draftState.claims.find((claim) => claim.includes("physical conformance")) ?? "";
  const simulationClaim = draftState.claims.find((claim) => claim.includes("simulation validity")) ?? "";
  for (const [claim, expected] of [
    [physicalClaim, ["Evidence class: physical measurement", "Measured signal source measurement-log/run-1; witness bench operator",
      "evidence/capture.json", captureHash, `Draft binding: ${expectedCase} / conformance`]],
    [simulationClaim, ["Evidence class: prediction with held-out reference", "prediction/run.json",
      predictionHash, `Draft binding: ${expectedCase} / simulation_validity`]],
  ]) {
    if (!claim.includes("Claim uncertainty: unknown") || !claim.includes("Freshness: bound to raw revision") || !claim.includes(expectedRevision) ||
        expected.some((item) => !item || !claim.includes(item))) {
      throw new Error("packaged claim context lost its rendered evidence class, uncertainty, source, dependency, freshness, or draft binding");
    }
  }
  const renderedDraft = await page.evaluate(`(() => {
    const article = document.querySelector('.actuator-review-draft-result');
    if (!article) throw new Error('rendered actuator review draft is absent');
    article.scrollIntoView({ block: 'nearest' });
    const frame = document.querySelector('.actuator-review-draft-result')?.closest('.kit-panel-body');
    const frameRect = frame?.getBoundingClientRect();
    const draftRect = article.getBoundingClientRect();
    const evidence = Array.from(article.querySelectorAll('li code')).map((item) => item.textContent?.trim() ?? '');
    const alternatives = Array.from(article.querySelectorAll('ul')).find((list) => {
      const items = Array.from(list.querySelectorAll('li')).map((item) => item.textContent?.trim() ?? '');
      return items.includes('repeat the simulation') && items.includes('review the model');
    })?.querySelectorAll('li') ?? [];
    const hashNode = Array.from(article.querySelectorAll('code')).find((item) => item.textContent?.trim() === ${JSON.stringify(expectedEvidenceHash)});
    const hashRange = document.createRange();
    if (hashNode) hashRange.selectNodeContents(hashNode);
    const hashRect = hashNode?.getBoundingClientRect();
    const category = Array.from(article.querySelectorAll('p')).find((item) => item.textContent?.startsWith('Correction category:'))?.textContent?.trim() ?? '';
    return { text: article.textContent ?? '', evidence,
      alternatives: Array.from(alternatives).map((item) => item.textContent?.trim() ?? ''), category,
      geometry: { viewport: { width: innerWidth, height: innerHeight },
        container: { width: frameRect?.width ?? 0, clientWidth: frame?.clientWidth ?? 0, clientHeight: frame?.clientHeight ?? 0, scrollWidth: frame?.scrollWidth ?? 0, verticalReachable: (() => { if (!(frame instanceof HTMLElement) || frame.clientWidth <= 0 || frame.clientHeight <= 0) return false; if (frame.scrollHeight <= frame.clientHeight + 1) return true; frame.scrollTop = frame.scrollHeight; return frame.scrollTop + frame.clientHeight >= frame.scrollHeight - 1; })() },
        draft: { left: draftRect.left, right: draftRect.right, top: draftRect.top, bottom: draftRect.bottom, width: draftRect.width, height: draftRect.height },
        hash: { left: hashRect?.left ?? 0, right: hashRect?.right ?? 0, top: hashRect?.top ?? 0, bottom: hashRect?.bottom ?? 0,
          fragments: hashRange.getClientRects().length } } };
  })()`);
  const viewportWidth = renderedDraft.geometry.viewport.width;
  const viewportHeight = renderedDraft.geometry.viewport.height;
  if (!renderedDraft.text.includes(expectedEvidenceHash) || !renderedDraft.evidence.includes(expectedEvidencePath) || !renderedDraft.evidence.includes(expectedEvidenceHash)) {
    throw new Error("rendered draft omitted its readable full evidence identity");
  }
  if (!renderedDraft.alternatives.includes("repeat the simulation") || !renderedDraft.alternatives.includes("review the model")) {
    throw new Error("rendered draft omitted its alternatives");
  }
  if (renderedDraft.category !== "Correction category: model_calibration") throw new Error("rendered draft omitted its correction category");
  if (viewportWidth > 760 || viewportHeight > 560 || viewportWidth < 500 || viewportHeight < 400 ||
      renderedDraft.geometry.container.width <= 0 || renderedDraft.geometry.container.clientWidth <= 0 || renderedDraft.geometry.container.clientHeight <= 0 ||
      renderedDraft.geometry.container.scrollWidth > renderedDraft.geometry.container.clientWidth + 1 ||
      renderedDraft.geometry.hash.left < 0 || renderedDraft.geometry.hash.right > viewportWidth ||
      (renderedDraft.geometry.draft.height > viewportHeight && !renderedDraft.geometry.container.verticalReachable)) {
    const offenders = await page.evaluate(`(() => {
      const frame = document.querySelector('.actuator-review-draft-result')?.closest('.kit-panel-body');
      if (!frame) return [];
      const right = frame.getBoundingClientRect().right;
      return Array.from(frame.querySelectorAll('*')).map((element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return { tag: element.tagName, className: element.getAttribute('class'), right: rect.right, width: rect.width,
          clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, display: style.display,
          text: element.textContent?.trim().slice(0, 80) };
      }).filter((item) => item.right > right + 1 || item.scrollWidth > item.clientWidth + 1)
        .sort((left, right) => right.right - left.right).slice(0, 12);
    })()`);
    throw new Error("draft has horizontal overflow at the bounded " + viewportWidth + "x" + viewportHeight + " viewport: " + JSON.stringify({ geometry: renderedDraft.geometry, offenders }));
  }
  await setSize(500, 560);
  await paint(page);
  const narrowDraft = await page.evaluate(
    "(() => { const article = document.querySelector('.actuator-review-draft-result'); const frame = document.querySelector('.actuator-review-draft-result')?.closest('.kit-panel-body'); const frameRect = frame?.getBoundingClientRect(); article?.scrollIntoView({ block: 'nearest' }); const hashNode = Array.from(article?.querySelectorAll('code') ?? []).find((item) => item.textContent?.trim() === " + JSON.stringify(expectedEvidenceHash) + "); const range = document.createRange(); if (hashNode) range.selectNodeContents(hashNode); return { viewport: { width: innerWidth, height: innerHeight }, container: { width: frameRect?.width ?? 0, clientWidth: frame?.clientWidth ?? 0, clientHeight: frame?.clientHeight ?? 0, scrollWidth: frame?.scrollWidth ?? 0 }, hash: { fragments: range.getClientRects().length }, text: article?.textContent ?? '' }; })()",
  );
  if (narrowDraft.viewport.width > 500 || narrowDraft.viewport.height > 560 || narrowDraft.viewport.width < 400 || narrowDraft.viewport.height < 400 ||
      narrowDraft.container.width <= 0 || narrowDraft.container.clientWidth <= 0 || narrowDraft.container.clientHeight <= 0 ||
      narrowDraft.container.scrollWidth > narrowDraft.container.clientWidth + 1 ||
      narrowDraft.hash.fragments < 2 || !narrowDraft.text.includes(expectedEvidencePath) || !narrowDraft.text.includes(expectedEvidenceHash)) {
    throw new Error("long evidence identity failed to wrap without horizontal overflow at the supported narrow " + narrowDraft.viewport.width + "x" + narrowDraft.viewport.height + " viewport: " + JSON.stringify(narrowDraft));
  }
  if (!screenshotPath) throw new Error("screenshotPath is required for actuator visual evidence");
  const png = await capture(app, screenshotPath);
  return { caseId: beforeDraft.caseId, evidencePath: expectedEvidencePath, evidenceSha256: expectedEvidenceHash,
    contractRevision: expectedRevision, simulationGaps: expectedGaps, disclosureKeyboardOperable: true,
    physicalGoPreserved: true, simulationNoGoPreserved: true, renderedAlternatives: renderedDraft.alternatives,
    claimContexts: { physical: physicalClaim, simulation: simulationClaim },
    renderedEvidence: renderedDraft.evidence, renderedCategory: renderedDraft.category, viewport: renderedDraft.geometry,
    screenshot: { path: screenshotPath, bytes: png.length },
    planFinding: planVerdict && planStepPath ? planVerdict.finding : undefined,
    draftReadOnly: true };
}

