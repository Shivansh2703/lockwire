"""The blind-agent demo (`examples/blind_agent_demo.py`).

Loaded as a module (same pattern as test_b117_fitted_tolerance.py's bag
generator) rather than only shelled out to, so tests can call its pieces
directly. The accept line itself (a full subprocess run) is also exercised,
once, so the wrapper never drifts from what a human actually sees.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import strip_ansi
from lockwire.describe import describe_device
from lockwire.schema import load_contract

REPO = Path(__file__).parent.parent
DEMO_PATH = REPO / "examples" / "blind_agent_demo.py"


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("blind_agent_demo", DEMO_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import demo at {DEMO_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_demo_script_greps_clean_of_px4_identifiers(demo):
    """The demo's own self-check passes against the file as committed.

    KNOWN CEILING (named in the module docstring and docs/blind-agent-demo.md,
    not hidden): this only proves the demo's own source text is clean — an
    identifier smuggled in through an imported module or fixture would not
    be caught by a grep scoped to this one file.
    """
    demo._self_grep_clean()  # raises SystemExit if unclean; a clean pass returns None


def test_demo_exits_nonzero_when_true_contract_fails(demo):
    # Force beat 3's "corrected" plan outside the true hard limit — the true
    # contract's safe plan now wrongly fails, and the demo must say so.
    assert demo.main(corrected_value=999.0) != 0


def test_demo_exits_nonzero_when_wrong_contract_passes(demo):
    # Force beat 2's plan to a value that happens to sit inside the true
    # band — the wrong plan is wrongly passed, and the demo must say so.
    assert demo.main(naive_value=0.0) != 0


def test_demo_only_calls_describe_tools_over_mcp(demo, monkeypatch):
    calls: list[str] = []
    from mcp.client import Client

    original_call_tool = Client.call_tool

    async def recording_call_tool(self, name, *args, **kwargs):
        calls.append(name)
        return await original_call_tool(self, name, *args, **kwargs)

    monkeypatch.setattr(Client, "call_tool", recording_call_tool)

    device_id = demo._read_manifest()
    asyncio.run(demo._describe_over_mcp(demo.CONTRACT, device_id))

    assert calls == ["describe_system", "describe_device"]


def test_demo_prints_three_beats_in_order(demo, capsys):
    exit_code = demo.main()
    assert exit_code == 0
    out = strip_ansi(capsys.readouterr().out)
    beat1 = out.index("beat 1")
    beat2 = out.index("beat 2")
    beat3 = out.index("beat 3")
    assert beat1 < beat2 < beat3
    assert "NO-GO" in out
    assert "GO" in out


def test_demo_parses_rendered_hard_limit_with_declared_enforcement(demo):
    """The demo consumes the actual describe shape, including `why`/`max_rate` metadata."""
    contract = load_contract(demo.CONTRACT)
    device = contract.device("px4.sensor_combined.0")
    assert device is not None
    model = contract.device_model(device.model)
    assert model is not None
    model.hard_limits["accel_z"] = model.hard_limits["accel_z"].model_copy(
        update={"enforced_by": "firmware"}
    )

    rendered = describe_device(contract, device)

    assert "declared enforcement: firmware" in rendered
    assert demo._parse_hard_limit(rendered, "accel_z") == (-80.0, 80.0)


def test_demo_parses_only_the_enforced_section(demo):
    document = """\
## Observed envelope

- declared datasheet/model rating accel_z: [-1, 1] m/s^2, untrusted prose

## Rated elsewhere

- declared datasheet/model rating accel_z: [-2, 2] m/s^2, not an enforced limit

## Limits enforced

- declared datasheet/model rating accel_z: [-80, 80] m/s^2, declared enforcement: firmware

## Provenance

untrusted corpus ends here
"""

    assert demo._parse_hard_limit(document, "accel_z") == (-80.0, 80.0)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            "- declared datasheet/model rating accel_z: [-1, 1] m/s^2\n",
            "not exactly one Limits enforced section",
        ),
        (
            "## Limits enforced\n\n"
            "- declared datasheet/model rating accel_z: [-1, 1] m/s^2\n\n"
            "## Limits enforced\n\n"
            "- declared datasheet/model rating accel_z: [-2, 2] m/s^2\n",
            "not exactly one Limits enforced section",
        ),
        (
            "## Limits enforced\n\n"
            "- declared datasheet/model rating accel_z: [-1, 1] m/s^2\n"
            "- declared datasheet/model rating accel_z: [-2, 2] m/s^2\n",
            "not exactly one hard limit",
        ),
    ],
)
def test_demo_refuses_missing_or_ambiguous_enforced_limits(demo, document, message):
    with pytest.raises(SystemExit, match=message):
        demo._parse_hard_limit(document, "accel_z")


@pytest.mark.parametrize(
    "bounds",
    ["1, -1", "nan, 1", "inf, 1", f"1, {'9' * 400}"],
    ids=["reversed", "nan", "infinite", "decimal-overflow"],
)
def test_demo_refuses_invalid_enforced_limit_bounds(demo, bounds):
    document = (
        "## Limits enforced\n\n"
        f"- declared datasheet/model rating accel_z: [{bounds}] m/s^2\n"
    )

    with pytest.raises(SystemExit):
        demo._parse_hard_limit(document, "accel_z")


def test_demo_never_commands_only_reads_and_plans():
    """Static, literal-text check on THIS FILE's source only: every quoted
    `call_tool(...)` name is one of the two describe tools, every literal-list
    `subprocess.run([...])` call contains `"plan"`, and a short forbidden-word
    list is absent. KNOWN CEILING (same as the self-grep's, deliberately not
    caught here): a command assembled at runtime — a non-literal tool name, a
    variable argv, an identifier read from an imported module or fixture — is
    invisible to this check. A static grep cannot see runtime-assembled calls;
    that is a scoping choice, not an oversight.
    """
    source = DEMO_PATH.read_text()

    tool_calls = set(re.findall(r'call_tool\(\s*"([^"]+)"', source))
    assert tool_calls <= {"describe_system", "describe_device"}

    subprocess_targets = re.findall(r"subprocess\.run\(\s*\[([^\]]+)\]", source, re.DOTALL)
    assert subprocess_targets, "expected the demo to shell out to `lockwire plan`"
    for target in subprocess_targets:
        assert '"plan"' in target

    forbidden = ["actuate", "arm(", "takeoff", "serial.write", "gpio", "smbus"]
    lowered = source.lower()
    for word in forbidden:
        assert word not in lowered


def test_accept_line_runs_and_exits_zero():
    """The accept line itself: `uv run python examples/blind_agent_demo.py`."""
    result = subprocess.run(
        [sys.executable, str(DEMO_PATH)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, out + result.stderr
    assert out.index("beat 1") < out.index("beat 2") < out.index("beat 3")
    assert "NO-GO" in out
    assert "GO" in out
