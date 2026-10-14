"""A source is a file beside the contract, a URL or a cited title.

Absolute paths are refused at every write door; one already sitting in a
contract is a finding on `check`, never a crash and never a silent pass.
"""

from __future__ import annotations
import ast
import datetime as dt
from pathlib import Path
import pytest
from lockwire.checks import check_source_portability
from lockwire.edit import (
    Change,
    EditRefusal,
    add_entry,
    apply_changes,
    resolve_source,
    source_is_absolute,
)
from lockwire.checks import check_urdf_drift
from lockwire.scene_import import write_change_file
from lockwire.schema import load_contract


FIXTURE = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"
ABSOLUTE = "/Users/someone/Desktop/datasheet.pdf"


def _copy(tmp_path: Path) -> Path:
    dest = tmp_path / "contract.yaml"
    dest.write_bytes(FIXTURE.read_bytes())
    return dest


# ------------------------------------------------------------------ 1. every door refuses


def _declare_door(contract: Path, source: str) -> None:
    apply_changes(
        contract,
        [
            Change(
                path="devices/dev.gnss.here3-plus/physical/mass",
                value="51.8 g",
                source=source,
                witness="Shivansh",
            )
        ],
    )


def _add_door(contract: Path, source: str) -> None:
    add_entry(
        contract,
        "rail",
        {"id": "rail.new", "capacity": "10 W", "owner": "shivansh"},
        source=source,
        witness="Shivansh",
    )


def _collection_append_door(contract: Path, source: str) -> None:
    """`declare families [...]` — the rows carry their own provenance."""
    apply_changes(
        contract,
        [
            Change(
                path="families",
                value=[
                    {
                        "id": "fam.new",
                        "name": "new family",
                        "provenance": {
                            "witness": "Shivansh",
                            "source": source,
                            "locked_at": "2026-09-08",
                        },
                    }
                ],
                source="datasheet.pdf",
                witness="Shivansh",
            )
        ],
    )


def _scene_import_door(contract: Path, source: str) -> None:
    write_change_file(
        (
            {
                "path": "devices/dev.gnss.here3-plus/physical/mount_position_m",
                "value": [0.0, 0.0, 0.0],
                "source": source,
                "witness": "Shivansh",
            },
        ),
        contract.parent / "changes.yaml",
        scene_source="synthetic.glb",
    )


def _value_side_door(contract: Path, source: str) -> None:
    """`declare .../provenance/source <path>` — the source is the value, not `--source`."""
    apply_changes(
        contract,
        [
            Change(
                path="devices/dev.gnss.here3-plus/provenance/source",
                value=source,
                source="datasheet.pdf",
                witness="Shivansh",
            )
        ],
    )


DOORS = {
    "declare/apply": _declare_door,
    "add": _add_door,
    "declare-collection-append": _collection_append_door,
    "declare-value-side": _value_side_door,
    "scene import": _scene_import_door,
}


@pytest.mark.parametrize("door", DOORS.values(), ids=list(DOORS))
def test_every_write_door_refuses_an_absolute_source(door, tmp_path: Path) -> None:
    contract = _copy(tmp_path)
    before = contract.read_bytes()
    with pytest.raises(EditRefusal):
        door(contract, ABSOLUTE)
    assert contract.read_bytes() == before
    assert not (tmp_path / "changes.yaml").exists()


@pytest.mark.parametrize("door", DOORS.values(), ids=list(DOORS))
def test_every_write_door_accepts_a_relative_source(door, tmp_path: Path) -> None:
    """The guard refuses absolute paths only — the ordinary door still opens."""
    door(_copy(tmp_path), "docs/datasheet.pdf")


# ------------------------------------------------------------------ 2. the refusal is named


def test_the_refusal_names_the_path_the_allowed_forms_and_the_ruling(tmp_path: Path) -> None:
    with pytest.raises(EditRefusal) as refusal:
        _declare_door(_copy(tmp_path), ABSOLUTE)
    message = str(refusal.value)
    assert ABSOLUTE in message
    assert "relative to the contract" in message
    assert "URL" in message
    assert "title" in message
    assert "cannot travel with the contract" in message


