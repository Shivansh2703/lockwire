"""A model file that is there but cannot be read never comes back GO through the doors below
(check, what-if, suggest-fixes, findings-export, plan, MCP, page, report); it refuses (exit 2) naming the file by its name. A plainly missing file is still scored GO (pinned by test_a_plainly_missing_model_file_is_left_as_it_was)."""

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import FIXTURES
from typer.testing import CliRunner

from lockwire import api, serve
from lockwire.checks import ModelFileRefusal
from lockwire.cli import app
from lockwire.edit import contract_revision

NAME = "servo_arm.urdf"
posix = pytest.mark.skipif(sys.platform == "win32", reason="symlink and chmod need a POSIX system")
KINDS = ["nul", "dir", "toolong", "fifo", "device", "plainpipe"]  # perm and brokenlink: their own tests below


def _unblock_readers(fifo: Path, request) -> None:
    """A daemon writer that keeps opening the pipe and closing it at once, so a door that wrongly
    opens it reads EOF and fails its assertion instead of hanging the run."""
    done = threading.Event()

    def pester():
        while not done.is_set():
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:  # ENXIO: nobody is reading yet
                pass
            time.sleep(0.02)

    threading.Thread(target=pester, daemon=True).start()
    request.addfinalizer(done.set)


@pytest.fixture
def unreadable(request, tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / NAME).write_bytes((FIXTURES / NAME).read_bytes())
    assert CliRunner().invoke(app, ["start", NAME]).exit_code == 0
    lock, kind = tmp_path / "lockwire.yaml", request.param
    bad = {"nul": '"servo\\0arm.urdf"', "toolong": "x" * 300 + ".urdf", "device": "/dev/null"}
    if kind == "plainpipe":  # a pipe whose name has no model suffix: never read, still refused
        if sys.platform == "win32":
            pytest.skip("no named pipes on Windows")
        os.mkfifo(tmp_path / "pipe")
        _unblock_readers(tmp_path / "pipe", request)
        bad[kind] = "pipe"
    if kind in bad:
        lock.write_text(lock.read_text().replace(f"source: {NAME}", f"source: {bad[kind]}"))
    elif kind == "dir":
        (tmp_path / NAME).unlink()
        (tmp_path / NAME).mkdir()
    elif kind == "fifo":
        if sys.platform == "win32":
            pytest.skip("no named pipes on Windows")
        (tmp_path / NAME).unlink()
        os.mkfifo(tmp_path / NAME)
        _unblock_readers(tmp_path / NAME, request)
    elif kind == "brokenlink":
        (tmp_path / NAME).unlink()
        (tmp_path / NAME).symlink_to(tmp_path / "gone.urdf")
    else:
        (tmp_path / NAME).chmod(0)
        request.addfinalizer(lambda: (tmp_path / NAME).chmod(0o644))
    return lock


def _served(command: str, **args):
    body = serve.commands()[command].command.model_validate({"command": command, "args": args})
    return serve.dispatch(body)


def _mcp(tool: str, **args) -> str:
    from mcp.client import Client

    from lockwire.mcp_server import build_server

    async def call():
        async with Client(build_server()) as client:
            return await client.call_tool(tool, args)

    result = asyncio.run(call())
    assert result.is_error
    return result.content[0].text


def _cli(*argv: str) -> str:
    result = CliRunner().invoke(app, list(argv))
    assert result.exit_code == 2
    return result.output


def _api(call, *args) -> str:
    with pytest.raises(ModelFileRefusal) as refused:
        call(*args)
    return str(refused.value)


def _reply(door: str, lock: Path, **args) -> str:
    reply = _served(door, contract=str(lock), **args)
    assert (reply.exit_code, reply.verdict, reply.payload) == (2, None, None)
    return reply.refusal


def _whatif_args(lock: Path) -> dict:
    change = {"path": "model/joints/shoulder/limits/effort", "value": "5", "source": "s", "witness": "w"}
    return {"expected_revision": contract_revision(lock), "change": change}


CHECK_DOORS = {  # doors that score the model file, apart from plan (its own test below)
    "cli check": lambda lock: _cli("check", "lockwire.yaml"),
    "api.check": lambda lock: _api(api.check, lock),
    "served check": lambda lock: _reply("check", lock),
    "served what-if": lambda lock: _reply("what-if", lock, **_whatif_args(lock)),
    "served suggest-fixes": lambda lock: _reply("suggest-fixes", lock, finding="model/structure_hash"),
    "mcp check_contract": lambda lock: _mcp("check_contract", contract=str(lock)),
    "served findings-export": lambda lock: _reply("findings-export", lock),
    "api.page": lambda lock: _api(api.page, lock),
}
PLAN_DOORS = {
    "cli plan": lambda lock: _cli("plan", "lockwire.yaml", "plan.yaml"),
    "api.plan": lambda lock: _api(api.plan, lock, lock.parent / "plan.yaml"),
    "served plan": lambda lock: _reply("plan", lock, plan=str(lock.parent / "plan.yaml")),
    "mcp validate_plan": lambda lock: _mcp("validate_plan", contract=str(lock), plan={"steps": []}),
}

