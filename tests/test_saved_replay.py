"""E3.1: caller-pinned saved Test replay through the public library path."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lockwire_pro import api_pro as api
from lockwire_pro.recording import load_recording
from lockwire.scan import CommandResult, docker_runner
from lockwire.schema import MAX_CONTRACT_BYTES, ArtifactRef

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_DOCKERFILE = FIXTURES / "live" / "Dockerfile"
LIVE_IMAGE = "lockwire-live:b105"
SELECTED_TARGET = {
    "build": "image-build-1",
    "pin": "board-pin-map-1",
    "address": "bus-address-map-1",
    "memory": "memory-map-1",
    "partition": "partition-table-1",
    "signed_slot": "signed-slot-A-1",
}


class SelectedTargetRunner:
    """Selected-target reader, either simulated or routed to an owned container."""

    def __init__(self, identity: dict[str, str] | None = None, *, runner=None) -> None:
        if (identity is None) == (runner is None):
            raise ValueError("supply exactly one selected-target source")
        self.identity = identity
        self.runner = runner
        self.calls: list[tuple[str, ...]] = []
        self.result: CommandResult | None = None

    @classmethod
    def from_owned_container(cls, container_id: str) -> "SelectedTargetRunner":
        """Read the identity through the actual selected container's command boundary."""
        return cls(runner=docker_runner(container_id))

    def __call__(self, command: tuple[str, ...]) -> CommandResult:
        self.calls.append(command)
        if command != ("cat", "/var/lockwire/selected-target.json"):
            return CommandResult(127, stderr="unexpected selected-target command")
        if self.result is not None:
            return self.result
        if self.runner is not None:
            return self.runner(command)
        assert self.identity is not None
        return CommandResult(0, json.dumps(self.identity, sort_keys=True))


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _saved_test(tmp_path: Path) -> tuple[Path, Path, Path, dict, ArtifactRef, SelectedTargetRunner]:
    contract = tmp_path / "contract.yaml"
    shutil.copyfile(FIXTURES / "contract_clean.yaml", contract)
    recording = tmp_path / "recording"
    shutil.copytree(FIXTURES / "bags" / "clean", recording)
    original = api.preflight(contract, recording)
    output = json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    manifest = {
        "format_version": 1,
        "recording_sha256": load_recording(recording).sha256,
        "sample_row_count": len((recording / "samples.jsonl").read_text().splitlines()),
        "contract_sha256": _sha(contract.read_bytes()),
        "firmware_sha256": "1" * 64,
        "model_sha256": "2" * 64,
        "input_sha256": "3" * 64,
        "output_sha256": _sha(output),
        "selected_target": dict(SELECTED_TARGET),
    }
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    manifest_file = evidence_root / "saved-test.json"
    manifest_file.write_text(json.dumps(manifest, sort_keys=True))
    ref = ArtifactRef(path=manifest_file.name, sha256=_sha(manifest_file.read_bytes()))
    return contract, recording, evidence_root, manifest, ref, SelectedTargetRunner(SELECTED_TARGET)


def test_caller_pinned_saved_test_replays_through_public_preflight(tmp_path):
    contract, recording, root, _manifest, ref, target = _saved_test(tmp_path)
    assert api.preflight(
        contract, recording, saved_manifest=ref, evidence_root=root, selected_target_runner=target
    ) == api.preflight(contract, recording)
    assert target.calls == [("cat", "/var/lockwire/selected-target.json")]


