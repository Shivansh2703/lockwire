"""The sidecar: GET /v1/hello, POST /v1/run for the read-only commands,
and ``lockwire serve``'s lifecycle. Uses FastAPI's TestClient for the app itself
(no socket involved) and one real subprocess test for the actual bind/port/token/
exit lifecycle a real Electron parent would drive.

The sidecar's HTTP surface is private to the app — these tests hold that
shape, not a published contract.
"""

import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FIXTURES
from lockwire_pro import api_pro as api
from lockwire import edit
from lockwire import serve
from lockwire_pro.api_payload_pro import CheckPayloadPro as CheckPayload
from lockwire_pro.api_payload_pro import RunStatePayload
from lockwire.cli import app as cli_app
from lockwire_pro.jobs import Job
from lockwire.report_json import HEALTH_ARTIFACT_TYPE, envelope
from lockwire.serve import create_app


X500 = FIXTURES / "x500" / "contract.yaml"


BAGS = FIXTURES / "bags"


def _baseline_artifact(tmp_path: Path) -> Path:
    """A blessed baseline artifact, so ``baseline-check`` reaches its contract
    load — mirrors ``tests/test_cli.py``'s helper of the same name."""
    from typer.testing import CliRunner

    from lockwire.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app, ["baseline", str(BAGS / "clean"), "--context", "nominal-op"]
    )
    assert result.exit_code == 0, result.output
    artifact = tmp_path / "baseline.yaml"
    artifact.write_text(re.sub(r"\x1b\[[0-9;]*m", "", result.output))
    return artifact


TOKEN = "test-token"


def _client(app) -> TestClient:
    """Loopback base_url, so the Host header the tests send is the one a real
    caller sends. TestClient's default is ``http://testserver``, and accepting
    that name would mean a test-only bypass living in the production guard."""
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client() -> TestClient:
    return _client(create_app(TOKEN))


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"X-Lockwire-Token": token}


def _actuator_draft_arguments(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    from test_actuator_check_transport import _path_backed_case

    _contract, contract, evidence, evidence_root, revision = _path_backed_case(tmp_path)
    return contract, {
        "contract": str(contract),
        "case_id": "elbow.nominal",
        "evidence": evidence.model_dump(mode="json"),
        "evidence_root": str(evidence_root),
        "expected_revision": revision,
        "provenance": "operator bench capture",
        "discrepancy": "measured tracking differs from the intended actuator response",
        "alternatives": ["inspect the fixture and repeat the capture"],
        "proposed_target": "physical",
        "proposed_patch": "inspect the linkage and correct mechanical interference",
        "rationale": "the cited capture binds the discrepancy to this actuator case",
        "correction_category": "physical",
        "required_recheck": ["rerun elbow.nominal with new hash-bound evidence"],
    }


def test_actuator_review_draft_is_api_routed_replayable_and_non_applying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract, args = _actuator_draft_arguments(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def refuse_action(*_args, **_kwargs):
        raise AssertionError("draft crossed a software write or subprocess boundary")

    monkeypatch.setattr("lockwire.api.apply_changes", refuse_action)
    monkeypatch.setattr("lockwire.api.apply_review", refuse_action)
    monkeypatch.setattr(edit, "_replace", refuse_action)
    monkeypatch.setattr(subprocess, "Popen", refuse_action)
    monkeypatch.setattr(os, "system", refuse_action)
    response = _client(create_app(TOKEN)).post(
        "/v1/run",
        headers=_auth(),
        json={"command": "actuator-review-draft", "args": args},
    )
    assert response.status_code == 200
    reply = response.json()
    expected = api.actuator_review_draft(
        contract,
        args["case_id"],
        evidence=api.ArtifactRef.model_validate(args["evidence"]),
        evidence_root=args["evidence_root"],
        expected_revision=args["expected_revision"],
        provenance=args["provenance"],
        discrepancy=args["discrepancy"],
        alternatives=args["alternatives"],
        proposed_target=args["proposed_target"],
        proposed_patch=args["proposed_patch"],
        rationale=args["rationale"],
        correction_category=args["correction_category"],
        required_recheck=args["required_recheck"],
    )
    assert reply["payload"] == expected
    assert "lockwire actuator-review-draft" in reply["cli_line"]
    assert "--expected-revision" in reply["cli_line"]
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_actuator_review_draft_refuses_unknown_or_stale_evidence_request(tmp_path: Path):
    contract, args = _actuator_draft_arguments(tmp_path)
    before = contract.read_bytes()
    malformed = {**args, "unreviewed": True}
    response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "actuator-review-draft", "args": malformed}
    )
    assert response.status_code == 422
    stale = {**args, "expected_revision": "0" * 64}
    response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "actuator-review-draft", "args": stale}
    )
    reply = response.json()
    assert response.status_code == 200
    assert reply["exit_code"] == 2
    assert reply["payload"] is None
    assert reply["refusal"]
    assert "Traceback" not in reply["refusal"]
    assert contract.read_bytes() == before


