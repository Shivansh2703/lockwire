"""The free package must import and lock on Windows (no fcntl)."""

import importlib
import sys
import types
from pathlib import Path

import pytest


def test_edit_imports_and_locks_without_fcntl(tmp_path: Path, monkeypatch):
    calls: list[tuple[int, int]] = []
    fake = types.SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, n: calls.append((mode, n))
    )
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` raises ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr("os.name", "nt")
    import lockwire.edit as real

    monkeypatch.delitem(sys.modules, "lockwire.edit")  # fresh import; restored on teardown
    edit = importlib.import_module("lockwire.edit")
    assert edit is not real
    with edit._contract_lock(tmp_path / "contract.yaml"):
        pass
    assert calls == [(2, 1), (0, 1)]


def _nt(monkeypatch, locking):
    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("time.sleep", lambda s: None)
    import lockwire.edit as edit

    return edit


def test_lock_retries_then_succeeds(tmp_path: Path, monkeypatch):
    calls: list[int] = []

    def locking(fd, mode, n):
        calls.append(mode)
        if mode == 2 and len(calls) < 3:
            raise OSError("busy")

    edit = _nt(monkeypatch, locking)
    with edit._contract_lock(tmp_path / "c.yaml"):
        assert calls == [2, 2, 2]


def test_lock_times_out_with_clear_error(tmp_path: Path, monkeypatch):
    def locking(fd, mode, n):
        raise OSError("busy")

    edit = _nt(monkeypatch, locking)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    with pytest.raises(edit.EditConflict, match="locked by another lockwire process; retry"):
        with edit._contract_lock(tmp_path / "c.yaml"):
            pass
