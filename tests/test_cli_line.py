"""One shared shell-line builder (`shlex.quote`), called by every
composer, and one rule that no command can drop a flag it was given.

`cli.py`'s emitting commands — `add`, the page footer, `report`,
`scene import`, `declare`, `review`, `apply` — plus `scan.py`'s docker line,
all route through the one shared builder (`lockwire.cli_line.cli_line`).
MCP's own `_cli_line` (mcp_server.py) predates the shared builder and originally
stood on its own `shlex.quote` call. It has since been migrated onto the shared builder too, so `cli_line.py` is now
the only file that calls `shlex.quote` to build a line from scratch.

An earlier version of this test covered `add device` and `add rail` and passed 24/24 value
flags, while three commands nobody had written a replay test for dropped four
flags between them. So the accept line here is not another replay: it
enumerates each emitting command's parameters from the command object itself.
"""

from __future__ import annotations
import ast
import json
import shlex
import subprocess
from pathlib import Path
import typer.main
from typer.testing import CliRunner
from lockwire.cli import app
from lockwire.cli_line import cli_line


runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"
X500_CONTRACT = FIXTURES / "x500" / "contract.yaml"


def test_line_splits_back_into_the_arguments_that_ran():
    parts = ["lockwire", "add", "device", "/tmp/a.yaml", "--model", "oak-d lite"]
    line = cli_line(*parts)
    assert shlex.split(line) == parts


def test_values_with_spaces_and_quotes_round_trip():
    parts = [
        "lockwire",
        "add",
        "device",
        "/tmp/has space/a.yaml",
        "--witness",
        "O'Brien",
        "--note",
        "",
        "--source",
        # both quote characters at once — this is where Python's own repr()
        # quoting (single-quotes, backslash-escaping the inner apostrophe)
        # stops being valid shell syntax; shlex.quote must still round-trip it.
        'it\'s a "test"',
    ]
    line = cli_line(*parts)
    assert shlex.split(line) == parts