def test_served_spec_review_uses_actual_case_bound_change_and_replayable_cli(tmp_path: Path):
    contract, args = _actuator_draft_arguments(tmp_path)
    before = contract.read_bytes()
    args.pop("proposed_target")
    args.pop("proposed_patch")
    args["correction_category"] = "spec_design"
    args["proposed_changes"] = [
        {
            "path": "actuator_cases/elbow.nominal/position_tracking_max_error",
            "value": "0.06",
            "source": "design-review",
            "witness": "bench operator",
            "locked_at": "2026-09-28",
            "unit": "rad",
        }
    ]
    response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "actuator-review-draft", "args": args}
    )
    assert response.status_code == 200
    reply = response.json()
    assert reply["refusal"] is None
    document = CheckPayload.model_validate(reply["payload"])
    assert document.actuator_review_draft is not None
    assert document.actuator_review_draft.correction_category == "spec_design"
    assert document.actuator_review_draft.proposed_patch.kind == "lockwire_spec"
    assert "source design-review" in document.actuator_review_draft.proposed_patch.patch
    assert "witness bench operator" in document.actuator_review_draft.proposed_patch.patch
    assert contract.read_bytes() == before

    argv = shlex.split(reply["cli_line"])
    assert argv[:2] == ["lockwire", "actuator-review-draft"]
    output = tmp_path / "review.json"
    replay = CliRunner().invoke(cli_app, [*argv[1:], "--json", str(output)])
    assert replay.exit_code == reply["exit_code"], replay.output
    assert json.loads(output.read_text()) == reply["payload"]
    assert contract.read_bytes() == before


@pytest.mark.parametrize(
    ("change", "category", "status", "reason"),
    [
        (
            {"path": "model/joints/elbow/limits/effort"},
            "spec_design",
            200,
            "selected actuator case",
        ),
        ({}, "physical", 200, "requires correction_category spec_design"),
        ({"locked_at": "2026-09-28junk"}, "spec_design", 200, "Invalid isoformat string"),
    ],
)
def test_served_spec_review_refuses_unbound_change_without_contract_write(
    tmp_path: Path, change: dict[str, str], category: str, status: int, reason: str
):
    contract, args = _actuator_draft_arguments(tmp_path)
    before = contract.read_bytes()
    args.pop("proposed_target")
    args.pop("proposed_patch")
    args["correction_category"] = category
    args["proposed_changes"] = [
        {
            "path": "actuator_cases/elbow.nominal/position_tracking_max_error",
            "value": "0.06",
            "source": "design-review",
            "witness": "bench operator",
            "locked_at": "2026-09-28",
            **change,
        }
    ]
    response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "actuator-review-draft", "args": args}
    )
    assert response.status_code == status
    assert reason in response.json()["refusal"]
    assert response.json()["payload"] is None
    assert contract.read_bytes() == before