def test_saved_test_replays_against_declared_owned_container_target(tmp_path):
    """E3.1: public replay reads build/pin identity from the container it owns."""
    try:
        ready = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"LIVE_PATH_UNTESTED: docker is unavailable: {error}")
    if ready.returncode != 0:
        detail = (ready.stderr or ready.stdout or "unknown").strip()
        pytest.skip(f"LIVE_PATH_UNTESTED: docker info exited {ready.returncode}: {detail}")
    built = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            LIVE_IMAGE,
            "-f",
            str(LIVE_DOCKERFILE),
            str(LIVE_DOCKERFILE.parent),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert built.returncode == 0, built.stderr
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--network", "none", LIVE_IMAGE, "sleep", "60"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    container_id = started.stdout.strip()
    try:
        network = subprocess.run(
            ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", container_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert network.returncode == 0, network.stderr
        assert network.stdout.strip() == "none", network.stdout
        contract, recording, root, manifest, ref, _target = _saved_test(tmp_path)
        target = SelectedTargetRunner.from_owned_container(container_id)
        assert api.preflight(
            contract,
            recording,
            saved_manifest=ref,
            evidence_root=root,
            selected_target_runner=target,
        ) == api.preflight(contract, recording)
        assert target.calls == [("cat", "/var/lockwire/selected-target.json")]
        manifest["selected_target"]["pin"] = "other-board-pin"
        manifest_path = root / ref.path
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        altered_ref = ArtifactRef(path=ref.path, sha256=_sha(manifest_path.read_bytes()))
        with pytest.raises(ValueError, match="differs from the selected target identity"):
            api.preflight(
                contract,
                recording,
                saved_manifest=altered_ref,
                evidence_root=root,
                selected_target_runner=target,
            )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    absent = subprocess.run(
        ["docker", "inspect", container_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert absent.returncode != 0, "owned selected-target container survived cleanup"


@pytest.mark.parametrize(
    "alteration", ["manifest_bytes", "sample_bytes", "sample_order", "sample_count"]
)
def test_saved_test_refuses_altered_recording_bytes_order_or_count(tmp_path, alteration):
    contract, recording, root, _manifest, ref, target = _saved_test(tmp_path)
    if alteration == "manifest_bytes":
        with (recording / "manifest.yaml").open("a") as handle:
            handle.write("\n")
    else:
        sample_file = recording / "samples.jsonl"
        rows = sample_file.read_text().splitlines(keepends=True)
        if alteration == "sample_bytes":
            rows[0] = rows[0].replace("0.057628603", "0.157628603")
        elif alteration == "sample_order":
            rows[0], rows[1] = rows[1], rows[0]  # different devices, still parseable
        else:
            rows.pop()
        sample_file.write_text("".join(rows))
    with pytest.raises(ValueError, match="recording bytes or order differ"):
        api.preflight(
            contract,
            recording,
            saved_manifest=ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_requires_external_manifest_identity_and_exact_count(tmp_path):
    contract, recording, root, manifest, ref, target = _saved_test(tmp_path)
    path = root / ref.path
    manifest["selected_target"]["signed_slot"] = "signed-slot-B-2"
    path.write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(ValueError, match="sha256 does not match"):
        api.preflight(
            contract,
            recording,
            saved_manifest=ref,
            evidence_root=root,
            selected_target_runner=target,
        )
    manifest["selected_target"]["signed_slot"] = SELECTED_TARGET["signed_slot"]
    manifest["sample_row_count"] += 1
    path.write_text(json.dumps(manifest, sort_keys=True))
    updated_ref = ArtifactRef(path=ref.path, sha256=_sha(path.read_bytes()))
    with pytest.raises(ValueError, match="sample row count differs"):
        api.preflight(
            contract,
            recording,
            saved_manifest=updated_ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_refuses_changed_contract_and_output_identity(tmp_path):
    contract, recording, root, manifest, ref, target = _saved_test(tmp_path)
    with contract.open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="contract identity differs"):
        api.preflight(
            contract,
            recording,
            saved_manifest=ref,
            evidence_root=root,
            selected_target_runner=target,
        )
    shutil.copyfile(FIXTURES / "contract_clean.yaml", contract)
    manifest["output_sha256"] = "f" * 64
    path = root / ref.path
    path.write_text(json.dumps(manifest, sort_keys=True))
    changed_ref = ArtifactRef(path=ref.path, sha256=_sha(path.read_bytes()))
    with pytest.raises(ValueError, match="output differs"):
        api.preflight(
            contract,
            recording,
            saved_manifest=changed_ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_requires_every_declared_target_identity(tmp_path):
    contract, recording, root, manifest, ref, target = _saved_test(tmp_path)
    del manifest["selected_target"]["partition"]
    path = root / ref.path
    path.write_text(json.dumps(manifest, sort_keys=True))
    changed_ref = ArtifactRef(path=ref.path, sha256=_sha(path.read_bytes()))
    with pytest.raises(ValueError, match="selected-target identities"):
        api.preflight(
            contract,
            recording,
            saved_manifest=changed_ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_refuses_manifest_target_that_differs_from_selected_target(tmp_path):
    contract, recording, root, manifest, _ref, target = _saved_test(tmp_path)
    manifest["selected_target"]["signed_slot"] = "signed-slot-B-2"
    path = root / "saved-test.json"
    path.write_text(json.dumps(manifest, sort_keys=True))
    ref = ArtifactRef(path=path.name, sha256=_sha(path.read_bytes()))

    with pytest.raises(ValueError, match="differs from the selected target identity"):
        api.preflight(
            contract,
            recording,
            saved_manifest=ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_refuses_without_a_selected_target_runner(tmp_path):
    contract, recording, root, _manifest, ref, _target = _saved_test(tmp_path)

    with pytest.raises(ValueError, match="needs a selected target runner"):
        api.preflight(contract, recording, saved_manifest=ref, evidence_root=root)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(7, stderr="target unavailable"), "could not be read"),
        (CommandResult(0, "not JSON"), "must be UTF-8 JSON"),
        (CommandResult(0, json.dumps({"build": "only-one-field"})), "must declare every"),
    ],
)
def test_saved_test_refuses_untrusted_selected_target_before_replay(
    tmp_path, monkeypatch, result, message
):
    contract, recording, root, _manifest, ref, target = _saved_test(tmp_path)
    target.result = result

    def replay_must_not_run(*_args):
        pytest.fail("selected-target refusal reached replay")

    monkeypatch.setattr(api, "run_preflight", replay_must_not_run)
    with pytest.raises(ValueError, match=message):
        api.preflight(
            contract,
            recording,
            saved_manifest=ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_refuses_contract_changed_during_parse(tmp_path, monkeypatch):
    contract, recording, root, manifest, ref, target = _saved_test(tmp_path)
    manifest["contract_sha256"] = _sha(contract.read_bytes() + b"\n")
    manifest_path = root / ref.path
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    changed_ref = ArtifactRef(path=ref.path, sha256=_sha(manifest_path.read_bytes()))
    original_parse = api.load_contract_bytes

    def change_after_parse(raw):
        parsed = original_parse(raw)
        with contract.open("ab") as handle:
            handle.write(b"\n")
        return parsed

    monkeypatch.setattr(api, "load_contract_bytes", change_after_parse)
    with pytest.raises(ValueError, match="contract changed while it was parsed"):
        api.preflight(
            contract,
            recording,
            saved_manifest=changed_ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_refuses_contract_changed_during_evaluation(tmp_path, monkeypatch):
    contract, recording, root, _manifest, ref, target = _saved_test(tmp_path)
    original_run = api.run_preflight

    def change_after_evaluation(*args, **kwargs):
        result = original_run(*args, **kwargs)
        with contract.open("ab") as handle:
            handle.write(b"\n")
        return result

    monkeypatch.setattr(api, "run_preflight", change_after_evaluation)
    with pytest.raises(ValueError, match="contract changed while it was evaluated"):
        api.preflight(
            contract,
            recording,
            saved_manifest=ref,
            evidence_root=root,
            selected_target_runner=target,
        )


def test_saved_test_parses_pinned_bytes_through_transient_path_swap(tmp_path, monkeypatch):
    contract, recording, root, manifest, ref, target = _saved_test(tmp_path)
    selected = contract.read_bytes()
    alternate = selected.replace(b"name: testbot", b"name: otherbot", 1)
    assert alternate != selected
    contract.write_bytes(alternate)
    alternate_output = json.dumps(
        api.preflight(contract, recording), sort_keys=True, separators=(",", ":")
    ).encode()
    contract.write_bytes(selected)
    manifest["output_sha256"] = _sha(alternate_output)
    manifest_path = root / ref.path
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    changed_ref = ArtifactRef(path=ref.path, sha256=_sha(manifest_path.read_bytes()))
    original_parse = api.load_contract_bytes

    def swap_for_parse(raw):
        contract.write_bytes(alternate)
        try:
            return original_parse(raw)
        finally:
            contract.write_bytes(selected)

    monkeypatch.setattr(api, "load_contract_bytes", swap_for_parse)
    with pytest.raises(ValueError, match="output differs"):
        api.preflight(
            contract,
            recording,
            saved_manifest=changed_ref,
            evidence_root=root,
            selected_target_runner=target,
        )
    assert contract.read_bytes() == selected


def test_saved_test_refuses_oversized_contract_before_read(tmp_path, monkeypatch):
    contract, recording, root, _manifest, ref, _target = _saved_test(tmp_path)
    with contract.open("ab") as handle:
        handle.truncate(MAX_CONTRACT_BYTES + 1)
    original_read = Path.read_bytes

    def assert_contract_not_read(path):
        if path == contract:
            raise AssertionError("oversized contract was read before size refusal")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", assert_contract_not_read)
    with pytest.raises(ValueError, match="contract file exceeds size limit"):
        api.preflight(contract, recording, saved_manifest=ref, evidence_root=root)