def test_add_device_line_replays_byte_identical(tmp_path):
    """The box's own accept line: the printed `ran:` line for `add device`,
    with the target path swapped, replays to a byte-identical file — proving
    every value flag (including one with a space) survives the round trip.
    """
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_bytes(X500_CONTRACT.read_bytes())
    b.write_bytes(X500_CONTRACT.read_bytes())

    result = runner.invoke(
        app,
        [
            "add",
            "device",
            str(a),
            "--id",
            "dev.camera.new",
            "--model",
            "mdl.camera.oak-d-lite",
            "--owner",
            "perception",
            "--rail",
            "rail.pm02-5v",
            "--draw",
            "2.4 W",
            "--source",
            "OAK-D datasheet p.2",
            "--witness",
            "Maya Chen",
        ],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    ran_line = next(line for line in output.splitlines() if line.startswith("ran: "))
    tokens = shlex.split(ran_line[len("ran: ") :])
    assert tokens[0] == "lockwire"
    assert "--draw" in tokens and "2.4 W" in tokens
    assert "--witness" in tokens and "Maya Chen" in tokens

    replay = [str(b) if tok == str(a) else tok for tok in tokens[1:]]
    replay_result = runner.invoke(app, replay)
    assert replay_result.exit_code == 0, replay_result.output
    assert a.read_bytes() == b.read_bytes()


def test_only_one_cli_line_builder_exists():
    """Exactly one function in src/lockwire calls `shlex.quote` to build a
    shell line from scratch (`lockwire.cli_line.cli_line`); every other
    composer (`cli.py`'s `ran:`/`add`/gui-footer/report lines, `scan.py`'s
    docker line, and — since its migration — MCP's own `_cli_line`) calls
    it instead of quoting independently.
    """
    src_dir = Path(__file__).parent.parent / "src" / "lockwire"
    dirs = [str(d) for d in (src_dir, src_dir.with_name("lockwire_pro")) if d.exists()]
    hits = subprocess.run(
        ["grep", "-rl", "shlex.quote(", *dirs],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    files = {Path(p).name for p in hits}
    assert files == {"cli_line.py"}, files


# --------------------------------------------------------------------------
# The rule, not one replayed command .
#
# Round 1 tested `add device` and `add rail` and passed 24/24 value flags —
# and three commands that nobody wrote a replay test for dropped four flags
# between them: `scene import` lost `--package-root`, `declare` lost `--note`
# and `--locked-at`, `apply` lost `--dry-run`. A replay test proves the
# command it replays; it does not prove the rule. So the accept line below
# enumerates each emitting command's parameters from the command object
# itself and asserts every one the user set reaches the printed line.
# --------------------------------------------------------------------------

X500_SCENE = FIXTURES / "x500" / "x500.glb"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"


def _click_command(path: tuple[str, ...]):
    """The command's own click object — never a hand-written flag list."""
    command = typer.main.get_command(app)
    for name in path:
        command = command.commands[name]
    return command


def _argv_from_values(command, values: dict) -> list[str]:
    """Turn {param name: value} into the argv a user would type.

    ``True``/``False`` mean a bool flag: its own opt, or its ``--no-...``
    secondary opt. A list means a repeatable option.
    """
    argv = [str(values[p.name]) for p in command.params if p.param_type_name == "argument"]
    for param in command.params:
        if param.name not in values or param.param_type_name == "argument":
            continue
        value = values[param.name]
        if value is True:
            argv.append(param.opts[0])
        elif value is False:
            argv.append(param.secondary_opts[0])
        elif isinstance(value, list):
            for item in value:
                argv += [param.opts[0], str(item)]
        else:
            argv += [param.opts[0], str(value)]
    return argv


def _expected_tokens(command, values: dict) -> list[str]:
    """Every token the emitted line must carry for those values."""
    expected = []
    for param in command.params:
        if param.name not in values:
            continue
        value = values[param.name]
        if param.param_type_name == "argument":
            expected.append(str(value))
        elif value is True:
            expected.append(param.opts[0])
        elif value is False:
            expected.append(param.secondary_opts[0])
        elif isinstance(value, list):
            expected += [str(item) for item in value]
            expected.append(param.opts[0])
        else:
            expected += [param.opts[0], str(value)]
    return expected


def _ran_line(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("ran: "))[len("ran: ") :]


def _footer_line(page: Path) -> str:
    html = page.read_text()
    idx = html.index("const DATA=") + len("const DATA=")
    data, _ = json.JSONDecoder().raw_decode(html[idx:])
    return data["cli_line"]


def test_a_flag_nobody_typed_is_not_emitted(tmp_path):
    """The other direction of the same lie. `apply` without `--dry-run`
    writes for real, so a line that grew a `--dry-run` would describe a run
    that changed nothing — as damaging as the missing flag, backwards.

    Asserted as exact equality with the argv actually typed, so neither a
    bool nor a defaulted value option can appear out of nowhere.
    """
    contract = tmp_path / "c.yaml"
    contract.write_bytes(X500_CONTRACT.read_bytes())
    changes = tmp_path / "changes.yaml"
    changes.write_text(
        "- path: topology/rails/rail.pm02-5v/capacity\n"
        "  value: 18 W\n"
        "  source: zz source\n"
        "  witness: zz witness\n"
    )

    argv = ["apply", str(contract), str(changes)]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert shlex.split(_ran_line(result.output)) == ["lockwire", *argv]

    argv = [
        "declare",
        str(contract),
        "devices/dev.camera.oak-d-lite/physical/mass",
        "0.061 kg",
        "--source",
        "zz source",
        "--witness",
        "zz witness",
    ]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert shlex.split(_ran_line(result.output)) == ["lockwire", *argv]


def test_no_emitted_line_in_the_cli_is_composed_without_the_shared_rebuild():
    """Source-level guard on the rule: every `build_cli_line(...)` call in
    `cli.py` rebuilds its argv with `argv_from_context`, and any literal
    argument appended *beside* that call replaces only a param the same
    `argv_from_context(...)` call excluded — never adds a new one.

    An earlier version (`"argv_from_context" in call`) only checked that the
    string appeared somewhere in the call block, so a composer that calls
    `argv_from_context` *and then* tacks on its own hand-built flag —
    `build_cli_line("lockwire", "add", *argv_from_context(ctx),
    "--hand-built-flag")` — passed it: the shared rebuild is in the block,
    the extra literal is invisible to a substring check. Nobody deletes a
    working shared call; they add a special case beside it, so that hybrid
    is the realistic failure, not pure hand-listing.

    Two composers legitimately do append a literal beside the rebuild —
    `gui` and `report` both `exclude={"out"}` from `argv_from_context` and
    append `"--out", "<this file>"` in its place, because the page's bytes
    must not depend on its own output filename. A bare "no literals beside
    argv_from_context" rule would redden those. So this walks the actual
    call via `ast` (grep/string matching cannot tell "the call's arguments
    are X" from "the call's text contains X" — that gap is exactly what let
    the hybrid through) and allows a trailing literal `--flag value` pair
    only when `flag` names a parameter the same call excluded.
    """
    cli_path = Path(__file__).parent.parent / "src" / "lockwire" / "cli.py"
    tree = ast.parse(cli_path.read_text(), filename=str(cli_path))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_cli_line"
    ]
    assert calls, "no build_cli_line(...) composer found in cli.py"

    bad: list[str] = []
    for call in calls:
        source = ast.unparse(call)
        argv_call = None
        excluded: set[str] = set()
        after: list[ast.expr] = []
        for arg in call.args:
            if (
                argv_call is None
                and isinstance(arg, ast.Starred)
                and isinstance(arg.value, ast.Call)
                and isinstance(arg.value.func, ast.Name)
                and arg.value.func.id == "argv_from_context"
            ):
                argv_call = arg.value
                for kw in argv_call.keywords:
                    if kw.arg == "exclude" and isinstance(kw.value, ast.Set):
                        excluded |= {
                            elt.value
                            for elt in kw.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        }
                continue
            if argv_call is not None:
                after.append(arg)

        if argv_call is None:
            bad.append(f"{source}: no argv_from_context(...) unpack found (hand-listed flags)")
            continue

        if len(after) % 2 != 0:
            bad.append(
                f"{source}: trailing literal with no placeholder value: {ast.unparse(after[-1])}"
            )
            continue

        trailing_flags = set()
        for flag_node, _value_node in zip(after[0::2], after[1::2]):
            if not (
                isinstance(flag_node, ast.Constant)
                and isinstance(flag_node.value, str)
                and flag_node.value.startswith("--")
            ):
                bad.append(f"{source}: trailing literal {ast.unparse(flag_node)} is not a --flag")
                continue
            trailing_flags.add(flag_node.value[2:].replace("-", "_"))

        unexcluded = trailing_flags - excluded
        if unexcluded:
            bad.append(
                f"{source}: literal flag(s) {sorted(unexcluded)} appended beside "
                "argv_from_context without excluding them from it — the hybrid "
                "this box guards against"
            )
    assert not bad, bad