@pytest.mark.parametrize("changes", [[], [{"path": "a"}], None])
def test_served_spec_review_refuses_empty_untyped_or_mixed_input(tmp_path: Path, changes):
    _, args = _actuator_draft_arguments(tmp_path)
    if changes is not None:
        args.pop("proposed_target")
        args.pop("proposed_patch")
    else:
        changes = [
            {
                "path": "actuator_cases/elbow.nominal/position_tracking_max_error",
                "value": "0.06",
                "source": "design-review",
                "witness": "bench operator",
                "locked_at": "2026-09-28",
            }
        ]
    args["correction_category"] = "spec_design"
    args["proposed_changes"] = changes
    response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "actuator-review-draft", "args": args}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("target", ["firmware", "configuration"])
def test_served_review_refuses_unsupported_category_binding(tmp_path: Path, target: str):
    contract, args = _actuator_draft_arguments(tmp_path)
    before = contract.read_bytes()
    args["proposed_target"] = target
    args["correction_category"] = "spec_design"
    response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "actuator-review-draft", "args": args}
    )
    assert response.status_code == 200
    assert "no supported correction_category binding" in response.json()["refusal"]
    assert response.json()["payload"] is None
    assert contract.read_bytes() == before


def test_run_job_http_start_then_poll_completes_with_producer_payload(tmp_path: Path):
    contract = tmp_path / "contract.yaml"
    shutil.copy(FIXTURES / "contract_clean.yaml", contract)
    app = create_app(TOKEN)

    with _client(app) as run_client:
        started = run_client.post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "run-start",
                "args": {"contract": str(contract), "recording": str(BAGS / "clean")},
            },
        ).json()
        assert started["refusal"] is None

        deadline = time.monotonic() + 30
        polled = run_client.post(
            "/v1/run", headers=_auth(), json={"command": "run-poll", "args": {}}
        ).json()
        while polled["payload"]["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            polled = run_client.post(
                "/v1/run", headers=_auth(), json={"command": "run-poll", "args": {}}
            ).json()

    state = polled["payload"]
    assert polled["refusal"] is None
    assert state["status"] == "completed"
    assert state["exit_code"] == 0
    assert state["rows_written"] == 5
    assert state["document"]["verdict"] == "GO"
    assert state["health"]["chain"]["rows"] == 5
    assert (
        RunStatePayload.model_validate(state).model_dump(mode="json", exclude_unset=True) == state
    )
    job = app.state.run_jobs._job
    assert job.process.poll() is not None
    assert not job.work_dir.exists()


def test_run_job_http_ssh_requires_pinned_source_before_launch(tmp_path: Path):
    contract = tmp_path / "contract.yaml"
    shutil.copy(FIXTURES / "contract_clean.yaml", contract)
    app = create_app(TOKEN)
    with _client(app) as run_client:
        reply = run_client.post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "run-start",
                "args": {
                    "contract": str(contract),
                    "live": True,
                    "ssh_host": "127.0.0.1",
                    "ssh_target": "declared-target",
                    "ssh_image": "declared-image",
                },
            },
        ).json()
    assert "identity file, known hosts" in reply["refusal"]
    assert app.state.run_jobs._job is None


def test_run_state_payload_canonicalizes_internal_health_no_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Run consumes the health producer's public verdict spelling, not its enum code."""
    from lockwire_pro import health
    from lockwire.report import Verdict

    contract = tmp_path / "contract.yaml"
    shutil.copy(FIXTURES / "contract_clean.yaml", contract)
    monkeypatch.setattr(
        health,
        "_verdict",
        lambda _findings: Verdict("NO_GO", 1, "", 1, 0, 0, None),
    )

    health_document = api.health_document(contract)
    state = {
        "status": "completed",
        "exit_code": 1,
        "rows_written": 0,
        "document": None,
        "output": "",
        "errors": "",
        "error": None,
        "health": health_document,
    }

    assert health_document["verdict"] == "NO-GO"
    assert RunStatePayload.model_validate(state).health.verdict == "NO-GO"


