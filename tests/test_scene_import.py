"""Tests for `lockwire scene import`: undeclared placements from the
model into an apply-ready change file, one provenance line per device."""

import threading
from pathlib import Path
import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner
from lockwire import edit, scene_import
from lockwire.cli import app
from lockwire.scene import Scene, ScenePart, load_scene
from lockwire.scene_import import SceneImportRefusal, build_import
from lockwire.schema import load_contract
from conftest import plain


X500_CONTRACT = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"
X500_GLB = Path(__file__).parent / "fixtures" / "x500" / "x500.glb"


def _part(lw_id: str, translation: tuple[float, float, float]) -> ScenePart:
    return ScenePart(
        node=lw_id,
        extras_id=lw_id,
        name_chain=(lw_id,),
        positions=(),
        normals=None,
        indices=(),
        color=(0.8, 0.8, 0.8, 1.0),
        translation=translation,
    )


def _scene(*parts: ScenePart) -> Scene:
    return Scene(
        parts=tuple(parts),
        up="z",
        nodes_with_ids={p.extras_id: p.node for p in parts if p.extras_id},
        source="synthetic.glb",
        bounds=((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        triangles=0,
        vertices=0,
    )


@pytest.fixture
def x500_contract():
    return load_contract(X500_CONTRACT)


@pytest.fixture
def x500_scene():
    return load_scene(X500_GLB)


# --------------------------------------------------------------------------- X500 bulk import


def test_x500_import_covers_every_device_with_its_own_source(x500_contract, x500_scene):
    result = build_import(x500_contract, x500_scene, witness="human", source_person="Shivansh")
    device_ids = {d.id for d in x500_contract.devices}
    rows_with_entries = [r for r in result.rows if r.action == "written"]
    assert {r.device_id for r in rows_with_entries} == device_ids
    assert len(result.entries) >= len(device_ids)  # at least one entry per device
    sources_by_device: dict[str, set[str]] = {}
    for entry in result.entries:
        device_id = entry["path"].split("/")[1]
        sources_by_device.setdefault(device_id, set()).add(entry["source"])
    assert len(sources_by_device) == len(device_ids)
    all_sources = {next(iter(s)) for s in sources_by_device.values()}
    assert len(all_sources) == len(device_ids), "each device must get its own source string"
    for entry in result.entries:
        assert set(entry.keys()) <= {"path", "value", "unit", "source", "witness", "note"}
        assert {"path", "value", "source", "witness", "note"} <= set(entry.keys())
        assert "Shivansh" in entry["source"]
        assert entry["witness"] == "human"
    # every mount_xyz value is finite and inside the scene bounds
    lo, hi = x500_scene.bounds
    for entry in result.entries:
        if entry["path"].endswith("/mount_xyz"):
            assert entry["unit"] == "m"
            for v, lo_c, hi_c in zip(entry["value"], lo, hi, strict=True):
                assert lo_c - 1e-6 <= v <= hi_c + 1e-6


def test_x500_import_camera_has_no_declared_mount_frame_so_gets_two_entries(
    x500_contract, x500_scene
):
    result = build_import(
        x500_contract,
        x500_scene,
        witness="human",
        source_person="Shivansh",
        only=["dev.camera.oak-d-lite"],
    )
    assert len(result.entries) == 2
    paths = {e["path"] for e in result.entries}
    assert paths == {
        "devices/dev.camera.oak-d-lite/physical/mount_xyz",
        "devices/dev.camera.oak-d-lite/physical/mount_frame",
    }
    frame_entry = next(e for e in result.entries if e["path"].endswith("mount_frame"))
    assert frame_entry["value"] == "x500_frame"
    assert "unit" not in frame_entry
    xyz_entry = next(e for e in result.entries if e["path"].endswith("mount_xyz"))
    assert xyz_entry["source"] == ("x500.glb node dev.camera.oak-d-lite; confirmed by Shivansh"), (
        "source names the contract id (what the contract knows), not the mesh node name"
    )


def test_x500_import_motor_already_has_mount_frame_so_gets_one_entry(x500_contract, x500_scene):
    result = build_import(
        x500_contract,
        x500_scene,
        witness="human",
        source_person="Shivansh",
        only=["dev.motor.m1"],
    )
    assert len(result.entries) == 1
    assert result.entries[0]["path"] == "devices/dev.motor.m1/physical/mount_xyz"


def test_x500_import_only_filters_to_named_devices(x500_contract, x500_scene):
    result = build_import(
        x500_contract,
        x500_scene,
        witness="human",
        source_person="Shivansh",
        only=["dev.camera.oak-d-lite"],
    )
    assert {r.device_id for r in result.rows} == {"dev.camera.oak-d-lite"}


def test_x500_import_skips_non_device_parts(x500_contract, x500_scene):
    result = build_import(x500_contract, x500_scene, witness="human", source_person="Shivansh")
    device_ids = {d.id for d in x500_contract.devices}
    assert result.skipped_non_devices
    assert device_ids.isdisjoint(result.skipped_non_devices)


# --------------------------------------------------------------------------- declared placements


def test_declared_device_within_tolerance_is_not_touched(clean_contract):
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.02)))
    result = build_import(clean_contract, scene, witness="human", source_person="Shivansh")
    row = next(r for r in result.rows if r.device_id == "dev.imu")
    assert row.action == "agrees, not touched"
    assert not any(e["path"].startswith("devices/dev.imu/") for e in result.entries)