def test_an_empty_source_keeps_its_own_refusal(tmp_path: Path) -> None:
    """The one-door message, not the source-path one — one guard, two named refusals."""
    with pytest.raises(EditRefusal) as refusal:
        _declare_door(_copy(tmp_path), "   ")
    assert "--source is required" in str(refusal.value)
    assert "cannot travel with the contract" not in str(refusal.value)


@pytest.mark.parametrize(
    ("source", "absolute", "why"),
    [
        ("/Users/someone/Desktop/ds.pdf", True, "posix absolute"),
        ("~/Desktop/ds.pdf", True, "home-relative is one machine's home"),
        (r"C:\Users\someone\ds.pdf", True, "windows drive, backslash"),
        ("C:/Users/someone/ds.pdf", True, "windows drive, forward slash"),
        ("c:", True, "bare drive"),
        (r"\\fileserver\share\ds.pdf", True, "UNC share"),
        (r"\Users\someone\ds.pdf", True, "backslash-rooted"),
        ("datasheet.pdf", False, "bare filename beside the contract"),
        ("docs/datasheet.pdf", False, "relative path in the repo"),
        ("../shared/datasheet.pdf", False, "escapes the contract dir, still travels"),
        ("https://example.com/ds.pdf", False, "a URL travels"),
        ("file:///Users/someone/ds.pdf", False, "a file: URL is a URL, not a bare path"),
        ("Datasheet Rev C, Table 4", False, "a cited title"),
    ],
)
def test_source_is_absolute_reads_each_shape(source: str, absolute: bool, why: str) -> None:
    assert source_is_absolute(source) is absolute, why


# ------------------------------------------------------------------ 3. relative resolution