def test_run_health_checks_caller_held_remote_ledger_and_bundle(tmp_path: Path, client: TestClient):
    from test_health_ledger_open import _anchor, _copied_ledger, _remote_bundle

    contract, ledger = _copied_ledger(tmp_path)
    root, refs = _remote_bundle(tmp_path, contract)
    rows, head = _anchor(ledger)
    args = {
        "contract": str(contract),
        "json_out": str(contract.parent / "health.json"),
        "ledger": str(ledger),
        "anchor_rows": rows,
        "anchor_head": head,
        "bundle_dependencies": [ref.model_dump(mode="json") for ref in refs],
        "evidence_root": str(root),
    }

    def read_health() -> dict:
        response = client.post("/v1/run", headers=_auth(), json={"command": "health", "args": args})
        assert response.status_code == 200
        return response.json()

    valid = read_health()
    assert valid["refusal"] is None
    assert valid["payload"]["integrity"] == {"ok": True, "issues": []}
    assert valid["payload"]["origin"]["path"] == str(ledger.resolve())
    assert "--anchor-head" in valid["cli_line"]
    assert json.loads(Path(args["json_out"]).read_text()) == envelope(
        HEALTH_ARTIFACT_TYPE, valid["payload"]["summary"]
    )

    (root / "firmware.bin").write_bytes(b"changed after the caller held its hash")
    stale = read_health()
    assert stale["refusal"] is None
    assert stale["exit_code"] == 1
    assert stale["verdict"] == stale["payload"]["verdict"] == "NO-GO"
    assert stale["payload"]["integrity"]["ok"] is False
    assert "firmware.bin" in " ".join(stale["payload"]["integrity"]["issues"])

    (root / "firmware.bin").write_bytes(b"selected firmware bytes")
    ledger.write_bytes(b"")
    truncated = read_health()
    assert truncated["refusal"] is None
    assert truncated["exit_code"] == 1
    assert truncated["verdict"] == "NO-GO"
    assert any(
        "row count observed" in issue for issue in truncated["payload"]["integrity"]["issues"]
    )