def test_declared_device_past_tolerance_default_run_lists_and_does_not_write(clean_contract):
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.026)))
    result = build_import(clean_contract, scene, witness="human", source_person="Shivansh")
    row = next(r for r in result.rows if r.device_id == "dev.imu")
    assert row.action == "differs by 6.0 mm, not touched"
    assert not any(e["path"].startswith("devices/dev.imu/") for e in result.entries)


def test_declared_too_writes_the_correction(clean_contract):
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.026)))
    result = build_import(
        clean_contract, scene, witness="human", source_person="Shivansh", declared_too=True
    )
    row = next(r for r in result.rows if r.device_id == "dev.imu")
    assert "written" in row.action
    entry = next(e for e in result.entries if e["path"] == "devices/dev.imu/physical/mount_xyz")
    assert entry["unit"] == "m"


# --------------------------------------------------------------------------- refusals


def test_no_device_parts_refuses(clean_contract):
    scene = _scene(_part("bus.usb0", (0.0, 0.0, 0.0)))
    with pytest.raises(SceneImportRefusal):
        build_import(clean_contract, scene, witness="human", source_person="Shivansh")


def test_anymal_d_model_has_single_root_for_import(clean_contract):
    """Before the fix, `parse_urdf` recorded zero joints, so every
    link looked like a root and import refused with 'no single root link'. With
    joints recorded, ANYmal resolves to exactly one root (`base`)."""
    from lockwire.urdf import parse_urdf

    from conftest import FIXTURES

    clean_contract.model = parse_urdf(FIXTURES / "anymal_d.urdf")
    scene = _scene(_part("base", (0.0, 0.0, 0.0)))
    result = build_import(
        clean_contract, scene, witness="human", source_person="Shivansh", link_based=True
    )
    assert result.root == "base"


def test_link_based_no_join_explains_instead_of_refusing(clean_contract):
    """A URDF/SDF's parts are links; a fresh model whose links don't match any
    device id must not be a tool failure — it explains and hands
    back zero entries."""
    scene = _scene(
        _part("base_link", (0.0, 0.0, 0.0)),
        _part("rotor_0", (0.1, 0.1, 0.0)),
    )
    result = build_import(
        clean_contract, scene, witness="human", source_person="Shivansh", link_based=True
    )
    assert result.entries == ()
    assert result.explanation is not None
    assert "base_link, rotor_0" in result.explanation
    assert "no device is called that" in result.explanation
    assert "extras.lockwire.id" in result.explanation


# --------------------------------------------------------------------------- written file shape


