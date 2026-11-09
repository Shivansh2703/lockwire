"""A ledger opens from anywhere, says where it came from, and the
chain is walked on every open — not only under ``--verify``.

Reuses the ledger-chain and health-document machinery: only
``build_document``'s new ``ledger=`` override, and the ``origin`` field it
adds to ``HealthDocument``, are new.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import shlex
import shutil

import pytest
from pathlib import Path

from typer.testing import CliRunner

from lockwire_pro import api_pro as api
from lockwire.cli import app
from lockwire_pro.health import append_health_rows, sidecar_path_for
from lockwire.schema import ArtifactRef, Contract, DeviceInstance, RobotInfo

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"

runner = CliRunner()
NOW = dt.datetime(2026, 9, 7, 12, 0, 0, tzinfo=dt.UTC)


def _contract() -> Contract:
    return Contract(
        robot=RobotInfo(name="testbot"),
        devices=[DeviceInstance(id="dev.a", model="m", owner="o")],
    )


def _copied_ledger(tmp_path: Path) -> tuple[Path, Path]:
    """A contract plus a ledger copied to an unrelated path — the copied-ledger scenario."""
    contract_path = tmp_path / "robot" / "lockwire.yaml"
    contract_path.parent.mkdir()
    shutil.copy(CLEAN_CONTRACT, contract_path)
    append_health_rows(contract_path, _contract(), [], source="unit-test", context=None, now=NOW)
    copied = tmp_path / "laptop" / "copied.health.jsonl"
    copied.parent.mkdir()
    shutil.copy(sidecar_path_for(contract_path), copied)
    return contract_path, copied


def _flat(output: str) -> str:
    return " ".join(output.split())


def _tamper_first_row(ledger_path: Path) -> None:
    """Edit a field the hash covers, without recomputing hash/prev — the
    accept line's own move (a hand edit to a copied ledger)."""
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["reason"] = "tampered"
    lines[0] = json.dumps(row)
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_document_carries_origin_and_chain(tmp_path):
    contract_path, ledger_path = _copied_ledger(tmp_path)

    document = api.health_document(contract_path, ledger=ledger_path)
    assert document["origin"]["path"] == str(ledger_path.resolve())
    assert document["origin"]["copied_at"] is not None
    assert document["chain"] == {
        "rows": 1,
        "skipped": 0,
        "ok": True,
        "line": None,
        "why": None,
    }

    result = runner.invoke(app, ["health", str(contract_path), "--ledger", str(ledger_path)])
    assert result.exit_code != 2, result.output  # 2 = contract could not be read
    assert "copied.health.jsonl" in result.output
    assert "chain verified" in _flat(result.output)
    assert "1 rows" in _flat(result.output)


def test_broken_chain_is_named_on_open(tmp_path):
    """A plain open (no ``--verify``) still walks the chain and names a break."""
    contract_path, ledger_path = _copied_ledger(tmp_path)
    _tamper_first_row(ledger_path)

    result = runner.invoke(app, ["health", str(contract_path), "--ledger", str(ledger_path)])
    assert result.exit_code == 1, result.output
    assert "chain broken at line 1" in _flat(result.output)


def test_broken_chain_still_shows_the_rows(tmp_path):
    """A broken chain is reported, never raised past — the table still prints."""
    contract_path, ledger_path = _copied_ledger(tmp_path)
    _tamper_first_row(ledger_path)

    result = runner.invoke(app, ["health", str(contract_path), "--ledger", str(ledger_path)])
    assert "dev.a" in result.output, result.output
    assert "Traceback" not in result.output


def test_library_opens_a_ledger_from_any_path(tmp_path):
    contract_path, ledger_path = _copied_ledger(tmp_path)

    document = api.health_document(contract_path, ledger=ledger_path)
    assert document["origin"]["path"] == str(ledger_path.resolve())
    assert document["origin"]["copied_at"] is not None
    assert document["chain"]["ok"] is True
    assert document["chain"]["rows"] == 1


def test_mcp_health_accepts_a_ledger_path(tmp_path):
    from lockwire.mcp_server import build_server

    contract_path, ledger_path = _copied_ledger(tmp_path)

    async def run():
        from mcp.client import Client

        async with Client(build_server()) as client:
            return await client.call_tool(
                "health", {"contract": str(contract_path), "ledger": str(ledger_path)}
            )

    result = asyncio.run(run())
    assert not result.is_error
    assert result.structured_content["origin"]["path"] == str(ledger_path.resolve())
    assert "--ledger" in result.structured_content["cli_line"]


