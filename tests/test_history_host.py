"""CLI and authenticated-sidecar doors for the accepted git history producer."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from lockwire import __version__
from lockwire import api
from lockwire.cli import app
from lockwire.serve import create_app
from test_api_history import _git, _repo

TOKEN = "history-route-secret"
REAL_OLDEST_CONTRACT = "785053cf4af0542277493b69919039a02e7abf28"


def _client(*, robot: bool = False) -> TestClient:
    return TestClient(create_app(TOKEN, robot=robot), base_url="http://127.0.0.1")


def _auth() -> dict[str, str]:
    return {"X-Lockwire-Token": TOKEN}


def test_ci_history_checkout_fetches_full_history():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    assert "fetch-depth: 0" in workflow.read_text(encoding="utf-8")


def test_cli_path_ref_and_range_are_the_api_rows(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    contract = repo / "lockwire.yaml"
    oldest = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    runner = CliRunner()

    monkeypatch.chdir(repo)
    full = runner.invoke(app, ["log", "--json"])
    assert full.exit_code == 0, full.output
    assert json.loads(full.output) == api.log(contract)

    at_ref = runner.invoke(app, ["log", "--json", "HEAD"])
    assert at_ref.exit_code == 0, at_ref.output
    assert json.loads(at_ref.output) == api.log(contract, "HEAD")

    revision_range = f"{oldest}..HEAD"
    ranged = runner.invoke(app, ["log", "--json", revision_range, "--", "lockwire.yaml"])
    assert ranged.exit_code == 0, ranged.output
    ranged_rows = json.loads(ranged.output)
    assert ranged_rows == api.log(contract, revision_range)
    assert [row["message"] for row in ranged_rows] == ["capacity 8"]

    monkeypatch.chdir(tmp_path)
    by_absolute_path = runner.invoke(app, ["log", "--json", "--", str(contract)])
    assert by_absolute_path.exit_code == 0, by_absolute_path.output
    assert json.loads(by_absolute_path.output) == api.log(contract)


def test_cli_history_is_path_scoped_and_human_output_is_nonempty(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "README").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "unrelated change")
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["log"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "capacity 8" in lines[0]
    assert "capacity 1" in lines[1]
    assert "unrelated change" not in result.output


def test_real_history_keeps_the_unsupported_oldest_contract(tmp_path, monkeypatch):
    contract = Path(__file__).parent / "fixtures" / "contract_clean.yaml"
    repo = Path(_git(contract.parent, "rev-parse", "--show-toplevel"))
    relpath = contract.relative_to(repo)
    shas = _git(repo, "log", "--format=%H", "--", str(relpath)).splitlines()
    if REAL_OLDEST_CONTRACT not in shas:
        pytest.skip("this checkout does not carry the original contract history")

    rows = api.log(contract)

    assert len(shas) == len(rows) == 11
    assert shas[-1] == REAL_OLDEST_CONTRACT
    oldest = rows[-1]
    assert set(oldest) == {"sha", "author", "date", "message", "verdict", "fact_diff"}
    assert oldest["sha"] == REAL_OLDEST_CONTRACT
    assert oldest["verdict"] == "REFUSED"
    assert oldest["fact_diff"] is None
    assert api.log(contract, REAL_OLDEST_CONTRACT) == [oldest]

    monkeypatch.chdir(tmp_path)
    cli = CliRunner().invoke(app, ["log", "--json", REAL_OLDEST_CONTRACT, "--", str(contract)])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output) == [oldest]


def test_cli_unknown_single_ref_matches_api_refusal(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    contract = repo / "lockwire.yaml"
    missing_ref = "definitely-missing-ref"
    monkeypatch.chdir(repo)

    with pytest.raises(ValueError) as error:
        api.log(contract, missing_ref)
    cli = CliRunner().invoke(app, ["log", missing_ref, "--json"])

    assert cli.exit_code == 2
    assert cli.output.strip() == str(error.value) == f"no such ref {missing_ref!r}"


def test_sidecar_history_is_authenticated_and_byte_equal_to_cli(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    contract = repo / "lockwire.yaml"
    oldest = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    revision_range = f"{oldest}..HEAD"
    monkeypatch.chdir(repo)
    request = {
        "command": "log",
        "args": {"contract": str(contract), "revision_range": revision_range},
    }
    client = _client()

    unauthenticated = client.post("/v1/run", json=request)
    assert unauthenticated.status_code == 401
    assert TOKEN not in unauthenticated.text

    response = client.post("/v1/run", headers=_auth(), json=request)
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == __version__
    assert body["cli_line"] == f"lockwire log {revision_range} -- {contract}"
    assert body["exit_code"] == 0
    assert body["verdict"] is None
    assert body["refusal"] is None
    assert body["payload"] == api.log(contract, revision_range)
    assert TOKEN not in response.text

    cli = CliRunner().invoke(app, ["log", "--json", revision_range, "--", str(contract)])
    assert cli.exit_code == 0, cli.output
    assert cli.output == json.dumps(body["payload"], indent=2) + "\n"


def test_sidecar_malformed_and_unsafe_ref_are_named_only_after_auth(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    contract = repo / "lockwire.yaml"
    monkeypatch.chdir(repo)
    malformed = {
        "command": "log",
        "args": {"contract": str(contract), "unexpected": True},
    }
    client = _client()

    assert client.post("/v1/run", json=malformed).status_code == 401
    assert client.post("/v1/run", headers=_auth(), json=malformed).status_code == 422

    unsafe = client.post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "log",
            "args": {
                "contract": str(contract),
                "revision_range": "--output=stolen..HEAD",
            },
        },
    )
    assert unsafe.status_code == 200
    assert unsafe.json()["exit_code"] == 2
    assert unsafe.json()["payload"] is None
    assert unsafe.json()["refusal"] == "no such ref '--output=stolen..HEAD'"