def test_run_job_http_poll_cancel_preserves_partial_and_cleans_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract = tmp_path / "contract.yaml"
    shutil.copy(FIXTURES / "contract_clean.yaml", contract)
    work_dir = tmp_path / "job-work"
    work_dir.mkdir()
    (work_dir / "output.txt").write_text("partial output\n", encoding="utf-8")
    (work_dir / "errors.txt").write_text("", encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    calls: list[tuple[str, dict]] = []

    def fake_start(kind: str, **kwargs) -> Job:
        calls.append((kind, kwargs))
        return Job(kind, process, work_dir, contract)

    monkeypatch.setattr(api, "start_job", fake_start)
    app = create_app(TOKEN)
    try:
        with _client(app) as run_client:
            request = {
                "command": "run-start",
                "args": {
                    "contract": str(contract),
                    "recording": str(BAGS / "clean"),
                    "context": "nominal-op",
                },
            }
            started = run_client.post("/v1/run", headers=_auth(), json=request).json()
            polled = run_client.post(
                "/v1/run", headers=_auth(), json={"command": "run-poll", "args": {}}
            ).json()
            double_start = run_client.post("/v1/run", headers=_auth(), json=request).json()
            cancelled = run_client.post(
                "/v1/run", headers=_auth(), json={"command": "run-cancel", "args": {}}
            ).json()

        assert started["payload"]["status"] == "running"
        assert polled["payload"]["status"] == "running"
        assert double_start["refusal"] == "a Run job is already running"
        assert cancelled["refusal"] is None
        assert cancelled["payload"]["status"] == "cancelled"
        assert cancelled["payload"]["output"] == "partial output\n"
        assert RunStatePayload.model_validate(cancelled["payload"])
        assert calls == [
            (
                "preflight",
                {
                    "contract": contract,
                    "recording": str(BAGS / "clean"),
                    "context": "nominal-op",
                    "live": False,
                    "container": None,
                    "local": False,
                    "seconds": 10.0,
                    "ssh_host": None,
                    "ssh_port": 22,
                    "ssh_user": "root",
                    "ssh_identity_file": None,
                    "ssh_known_hosts": None,
                    "ssh_target": None,
                    "ssh_image": None,
                    "keep_bag": None,
                },
            )
        ]
        assert process.poll() is not None
        assert not work_dir.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        shutil.rmtree(work_dir, ignore_errors=True)


def test_concurrent_run_starts_leave_one_pollable_cancellable_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract = tmp_path / "contract.yaml"
    shutil.copy(FIXTURES / "contract_clean.yaml", contract)
    first_start = threading.Event()
    overlapping_start = threading.Event()
    release = threading.Event()
    calls_lock = threading.Lock()
    jobs = []

    class FakeJob:
        exit_code = None

        def snapshot(self):
            return {
                "status": "running" if self.exit_code is None else "cancelled",
                "exit_code": self.exit_code,
                "rows_written": 0,
                "document": None,
                "output": "",
                "errors": "",
                "error": None,
            }

        def cancel(self):
            self.exit_code = -15

    def fake_start(*_args, **_kwargs):
        job = FakeJob()
        with calls_lock:
            jobs.append(job)
            call_number = len(jobs)
        if call_number == 1:
            first_start.set()
            assert release.wait(timeout=5)
        else:
            overlapping_start.set()
        return job

    monkeypatch.setattr(api, "start_job", fake_start)
    app = create_app(TOKEN)
    request = {
        "command": "run-start",
        "args": {"contract": str(contract), "recording": str(BAGS / "clean")},
    }

    def post_start():
        with _client(app) as run_client:
            return run_client.post("/v1/run", headers=_auth(), json=request).json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post_start)
        assert first_start.wait(timeout=5)
        second = pool.submit(post_start)
        overlapping_start.wait(timeout=1)
        release.set()
        replies = [first.result(timeout=5), second.result(timeout=5)]

    assert len(jobs) == 1
    assert [reply["refusal"] for reply in replies].count(None) == 1
    assert [reply["refusal"] for reply in replies].count("a Run job is already running") == 1
    with _client(app) as run_client:
        polled = run_client.post(
            "/v1/run", headers=_auth(), json={"command": "run-poll", "args": {}}
        ).json()
        cancelled = run_client.post(
            "/v1/run", headers=_auth(), json={"command": "run-cancel", "args": {}}
        ).json()
    assert polled["payload"]["status"] == "running"
    assert cancelled["payload"]["status"] == "cancelled"
    assert app.state.run_jobs._job is jobs[0]


def test_run_poll_and_cancel_refuse_before_start(client: TestClient):
    for command in ("run-poll", "run-cancel"):
        body = client.post("/v1/run", headers=_auth(), json={"command": command, "args": {}}).json()
        assert body["refusal"] == "no Run job has been started"
        assert body["payload"] is None