@pytest.mark.parametrize("bad_witness", ["you", "", "  ", "Claude"])
def test_write_change_file_refuses_bad_witness_direct_library_call(
    tmp_path, x500_contract, x500_scene, bad_witness
):
    """The choke point, not the CLI: build_import + write_change_file called directly
    (no CliRunner, no cli.py) with a refused witness must raise and write nothing —
    this is exactly the path that bypassed the guard in the earlier pass."""
    from lockwire.edit import EditRefusal
    from lockwire.scene_import import write_change_file

    result = build_import(x500_contract, x500_scene, witness=bad_witness, source_person="Shivansh")
    out = tmp_path / "changes.yaml"
    with pytest.raises(EditRefusal, match="--witness must name a real person"):
        write_change_file(result.entries, out, scene_source=x500_scene.source)
    assert not out.exists()


def test_written_change_file_parses_and_has_exact_keys(tmp_path, x500_contract, x500_scene):
    from lockwire.scene_import import write_change_file

    result = build_import(x500_contract, x500_scene, witness="human", source_person="Shivansh")
    out = tmp_path / "changes.yaml"
    write_change_file(result.entries, out, scene_source=x500_scene.source)
    data = YAML(typ="safe").load(out.read_text())
    assert isinstance(data, list) and len(data) == len(result.entries)
    for entry in data:
        assert set(entry.keys()) <= {"path", "value", "unit", "source", "witness", "note"}
        assert {"path", "value", "source", "witness", "note"} <= set(entry.keys())


def test_write_change_file_failure_preserves_existing_output_and_backup(
    tmp_path, x500_contract, x500_scene, monkeypatch
):
    """A failed staged dump cannot truncate either the change file or its undo baseline."""
    from lockwire.scene_import import write_change_file

    result = build_import(x500_contract, x500_scene, witness="human", source_person="Shivansh")
    out = tmp_path / "changes.yaml"
    original = b"# existing change file\n- path: old\n"
    out.write_bytes(original)

    write_change_file(result.entries, out, scene_source=x500_scene.source)
    baseline = out.read_bytes()
    backup = out.with_name(out.name + ".bak")
    assert backup.read_bytes() == original

    def fail_dump(self, data, stream, *args, **kwargs):
        raise OSError("injected staged dump failure")

    monkeypatch.setattr(scene_import.YAML, "dump", fail_dump)
    with pytest.raises(OSError, match="injected staged dump failure"):
        write_change_file(result.entries, out, scene_source=x500_scene.source)

    assert out.read_bytes() == baseline
    assert backup.read_bytes() == original