SHOWN_DOORS = {  # no verdict command, but the window and the report show one
    "served page": lambda lock: _reply("page", lock),
    "served report": lambda lock: _reply("report", lock),
    "cli report": lambda lock: _cli("report", "lockwire.yaml", "--out", "report.html"),
}


def _says_why(text: str, lock: Path) -> None:
    assert "so there is no verdict" in text and str(lock.parent) not in text


@pytest.mark.parametrize("unreadable", KINDS, indirect=True)
@pytest.mark.parametrize("door", CHECK_DOORS)
def test_every_verdict_door_refuses_an_unreadable_model(unreadable: Path, door: str):
    _says_why(CHECK_DOORS[door](unreadable), unreadable)


@pytest.mark.parametrize("unreadable", KINDS, indirect=True)
@pytest.mark.parametrize("door", SHOWN_DOORS)
def test_the_page_and_the_report_refuse_an_unreadable_model(unreadable: Path, door: str):
    _says_why(SHOWN_DOORS[door](unreadable), unreadable)


# The three below changed what these doors used to do (each docstring says what it did before): each is pinned by one test alone.


@posix
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
@pytest.mark.parametrize("unreadable", ["perm"], indirect=True)
@pytest.mark.parametrize("door", CHECK_DOORS)
def test_owner_call_no_read_permission_refuses_exit_2_not_3(unreadable: Path, door: str):
    """Before: exit 3 NOT_SCORED at check, api and MCP; exit 0 NOT_SCORED through what-if."""
    _says_why(CHECK_DOORS[door](unreadable), unreadable)


@posix
@pytest.mark.parametrize("unreadable", ["brokenlink"], indirect=True)
@pytest.mark.parametrize("door", CHECK_DOORS)
def test_owner_call_a_broken_symlink_counts_as_unreadable_not_missing(unreadable: Path, door: str):
    """Before: GO at every door, as a plainly missing file still is."""
    _says_why(CHECK_DOORS[door](unreadable), unreadable)


@pytest.mark.parametrize("unreadable", KINDS, indirect=True)
@pytest.mark.parametrize("door", PLAN_DOORS)
def test_owner_call_plan_refuses_for_an_unreadable_model(unreadable: Path, door: str):
    """Plan never reads the model file. Before: GO at every plan door."""
    (unreadable.parent / "plan.yaml").write_text("steps: []\n")
    _says_why(PLAN_DOORS[door](unreadable), unreadable)


@posix
@pytest.mark.parametrize("what", ["pipe", "device"])
@pytest.mark.parametrize("door", ["check", "init", "describe", "start", "report", "accept"])
def test_a_pipe_or_device_given_as_the_file_refuses_exit_2_and_never_hangs(
    tmp_path: Path, monkeypatch, request, door: str, what: str
):
    """The bare doors: `check model.urdf`, `init`, `describe`, `start` and a lock path itself.
    A regression reads EOF from the writer thread and then fails on the message, not on a hang."""
    monkeypatch.chdir(tmp_path)
    target = "/dev/null"
    if what == "pipe":
        target = str(tmp_path / ("lock.yaml" if door in ("describe", "report", "accept") else "m.urdf"))
        os.mkfifo(target)
        _unblock_readers(Path(target), request)
    args = {
        "check": ["check", target],
        "init": ["init", target],
        "describe": ["describe", target],
        "start": ["start", target],
        "report": ["report", target, "--out", "r.html"],
        "accept": ["accept", "model/structure_hash", target, "--reason", "x"],
    }[door]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2 and "pipe or a device" in result.output, result.output


def test_a_plainly_missing_model_file_is_left_as_it_was(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / NAME).write_bytes((FIXTURES / NAME).read_bytes())
    CliRunner().invoke(app, ["start", NAME])
    (tmp_path / NAME).unlink()
    assert _served("check", contract="lockwire.yaml").verdict == "GO"  # a missing file is still scored GO; that is not decided yet


@pytest.mark.parametrize(
    "contract, plan, words",
    [
        ("lockwire.yaml", "gone.yaml", "plan file gone.yaml not found; check the path"),
        ("gone.yaml", "plan.yaml", "contract gone.yaml not found; check the path"),
        ("lockwire.yaml", "bad.yaml", 'in "bad.yaml", line 2'),
        ("lockwire.yaml", "folder", "folder is a folder, not a file; give a file inside it"),
        ("lockwire.yaml", "y" * 300 + ".yaml", ".yaml: File name too long"),
        ("z" * 300 + ".yaml", "plan.yaml", ".yaml: File name too long"),
    ],
)
def test_served_plan_refusal_is_plain_and_names_the_file_only(tmp_path: Path, contract, plan, words):
    (tmp_path / "lockwire.yaml").write_bytes((FIXTURES / "contract_clean.yaml").read_bytes())
    (tmp_path / "plan.yaml").write_text("steps: []\n")
    (tmp_path / "bad.yaml").write_text("steps: [\n")
    (tmp_path / "folder").mkdir()
    reply = _served("plan", contract=str(tmp_path / contract), plan=str(tmp_path / plan))
    assert (reply.exit_code, reply.verdict) == (2, None)
    assert words in reply.refusal and str(tmp_path) not in reply.refusal