def test_analysis_job_commands_share_exclusive_owner_and_cancel_the_same_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class FakeJob:
        exit_code = None

        def __init__(self):
            self.cancelled = False

        def snapshot(self):
            return {
                "status": "cancelled" if self.cancelled else "running",
                "exit_code": self.exit_code,
                "rows_written": 0,
                "document": None,
                "output": "",
                "errors": "",
                "error": None,
            }

        def cancel(self):
            self.cancelled = True
            self.exit_code = -signal.SIGTERM

    job = FakeJob()
    starts = []

    def fake_analysis_start(contract, **kwargs):
        starts.append((contract, kwargs))
        return job

    monkeypatch.setattr(api, "start_analysis_job", fake_analysis_start)
    monkeypatch.setattr(
        api,
        "start_job",
        lambda *_args, **_kwargs: pytest.fail("Run must not start alongside analysis"),
    )
    (tmp_path / "manifest.json").write_bytes(b"{}")
    app = create_app(TOKEN)
    args = {
        "contract": str(X500),
        "expected_revision": "a" * 64,
        "procedure_id": "static",
        "evidence_root": str(tmp_path),
        "package_manifest": str(tmp_path / "manifest.json"),
    }
    with _client(app) as http:
        started = http.post(
            "/v1/run", headers=_auth(), json={"command": "analysis-start", "args": args}
        ).json()
        polled = http.post(
            "/v1/run", headers=_auth(), json={"command": "analysis-poll", "args": {}}
        ).json()
        refused = http.post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "run-start",
                "args": {"contract": str(X500), "recording": str(BAGS / "clean")},
            },
        ).json()
        cancelled = http.post(
            "/v1/run", headers=_auth(), json={"command": "analysis-cancel", "args": {}}
        ).json()

    assert started["payload"]["status"] == "running"
    assert polled["payload"]["status"] == "running"
    assert refused["refusal"] == "a Run job is already running"
    assert cancelled["payload"]["status"] == "cancelled"
    assert starts == [
        (
            X500,
            {
                "expected_revision": "a" * 64,
                "procedure_id": "static",
                "evidence_root": tmp_path,
                "package_manifest_path": tmp_path / "manifest.json",
            },
        )
    ]
    assert app.state.run_jobs._job is job


def test_analysis_poll_keeps_duplicate_json_failure_stable_and_cleans_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class CompletedProcess:
        def poll(self):
            return 0

    work_dir = tmp_path / "duplicate-analysis-job"
    work_dir.mkdir()
    output_text = "solver bytes\n"
    output_sha256 = hashlib.sha256(output_text.encode()).hexdigest()
    (work_dir / "document.json").write_text(
        '{"kind":"analysis_result","kind":"analysis_result",'
        '"status":"COMPLETED","solver_started":true,'
        f'"output_text":{json.dumps(output_text)},"output_sha256":"{output_sha256}"}}',
        encoding="utf-8",
    )
    (work_dir / "output.txt").write_text("", encoding="utf-8")
    (work_dir / "errors.txt").write_text("", encoding="utf-8")
    job = Job("analyze", CompletedProcess(), work_dir)
    monkeypatch.setattr(api, "start_analysis_job", lambda *_args, **_kwargs: job)
    (tmp_path / "manifest.json").write_bytes(b"{}")
    app = create_app(TOKEN)
    args = {
        "contract": str(X500),
        "expected_revision": "a" * 64,
        "procedure_id": "static",
        "evidence_root": str(tmp_path),
        "package_manifest": str(tmp_path / "manifest.json"),
    }

    with _client(app) as http:
        started = http.post(
            "/v1/run", headers=_auth(), json={"command": "analysis-start", "args": args}
        ).json()
        repeated = http.post(
            "/v1/run", headers=_auth(), json={"command": "analysis-poll", "args": {}}
        ).json()

    assert started["refusal"] is None
    assert started["payload"]["status"] == "failed"
    assert started["payload"]["document"] is None
    assert "JSON repeats key 'kind'" in started["payload"]["error"]
    assert repeated["refusal"] is None
    assert repeated["payload"]["status"] == "failed"
    assert repeated["payload"]["error"] == started["payload"]["error"]
    assert not work_dir.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "run-start", "args": {}},
        {
            "command": "run-start",
            "args": {"contract": str(X500), "recording": str(BAGS / "clean"), "seconds": "10"},
        },
        {"command": "run-poll", "args": {"unexpected": True}},
        {"command": "run-cancel", "args": []},
        {"command": "run-unknown", "args": {}},
    ],
)
def test_run_job_request_schema_rejects_malformed_unknown_and_unauthenticated(
    client: TestClient, payload: dict
):
    assert client.post("/v1/run", headers=_auth(), json=payload).status_code == 422
    unauthenticated = client.post("/v1/run", json=payload)
    assert unauthenticated.status_code == 401
    assert TOKEN not in unauthenticated.text


