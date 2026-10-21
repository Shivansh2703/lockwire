#!/usr/bin/env node
// Stand-in for the Python sidecar's handshake (real one is a separate, not-yet-merged
// box). Driven entirely by env vars so sidecar.test.ts never needs a second script.
// FAKE_SIDECAR_MODE: ready (default) | malformed | silent | exit
// FAKE_SIDECAR_DELAY_MS: wait this long before acting (default 0)
// FAKE_SIDECAR_TOKEN: token value in the ready line (default "fake-token")
if (process.argv.includes("--exit-on-stdin-close")) {
  process.stdin.resume();
  process.stdin.on("end", () => process.exit(0));
}
const noiseKb = Number(process.env.FAKE_SIDECAR_STDERR_KB ?? 0);
// M31: a chatty child; its ready line waits until the OS pipe has taken all of it.
const go = () => setTimeout(() => {
  const mode = process.env.FAKE_SIDECAR_MODE ?? "ready";
  if (mode === "ready") {
    const token = process.env.FAKE_SIDECAR_TOKEN ?? "fake-token";
    const port = Number(process.env.FAKE_SIDECAR_PORT ?? 45678);
    console.log(JSON.stringify({ lockwire: "ready", version: "0.0.0-fake", port, token }));
  } else if (mode === "malformed") {
    console.log("not json");
  } else if (mode === "exit") {
    process.exit(1);
  }
  // "silent" prints nothing and just waits (for the parent's stdin to close, or the
  // test's own timers).
}, Number(process.env.FAKE_SIDECAR_DELAY_MS ?? 0));
if (noiseKb > 0) process.stderr.write("x".repeat(noiseKb * 1024), go);
else go();