def _remote_bundle(tmp_path: Path, contract_path: Path) -> tuple[Path, tuple[ArtifactRef, ...]]:
    root = tmp_path / "bundle"
    root.mkdir()
    shutil.copy(contract_path, root / "contract.yaml")
    (root / "firmware.bin").write_bytes(b"selected firmware bytes")
    return root, tuple(
        ArtifactRef(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (root / "contract.yaml", root / "firmware.bin")
    )


def _anchor(path: Path) -> tuple[int, str]:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return len(rows), rows[-1]["hash"]


def test_caller_anchored_remote_health_invalidates_tamper_and_stale_dependency(tmp_path):
    contract, ledger = _copied_ledger(tmp_path)
    root, refs = _remote_bundle(tmp_path, contract)
    original = ledger.read_bytes()
    kwargs = {
        "ledger": ledger,
        "health_anchor": _anchor(ledger),
        "bundle_dependencies": refs,
        "evidence_root": root,
    }
    legacy = api.health_document(contract, ledger=ledger)
    assert "integrity" not in legacy
    valid = api.health_document(contract, **kwargs)
    assert valid["integrity"] == {"ok": True, "issues": []}
    assert valid["verdict"] == legacy["verdict"]
    assert ledger.read_bytes() == original

    (root / "firmware.bin").write_bytes(b"stale firmware bytes")
    stale = api.health_document(contract, **kwargs)
    assert stale["integrity"]["ok"] is False
    assert "firmware.bin" in " ".join(stale["integrity"]["issues"])
    assert stale["verdict"] == "NO-GO" and stale["exit_code"] == 1
    assert ledger.read_bytes() == original

    (root / "firmware.bin").write_bytes(b"selected firmware bytes")
    _tamper_first_row(ledger)
    tampered = api.health_document(contract, **kwargs)
    assert tampered["chain"]["ok"] is False
    assert any("hash chain broken" in issue for issue in tampered["integrity"]["issues"])
    assert tampered["verdict"] == "NO-GO" and tampered["exit_code"] == 1


def test_anchored_health_binds_bundle_to_the_exact_selected_contract(tmp_path):
    contract, ledger = _copied_ledger(tmp_path)
    root, refs = _remote_bundle(tmp_path, contract)
    anchor = _anchor(ledger)
    ledger_before = ledger.read_bytes()
    with contract.open("a") as handle:
        handle.write("\n")
    result = api.health_document(
        contract,
        ledger=ledger,
        health_anchor=anchor,
        bundle_dependencies=refs,
        evidence_root=root,
    )
    assert result["integrity"]["ok"] is False
    assert any("bundle contract identity" in issue for issue in result["integrity"]["issues"])
    assert result["verdict"] == "NO-GO" and result["exit_code"] == 1
    assert ledger.read_bytes() == ledger_before


def test_anchored_health_refuses_contract_changed_after_parse(tmp_path, monkeypatch):
    contract, ledger = _copied_ledger(tmp_path)
    root, refs = _remote_bundle(tmp_path, contract)
    original_load = api.load

    def change_after_parse(path):
        loaded = original_load(path)
        with contract.open("a") as handle:
            handle.write("\n")
        return loaded

    monkeypatch.setattr(api, "load", change_after_parse)
    result = api.health_document(
        contract,
        ledger=ledger,
        health_anchor=_anchor(ledger),
        bundle_dependencies=refs,
        evidence_root=root,
    )
    assert result["integrity"]["ok"] is False
    assert any(
        "changed between parse and health read" in issue for issue in result["integrity"]["issues"]
    )
    assert result["verdict"] == "NO-GO" and result["exit_code"] == 1


def test_caller_held_head_and_count_detect_remote_ledger_tail_truncation(tmp_path):
    contract, ledger = _copied_ledger(tmp_path)
    append_health_rows(
        contract,
        _contract(),
        [],
        source="unit-test",
        context=None,
        now=NOW + dt.timedelta(seconds=1),
    )
    shutil.copy(sidecar_path_for(contract), ledger)
    root, refs = _remote_bundle(tmp_path, contract)
    anchor = _anchor(ledger)
    assert anchor[0] == 2
    first = ledger.read_bytes().splitlines(keepends=True)[0]
    ledger.write_bytes(first)
    before = ledger.read_bytes()
    result = api.health_document(
        contract,
        ledger=ledger,
        health_anchor=anchor,
        bundle_dependencies=refs,
        evidence_root=root,
    )
    assert result["chain"]["ok"] is True  # the surviving prefix still chains
    assert any(
        "row count observed 1, expected 2" in issue for issue in result["integrity"]["issues"]
    )
    assert any("ledger head observed" in issue for issue in result["integrity"]["issues"])
    assert result["verdict"] == "NO-GO" and result["exit_code"] == 1
    assert ledger.read_bytes() == before


def test_remote_health_requires_both_independent_anchor_and_bundle(tmp_path):
    contract, ledger = _copied_ledger(tmp_path)
    with pytest.raises(ValueError, match="caller-held head/count and bundle"):
        api.health_document(contract, ledger=ledger, health_anchor=_anchor(ledger))


def test_mcp_remote_health_exposes_anchored_dependency_refusal(tmp_path):
    from mcp.client import Client
    from lockwire.mcp_server import build_server

    contract, ledger = _copied_ledger(tmp_path)
    root, refs = _remote_bundle(tmp_path, contract)
    anchor = _anchor(ledger)
    (root / "firmware.bin").write_bytes(b"changed after the caller held its hash")

    async def read_remote(rows: int, head: str):
        async with Client(build_server()) as client:
            return await client.call_tool(
                "health",
                {
                    "contract": str(contract),
                    "ledger": str(ledger),
                    "anchor_rows": rows,
                    "anchor_head": head,
                    "bundle_dependencies": [ref.model_dump() for ref in refs],
                    "evidence_root": str(root),
                },
            )

    result = asyncio.run(read_remote(*anchor))
    assert not result.is_error
    document = result.structured_content
    assert document["integrity"]["ok"] is False
    assert "firmware.bin" in " ".join(document["integrity"]["issues"])
    assert document["verdict"] == "NO-GO" and document["exit_code"] == 1
    assert "--anchor-head" in document["cli_line"]
    cli_result = runner.invoke(app, shlex.split(document["cli_line"])[1:])
    assert cli_result.exit_code == 1, cli_result.output
    assert "health integrity: NO-GO" in _flat(cli_result.output)
    assert "firmware.bin" in cli_result.output

    (root / "firmware.bin").write_bytes(b"selected firmware bytes")
    for rows, head, expected_issue in (
        (anchor[0] + 1, anchor[1], "row count observed"),
        (anchor[0], "f" * 64, "ledger head observed"),
    ):
        mismatch = asyncio.run(read_remote(rows, head))
        assert not mismatch.is_error
        body = mismatch.structured_content
        assert body["integrity"]["ok"] is False
        assert expected_issue in " ".join(body["integrity"]["issues"])
        assert body["verdict"] == "NO-GO" and body["exit_code"] == 1


def test_concurrent_appends_keep_one_chain(tmp_path):
    """two writers appending at once forked the hash chain (chain_break near row 46)."""
    from concurrent.futures import ThreadPoolExecutor

    from lockwire_pro.health import chain_break, read_ledger_lines

    contract_path = tmp_path / "contract.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)

    def write(_):
        for _ in range(40):
            append_health_rows(contract_path, _contract(), [], source="t", context=None, now=NOW)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(write, range(2)))
    assert chain_break(read_ledger_lines(sidecar_path_for(contract_path))) is None


def test_read_only_directory_gives_a_plain_error_not_a_lock_traceback(tmp_path):
    """A read-only directory names the ledger, not the .lock file."""
    import os

    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("permissions are not enforced here")
    folder = tmp_path / "ro"
    folder.mkdir()
    contract_path = folder / "contract.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    folder.chmod(0o555)
    try:
        with pytest.raises(PermissionError) as raised:
            append_health_rows(contract_path, _contract(), [], source="t", context=None, now=NOW)
    finally:
        folder.chmod(0o755)
    assert "cannot write the health ledger" in str(raised.value)
    assert ".lock" not in str(raised.value)