def test_run_preflight_live_refused_not_served_yet(client: TestClient):
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "preflight",
            "args": {"contract": str(X500), "recording": "tests/fixtures/bags/clean", "live": True},
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["exit_code"] == 2
    assert "not served yet" in body["refusal"]


def test_run_preflight_names_the_checks_that_agreed(client: TestClient):
    """a scored recording's reply carries the checks that passed, not only the not-checked rows.

    the x500 ulog measures no declared device (its old
    score was contract-only budget rows), so this uses the clean bag, which does.
    """
    args = {"contract": str(FIXTURES / "contract_clean.yaml"), "recording": str(BAGS / "clean")}
    body = client.post(
        "/v1/run", headers=_auth(), json={"command": "preflight", "args": args}
    ).json()
    document = body["payload"]
    # The old `scored == len(passed)` held only because both were the 3 budget rows.
    # On a measured bag stages 0/1 score presence without a passed row (28 vs 18).
    assert document["verdict"] == "GO" and document["scored"] > 0 and document["passed"]
    assert all(row["label"] and row["observed"] and row["expected"] for row in document["passed"])


def test_baseline_check_calls_library_passed_channels(client: TestClient, tmp_path: Path):
    """``baseline-check`` calls ``api.passed_baseline_channels`` instead of
    carrying its own "which channels passed" computation (one evaluator) — the
    same block that used to sit duplicated in ``cli.py`` and ``serve.py``.

    Bite: monkeypatch the library function to blow up and watch the sidecar's
    own response turn into a refusal carrying the planted message — proof the
    call happens outside the handler's own load-error guard, not that it is
    merely present in the source (that is ``test_handlers_call_the_library_
    not_the_checks`` below)."""

    artifact = _baseline_artifact(tmp_path)
    args = {
        "recording": str(BAGS / "clean"),
        "baseline": str(artifact),
        "contract": str(X500),
        "no_ledger": True,
    }

    def _boom(findings, artifact):
        raise RuntimeError("planted by test_baseline_check_calls_library_passed_channels")

    original = api.passed_baseline_channels
    api.passed_baseline_channels = _boom
    try:
        response = client.post(
            "/v1/run", headers=_auth(), json={"command": "baseline-check", "args": args}
        )
    finally:
        api.passed_baseline_channels = original

    assert response.status_code == 200
    body = response.json()
    assert "planted by test_baseline_check_calls_library_passed_channels" in body["refusal"]


def test_baseline_check_absent_device_is_not_scored_exit_3(client: TestClient, tmp_path: Path):
    """the sidecar reads a missing declared device as the CLI does."""
    from test_baseline import _variant

    args = {
        "recording": str(_variant(tmp_path, drop=("dev.imu",))),
        "baseline": str(_baseline_artifact(tmp_path)),
        "contract": str(X500),
        "no_ledger": True,
    }
    body = client.post(
        "/v1/run", headers=_auth(), json={"command": "baseline-check", "args": args}
    ).json()

    assert body["exit_code"] == 3, body
    assert "NOT SCORED" in body["payload"]


def test_health_absent_worst_is_exit_3(client: TestClient, tmp_path: Path, monkeypatch):
    """Box E: a fleet that reads `absent` is not GO on the transport either."""
    monkeypatch.setattr(api, "health", lambda contract: {"worst": "absent"})
    args = {"contract": str(X500)}
    body = client.post("/v1/run", headers=_auth(), json={"command": "health", "args": args}).json()

    assert body["exit_code"] == 3, body


