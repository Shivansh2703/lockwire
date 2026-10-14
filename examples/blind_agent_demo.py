#!/usr/bin/env python3
"""Blind agent vs. the gate.

An agent that has never seen the target contract (`CONTRACT`, below) reads
two things over MCP (`describe_system`, `describe_device` — nothing else)
and proposes a plan from what it read. This script never writes to a real or
simulated actuator — it only reads and calls `lockwire plan`. It never
rejects, clamps, or withholds anything itself; `lockwire plan` is the one
evaluator, exercised exactly as an agent would exercise it, over the CLI.

Three beats:
  1. the agent's first guess, sent with no gate at all — nothing here stops
     it; a real flight finds out afterward, from the log.
  2. the same guess, scored by `lockwire plan` before anything moves — NO-GO,
     with the number and the margin.
  3. the corrected plan, built from what the gate said — GO.

Self-check: this file greps ITSELF clean of the target's device identifiers,
units, and hard-limit numbers before it ever runs (`_self_grep_clean()`).
Known ceiling: a grep of this file's own text cannot see an identifier
smuggled in through an IMPORTED module or fixture — see
docs/blind-agent-demo.md, "what the self-grep does not prove."

Exit: 0 only if beat 2 is NO-GO *and* beat 3 is GO. Non-zero if either the
true contract's safe plan fails or the naive plan is wrongly passed.
"""

from __future__ import annotations

import asyncio
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The fixture directory this demo scores against —
# a path, not a device id/unit/number, so it sits outside the self-grep's
# ban list below; the blind agent still learns everything it acts on from
# `describe_system`/`describe_device`, never from reading this path's contents.
REPO = Path(__file__).resolve().parent.parent
_FIXTURE_DIR = REPO / "examples" / "px4_sitl"
CONTRACT = _FIXTURE_DIR / "px4_contract.yaml"
MANIFEST = _FIXTURE_DIR / "agent_manifest.txt"

# The agent's first guess before it has read anything: a round number with no
# relationship to any contract value — the naive default the blind agent
# tries first, which is the whole point of beat 1.
NAIVE_GUESS = 95.0

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip rich's ANSI styling so a captured CLI run reads and greps clean."""
    return _ANSI.sub("", text)


def _self_grep_clean() -> None:
    """Refuse to run if this file's own text names one of the target
    contract's device ids, its unit, or one of its hard-limit numbers.

    KNOWN CEILING (named, not hidden): this scans only this file's source
    text. An identifier imported from another module, or read from a fixture
    this file does not itself spell out, would not be seen here — see
    docs/blind-agent-demo.md.
    """
    import base64

    source = Path(__file__).read_text()
    # base64'd so the banned words themselves never appear as source text —
    # a plain string list here would trip its own grep. See
    # docs/blind-agent-demo.md for what each code decodes to.
    banned_b64 = [
        "c2Vuc29yX2NvbWJpbmVk",
        "YmF0dGVyeV9zdGF0dXM=",
        "eDUwMA==",
        "ODAuMA==",
        "LTgwLjA=",
        "bS9zXjI=",
    ]
    banned = [base64.b64decode(word).decode() for word in banned_b64]
    hits = [word for word in banned if word.lower() in source.lower()]
    if hits:
        raise SystemExit(f"self-grep failed: found banned pattern(s) in {__file__}")


def _read_manifest() -> str:
    """The device id to inspect, handed to the agent by an external
    manifest — the same way a fleet discovery service would name a target
    before the agent ever touches the contract. Not a Python constant in
    this file."""
    for line in MANIFEST.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise SystemExit(f"{MANIFEST} names no device")


async def _describe_over_mcp(contract: Path, device_id: str) -> tuple[str, str]:
    """The blind agent's ONLY two reads: describe_system, describe_device —
    both over MCP, both read-only, neither a file opened directly."""
    from mcp.client import Client

    from lockwire.mcp_server import build_server

    async with Client(build_server()) as client:
        system = await client.call_tool("describe_system", {"contract": str(contract)})
        device = await client.call_tool(
            "describe_device", {"contract": str(contract), "device_id": device_id}
        )
    return system.structured_content["markdown"], device.structured_content["markdown"]


def _parse_measured_signal(device_md: str) -> tuple[str, str, str]:
    """(topic, msg_type, dotted field) for the device's first measured
    signal, parsed out of describe_device's own Markdown table — never
    assumed. Skips the table's own header row (its first data row is a
    literal ``signal | field | unit`` line, not a device fact)."""
    topic = re.search(r"^- topic: (\S+)", device_md, re.MULTILINE)
    msg_type = re.search(r"^- msg_type: (\S+)", device_md, re.MULTILINE)
    rows = re.findall(r"^\| (\w[\w.]*) \| ([\w.]+) \| ([^\s|]+) \|$", device_md, re.MULTILINE)
    rows = [row for row in rows if row[0] != "signal"]
    if not (topic and msg_type and rows):
        raise SystemExit("describe_device named no measured signal to plan against")
    return topic.group(1), msg_type.group(1), rows[0][1]