def test_a_relative_source_resolves_from_the_contract_directory_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _copy(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert resolve_source(contract, "docs/datasheet.pdf") == tmp_path / "docs" / "datasheet.pdf"
    assert resolve_source(contract, "datasheet.pdf") == tmp_path / "datasheet.pdf"


def test_a_source_that_is_not_a_relative_file_resolves_to_nothing(tmp_path: Path) -> None:
    contract = _copy(tmp_path)
    assert resolve_source(contract, "https://example.com/ds.pdf") is None
    assert resolve_source(contract, "Datasheet Rev C, Table 4") is None
    assert resolve_source(contract, ABSOLUTE) is None


def test_a_clean_contract_gains_no_source_finding() -> None:
    """The false-positive budget: relative sources and titles stay silent."""
    assert check_source_portability(load_contract(FIXTURE)) == []


def test_a_relative_source_written_today_is_readable_back_without_a_finding(
    tmp_path: Path,
) -> None:
    contract = _copy(tmp_path)
    _add_door(contract, "docs/datasheet.pdf")
    loaded = load_contract(contract)
    assert check_source_portability(loaded) == []
    assert loaded.topology is not None
    rail = next(r for r in loaded.topology.rails if r.id == "rail.new")
    assert rail.provenance is not None
    assert resolve_source(contract, rail.provenance.source) == (tmp_path / "docs" / "datasheet.pdf")
    assert rail.provenance.locked_at <= dt.date.today()


# ------------------------------------------------------------------ 3b. the reader resolves it


def test_urdf_drift_resolves_a_relative_model_source_from_the_contract_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write side going relative is only honest if the read side follows.

    `init` stamps the model file relative to the contract; `check_urdf_drift` must
    anchor that to the contract's directory, not the working directory, or drift
    silently stops being scored the moment the source became portable.
    """
    urdf = Path(__file__).parent / "fixtures" / "testbot.urdf"
    home = tmp_path / "robot"
    home.mkdir()
    (home / "testbot.urdf").write_bytes(urdf.read_bytes())
    contract = home / "contract.yaml"

    from lockwire.schema import Contract as _Contract
    from lockwire.schema import RobotInfo
    from lockwire.urdf import parse_urdf

    model = parse_urdf(home / "testbot.urdf")
    spelling = "testbot.urdf"
    loaded = _Contract(
        robot=RobotInfo(name="testbot"),
        model=model.model_copy(
            update={
                "source": spelling,
                "provenance": model.provenance.model_copy(update={"source": spelling}),
            }
        ),
    )
    contract.write_text("robot:\n  name: testbot\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)  # not the contract's directory
    assert check_urdf_drift(loaded) != []  # unanchored: not_scored
    assert check_urdf_drift(loaded)[0].severity == "not_scored"
    assert check_urdf_drift(loaded, contract_path=contract) == []  # anchored: it matches


# ------------------------------------------------------------------ 4. The one-door law, executable


_SRC = Path(__file__).parent.parent / "src" / "lockwire"
_SRCS = [r for r in (_SRC, _SRC.with_name("lockwire_pro")) if r.exists()]
_SANCTIONED_DOOR = _SRC / "gitref.py"
_SUBPROCESS_CALL_NAMES = {"run", "Popen", "call", "check_call", "check_output"}


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_CALL_NAMES


def _collect_list_assignments(tree: ast.AST) -> dict[str, ast.List | ast.Tuple]:
    """Map every plain `name = [...]` / `name = (...)` in the module to its literal.

    Whole-module, not scope-precise: good enough for a guard whose job is to
    resolve `cmd = ["git", ...]; subprocess.run(cmd)` rather than to be a real
    dataflow analysis. A later assignment to the same name overwrites the map
    entry, matching what a straight-line read of the function would see.
    """
    assignments: dict[str, ast.List | ast.Tuple] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
    return assignments


def _resolve_argv(
    expr: ast.expr, assignments: dict[str, ast.List | ast.Tuple]
) -> ast.List | ast.Tuple | None:
    """A list/tuple literal directly, or one reached through a Name it was assigned to."""
    if isinstance(expr, (ast.List, ast.Tuple)):
        return expr
    if isinstance(expr, ast.Name):
        return assignments.get(expr.id)
    return None


def _first_arg_is_git_argv(node: ast.Call, assignments: dict[str, ast.List | ast.Tuple]) -> bool:
    """True when the first positional arg IS, or RESOLVES to, a "git"-headed argv."""
    if not node.args:
        return False
    argv = _resolve_argv(node.args[0], assignments)
    if argv is None or not argv.elts:
        return False
    head = argv.elts[0]
    return isinstance(head, ast.Constant) and head.value == "git"


def _find_git_shellouts(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments = _collect_list_assignments(tree)
    return [
        f"{path}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_subprocess_call(node)
        and _first_arg_is_git_argv(node, assignments)
    ]


def test_only_gitref_shells_out_to_git() -> None:
    """The one-door law: nothing but gitref.py may pass a git argv to subprocess.

    Walks every module under src/lockwire (rglob — subpackages included, not two
    files named by hand). Keyed on GIT, not on subprocess in general: jobs.py (ROS
    launch), scan.py (subprocess_runner), sample_remote.py (docker) and cli.py
    (calls scan.subprocess_runner) all shell out legitimately and must stay green.
    Read as a real AST rather than grepped by substring so that vcs.py:107's
    `argv = ["git", "commit", ...]` — built only for display/dry-run preview and
    never handed to subprocess (actual execution goes through gitref.commit()) —
    does not read as a violation it is not.

    The exemption is BY PATH, not basename — a file elsewhere in the tree named
    `gitref.py` is not the sanctioned door and is not exempt. And an argv built as
    `cmd = ["git", ...]; subprocess.run(cmd)` is resolved through its Name to the
    list literal it was assigned, so passing the list by variable rather than
    inline does not hide it from the walk.
    """
    offenders = []
    for path in sorted(p for r in _SRCS for p in r.rglob("*.py")):
        if path == _SANCTIONED_DOOR:
            continue
        offenders.extend(_find_git_shellouts(path))
    assert not offenders, offenders