def test_write_change_file_replace_and_backup_rollback_retain_undo_recovery(
    tmp_path, x500_contract, x500_scene, monkeypatch
):
    """The public scene-import writer retains the old undo bytes if rollback itself fails."""
    from lockwire.scene_import import write_change_file

    result = build_import(x500_contract, x500_scene, witness="human", source_person="Shivansh")
    out = tmp_path / "changes.yaml"
    prior_undo = b"# prior undo baseline\n- path: old\n"
    out.write_bytes(prior_undo)
    write_change_file(result.entries, out, scene_source=x500_scene.source)
    current = out.read_bytes()
    backup = out.with_name(out.name + ".bak")
    assert backup.read_bytes() == prior_undo

    real_replace = edit.os.replace
    backup_replacements = 0

    def fail_output_and_backup_rollback(source, destination):
        nonlocal backup_replacements
        if Path(destination) == backup:
            backup_replacements += 1
            if backup_replacements == 2:
                raise OSError("backup rollback failed")
        if Path(destination) == out:
            raise OSError("output replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr(edit.os, "replace", fail_output_and_backup_rollback)
    with pytest.raises(edit.EditRecoveryError, match="prior undo bytes are at") as caught:
        write_change_file(result.entries, out, scene_source=x500_scene.source)

    assert backup_replacements == 2
    assert out.read_bytes() == current
    assert backup.read_bytes() == current
    assert caught.value.recovery_artifact.read_bytes() == prior_undo


def test_scene_import_write_race_keeps_draft_and_undo_bytes(tmp_path, monkeypatch):
    from lockwire import cli

    contract = tmp_path / "contract.yaml"
    contract.write_bytes(X500_CONTRACT.read_bytes())
    opened = edit.contract_revision(contract)
    out = tmp_path / "changes.yaml"
    draft_before = b"# retained draft\n- path: old\n"
    undo_before = b"# retained undo\n- path: older\n"
    out.write_bytes(draft_before)
    backup = out.with_name(out.name + ".bak")
    backup.write_bytes(undo_before)
    built = threading.Event()
    resume = threading.Event()
    original_build = cli.build_import

    def paused_build(*args, **kwargs):
        result = original_build(*args, **kwargs)
        built.set()
        assert resume.wait(5)
        return result

    monkeypatch.setattr(cli, "build_import", paused_build)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            CliRunner().invoke(
                app,
                [
                    "scene",
                    "import",
                    str(X500_GLB),
                    "--contract",
                    str(contract),
                    "--witness",
                    "human",
                    "--source-person",
                    "Shivansh",
                    "--out",
                    str(out),
                    "--write",
                    "--expected-revision",
                    opened,
                ],
            )
        )
    )
    worker.start()
    try:
        assert built.wait(5)
        edit.apply_changes(
            contract,
            [edit.Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")],
            expected_revision=opened,
        )
        concurrent_bytes = contract.read_bytes()
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert outcomes[0].exit_code == 2, outcomes[0].output
    assert "reload before applying" in plain(outcomes[0].output)
    assert contract.read_bytes() == concurrent_bytes
    assert out.read_bytes() == draft_before
    assert backup.read_bytes() == undo_before


def test_scene_import_write_contract_failure_restores_draft_and_undo(tmp_path, monkeypatch):
    contract = tmp_path / "contract.yaml"
    contract.write_bytes(X500_CONTRACT.read_bytes())
    contract_before = contract.read_bytes()
    out = tmp_path / "changes.yaml"
    draft_before = b"# retained draft\n- path: old\n"
    undo_before = b"# retained undo\n- path: older\n"
    out.write_bytes(draft_before)
    backup = out.with_name(out.name + ".bak")
    backup.write_bytes(undo_before)
    real_replace = edit.os.replace

    def fail_contract_replace(source, destination):
        if Path(destination) == contract:
            raise OSError("contract replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr(edit.os, "replace", fail_contract_replace)
    result = CliRunner().invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(contract),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
            "--write",
            "--expected-revision",
            edit.contract_revision(contract),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "contract replace failed" in plain(result.output)
    assert contract.read_bytes() == contract_before
    assert not contract.with_name(contract.name + ".bak").exists()
    assert out.read_bytes() == draft_before
    assert backup.read_bytes() == undo_before


def test_scene_import_write_rollback_failure_retains_prior_draft_bytes(
    tmp_path, x500_contract, x500_scene, monkeypatch
):
    contract = tmp_path / "contract.yaml"
    contract.write_bytes(X500_CONTRACT.read_bytes())
    original_contract = contract.read_bytes()
    result = build_import(x500_contract, x500_scene, witness="human", source_person="Shivansh")
    out = tmp_path / "changes.yaml"
    original_draft = b"# retained draft\n- path: old\n"
    original_undo = b"# retained undo\n- path: older\n"
    out.write_bytes(original_draft)
    backup = out.with_name(out.name + ".bak")
    backup.write_bytes(original_undo)
    real_replace = edit.os.replace
    draft_replacements = 0

    def fail_contract_and_draft_restore(source, destination):
        nonlocal draft_replacements
        if Path(destination) == contract:
            raise OSError("contract replace failed")
        if Path(destination) == out:
            draft_replacements += 1
            if draft_replacements == 2:
                raise OSError("draft rollback failed")
        return real_replace(source, destination)

    monkeypatch.setattr(edit.os, "replace", fail_contract_and_draft_restore)
    with pytest.raises(OSError, match="recovery bytes at"):
        scene_import.write_and_apply_import(
            contract,
            result.entries,
            out,
            scene_source=x500_scene.source,
            expected_revision=edit.contract_revision(contract),
        )
    retained = list(tmp_path.glob(".changes.yaml.*.tmp"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == original_draft
    assert backup.read_bytes() == original_undo
    assert contract.read_bytes() == original_contract