def _parse_hard_limit(device_md: str, signal: str) -> tuple[float, float]:
    """(lo, hi) for `signal`'s hard limit, parsed out of the "Limits
    enforced" section's one declared-rating line (`Band.__str__` ->
    "[lo, hi] unit").  Refuse prose outside that section or ambiguous data."""
    sections = re.findall(
        r"^## Limits enforced[ \t]*\n(.*?)(?=^## |\Z)",
        device_md,
        re.MULTILINE | re.DOTALL,
    )
    if len(sections) != 1:
        raise SystemExit("describe_device named not exactly one Limits enforced section")
    pattern = (
        rf"^- declared datasheet/model rating {re.escape(signal)}: "
        r"\[(-?[\d.]+), (-?[\d.]+)\]"
    )
    matches = re.findall(pattern, sections[0], re.MULTILINE)
    if len(matches) != 1:
        raise SystemExit(f"describe_device named not exactly one hard limit for {signal!r}")
    lo, hi = map(float, matches[0])
    if not (math.isfinite(lo) and math.isfinite(hi) and lo <= hi):
        raise SystemExit(f"describe_device named invalid hard limit for {signal!r}")
    return lo, hi


def _write_plan(path: Path, topic: str, msg_type: str, field: str, value: float) -> None:
    """One `publish` step, same shape `lockwire plan` reads (plan.py's own
    loader — reused here via lockwire.schema._yaml, not a second YAML setup)."""
    from lockwire.schema import _yaml

    msg: dict = {}
    node = msg
    parts = field.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value

    document = {
        "steps": [{"kind": "publish", "topic": topic, "msg_type": msg_type, "msg": msg}]
    }
    with open(path, "w", encoding="utf-8") as handle:
        _yaml().dump(document, handle)


def _run_lockwire_plan(contract: Path, plan_path: Path) -> tuple[str, int]:
    """The door this demo scores through: the real `lockwire plan` CLI,
    exercised exactly as an agent would exercise it — never validate_plan()
    called in-process."""
    import os

    lockwire_bin = Path(sys.executable).with_name("lockwire")
    env = {**os.environ, "COLUMNS": "200"}
    result = subprocess.run(
        [str(lockwire_bin), "plan", str(contract), str(plan_path)],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
    )
    return _plain(result.stdout + result.stderr), result.returncode


def main(naive_value: float | None = None, corrected_value: float | None = None) -> int:
    """Run all three beats and return the demo's own exit code.

    ``naive_value``/``corrected_value`` default to the agent's real first
    guess and the true band's midpoint; a test overriding either is asking
    "what if beat 2 wrongly passed" / "what if beat 3's safe plan wrongly
    failed" — the two mutation bites this function itself guards against.
    """
    _self_grep_clean()

    device_id = _read_manifest()
    system_md, device_md = asyncio.run(_describe_over_mcp(CONTRACT, device_id))
    del system_md  # read, per the blind-agent contract, even though this
    # demo's one plan step needs nothing from it beyond proving the call happened

    topic, msg_type, field = _parse_measured_signal(device_md)
    # The signal name is the table row's first column (e.g. "accel_z"), the
    # hard-limit section keys on that name, not on the field path.
    signal_row = re.search(r"^\| (\w[\w.]*) \| " + re.escape(field), device_md, re.MULTILINE)
    if signal_row is None:
        raise SystemExit("describe_device's signal table did not match its own field path")
    signal_name = signal_row.group(1)
    lo, hi = _parse_hard_limit(device_md, signal_name)

    if naive_value is None:
        naive_value = NAIVE_GUESS
    if corrected_value is None:
        # The true band's midpoint, read a moment ago — inside the limit by
        # construction, never invented.
        corrected_value = (lo + hi) / 2

    print("=== beat 1: the agent's first guess, no gate ===")
    print(
        f"proposing to publish {naive_value} on {topic} field {field} — "
        "nothing here refuses it; sent as-is, a real flight would only learn "
        "the value was wrong afterward, from the log."
    )
    print()

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        naive_plan = Path(tmp) / "naive_plan.yaml"
        _write_plan(naive_plan, topic, msg_type, field, naive_value)

        print("=== beat 2: the same guess, through `lockwire plan` first ===")
        naive_out, naive_exit = _run_lockwire_plan(CONTRACT, naive_plan)
        print(naive_out)
        print(f"exit={naive_exit}")
        if naive_exit == 0:
            print("FAIL: the wrong plan was wrongly passed (expected NO-GO)")
            ok = False
        print()

        good_plan = Path(tmp) / "good_plan.yaml"
        _write_plan(good_plan, topic, msg_type, field, corrected_value)

        print("=== beat 3: the corrected plan, on the true contract ===")
        good_out, good_exit = _run_lockwire_plan(CONTRACT, good_plan)
        print(good_out)
        print(f"exit={good_exit}")
        if good_exit != 0:
            print("FAIL: the true contract's safe plan did not pass")
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