def test_health_json_out_stays_inside_the_contract_folder(client: TestClient, tmp_path: Path):
    outside = tmp_path / "elsewhere.json"
    args = {"contract": str(X500), "json_out": str(outside)}
    body = client.post("/v1/run", headers=_auth(), json={"command": "health", "args": args}).json()

    assert body["exit_code"] == 2, body
    assert not outside.exists()


PAID_COMMANDS = {
    "actuator-review-draft", "preflight", "baseline-check", "baseline-review", "health",
    "run-start", "run-poll", "run-cancel", "analysis-start", "analysis-poll", "analysis-cancel",
}  # fmt: skip


def test_hello_lists_the_free_commands_and_the_plugins(client: TestClient):
    body = client.get("/v1/hello", headers=_auth()).json()
    names = {command["name"] for command in body["commands"]}
    assert names == set(serve._FREE_COMMANDS) | PAID_COMMANDS


def test_page_takes_a_recording_and_carries_the_run_section(client: TestClient):
    """The paid `page` request adds `recording`; the free one has no such field."""
    from lockwire_pro.api_payload_pro import PagePayloadPro

    reply = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "page", "args": {"contract": str(X500), "recording": str(BAGS / "clean")}},
    ).json()
    assert reply["refusal"] is None
    PagePayloadPro.model_validate(reply["payload"])
    assert reply["payload"]["run"]["recording"] == str(BAGS / "clean")
    assert "recording" not in serve.PageArgs.model_fields


def test_free_sidecar_refuses_paid_commands(monkeypatch: pytest.MonkeyPatch):
    """Moved from test_serve.py (the public snapshot may not name paid commands):
    with no plugin loaded, paid commands are a 422."""
    from lockwire import plugins

    monkeypatch.setattr(plugins, "entry_points", lambda group: [])
    monkeypatch.setattr(plugins, "_ready", False)
    monkeypatch.setattr(plugins, "SERVE_COMMANDS", {})
    monkeypatch.setattr(plugins, "SERVE_SETUP", [])
    monkeypatch.setattr(plugins, "SERVE_POLICY", None)
    client = _client(create_app(TOKEN))
    for command in ("preflight", "run-start", "health", "baseline-check"):
        reply = client.post("/v1/run", headers=_auth(), json={"command": command, "args": {}})
        assert reply.status_code == 422, command


def test_live_run_poll_names_the_devices_it_records(tmp_path: Path, monkeypatch):
    contract = tmp_path / "contract.yaml"
    shutil.copy(FIXTURES / "contract_live.yaml", contract)
    job = type(
        "J",
        (),
        {
            "snapshot": lambda self: {
                "status": "running",
                "exit_code": None,
                "rows_written": 0,
                "document": None,
                "output": "",
                "errors": "",
                "error": None,
            }
        },
    )()
    monkeypatch.setattr("lockwire_pro.api_pro.start_job", lambda *a, **k: job)
    state = api.RunJobController().start(contract, live=True, container="c")
    RunStatePayload.model_validate(state)
    assert state["watching"] == ["dev.imu"]


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("health", {"json_out": "victim.json"}),
        ("run-start", {}),
    ],
)
def test_robot_mode_refuses_paid_write_commands(tmp_path: Path, command: str, args: dict):
    """robot mode allows reads only; describe --out, health json_out
    and run-start wrote or started jobs on the robot."""
    robot = _client(create_app(TOKEN, robot=True))
    if command not in serve.commands():
        pytest.skip(f"{command} is not installed here")
    args = {"contract": str(X500), **{k: str(tmp_path / v) for k, v in args.items()}}
    reply = robot.post("/v1/run", headers=_auth(), json={"command": command, "args": args}).json()
    assert reply["refusal"] == f"write commands are not served on the robot: {command}"
    assert list(tmp_path.iterdir()) == []
