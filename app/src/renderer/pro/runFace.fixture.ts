import type { RunFacePayload } from "./runFacePayload";

// Measured 2026-09-08 against a scored copy of `tests/fixtures/contract_clean.yaml`
// (`cp tests/fixtures/contract_clean.yaml /tmp/e3run_fixture/lockwire.yaml`, then
// `uv run lockwire preflight /tmp/e3run_fixture/lockwire.yaml --from tests/fixtures/bags/clean`
// to give the ledger real rows), read back with
// `uv run python -c "import json, lockwire.api as A; print(json.dumps(A.health_document('/tmp/e3run_fixture/lockwire.yaml')))"`.
// Real, measured output — not invented data. `interface_poll_s`, `path`, `origin`,
// `contract_sha`, `foreign_rows`, `chain` and `summary` are the document's other fields;
// this face does not read them yet, so they are omitted from `RunFacePayload`.
export const CLEAN_RUN_PAYLOAD: RunFacePayload = {
  verdict: "GO",
  exit_code: 0,
  not_watched: 2,
  scored: 3,
  nothing_watched: false,
  next_step: null,
  devices: [
    { id: "dev.barometer", kind: "device", state: "healthy", word: "inside its bands", run: "2026-09-09T00:20:45Z", reason: "inside its bands, 3 signals compared", runs: 1 },
    { id: "dev.esc.busless", kind: "device", state: "not_watched", word: "nothing compared yet", run: "2026-09-09T00:20:45Z", reason: "nothing declared to compare against", runs: 1 },
    { id: "dev.gnss", kind: "device", state: "healthy", word: "inside its bands", run: "2026-09-09T00:20:45Z", reason: "inside its bands, 2 signals compared", runs: 1 },
    { id: "dev.imu", kind: "device", state: "healthy", word: "inside its bands", run: "2026-09-09T00:20:45Z", reason: "inside its bands, 10 signals compared", runs: 1 },
    { id: "dev.radio.rfd900", kind: "device", state: "not_watched", word: "nothing compared yet", run: "2026-09-09T00:20:45Z", reason: "nothing declared to compare against", runs: 1 },
  ],
};

// Measured 2026-09-08 against `tests/fixtures/contract_empty.yaml` copied to an empty
// directory with no sidecar ledger yet.
export const DAY_ONE_RUN_PAYLOAD: RunFacePayload = {
  verdict: "NOT_SCORED",
  exit_code: 3,
  not_watched: 0,
  scored: 0,
  nothing_watched: true,
  next_step: "Nothing watched yet",
  next_action:
    "nothing is watched yet — declare a hard limit on a device model, or run `lockwire baseline <recording-dir> --context <context>` to author a baseline to compare against",
  devices: [],
};
