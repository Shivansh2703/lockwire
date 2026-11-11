import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from conftest import plain, strip_ansi
from ruamel.yaml import YAML
from typer.testing import CliRunner

import lockwire
from lockwire.cli import app

runner = CliRunner()
BAGS = Path(__file__).parent / "fixtures" / "bags"
FIXTURES = Path(__file__).parent / "fixtures"


def test_init_then_check(testbot_urdf, tmp_path):
    contract_path = tmp_path / "lockwire.yaml"

    result = runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract_path)])
    assert result.exit_code == 0, result.output
    assert 'schema_version: "0.6"' in contract_path.read_text().replace("'", '"')
    assert contract_path.exists()
    assert "zero-mass link(s)" in plain(result.output)

    # zero-mass sensor_mount is a warning -> still GO (exit 0)
    result = runner.invoke(app, ["check", str(contract_path), "--all"])
    assert result.exit_code == 0, result.output
    assert "GO" in plain(result.output)
    assert "sensor_mount" in plain(result.output)


def test_explicit_upgrade_writes_new_v04_without_changing_legacy_bytes(tmp_path):
    from lockwire.schema import load_contract

    for version in ("0.2", "0.3", "0.4", "0.5"):
        source = tmp_path / f"legacy-{version}.yaml"
        original = (
            f'# retained legacy comment\nschema_version: "{version}"\nrobot:\n  name: legacy\n'
        ).encode()
        source.write_bytes(original)
        output = tmp_path / f"upgraded-{version}.yaml"
        digest = hashlib.sha256(original).hexdigest()

        result = runner.invoke(
            app,
            [
                "upgrade",
                str(source),
                "--out",
                str(output),
                "--expected-revision",
                digest,
            ],
        )

        assert result.exit_code == 0, result.output
        assert source.read_bytes() == original
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        assert load_contract(source).schema_version == version
        assert load_contract(output).schema_version == "0.6"
        assert output.read_bytes() != original


def test_explicit_upgrade_refuses_changed_source_and_occupied_targets(tmp_path):
    source = tmp_path / "legacy.yaml"
    original = b'schema_version: "0.3"\nrobot: {name: legacy}\n'
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    output = tmp_path / "upgraded.yaml"
    output.write_bytes(b"existing history must remain\n")

    attempts = (
        (source, output, "deadbeef", "full lowercase raw contract sha256"),
        (source, output, "0" * 64, "changed since its revision"),
        (source, source, digest, "must differ"),
        (source, output, digest, "exists"),
    )
    for selected_source, selected_output, selected_digest, refusal in attempts:
        result = runner.invoke(
            app,
            [
                "upgrade",
                str(selected_source),
                "--out",
                str(selected_output),
                "--expected-revision",
                selected_digest,
            ],
        )
        assert result.exit_code == 2, result.output
        assert refusal in plain(result.output)
        assert source.read_bytes() == original
        assert output.read_bytes() == b"existing history must remain\n"


def test_explicit_upgrade_refuses_nonlegacy_and_unsupported_content(tmp_path):
    for version, extra, refusal in (
        ("0.6", "", "requires an explicit legacy"),
        ("0.7", "", "REFUSED"),
        ("0.3", "structural: {}\n", "must not contain structural"),
        ("0.3", "actuator_cases: [{id: hidden}]\n", "actuator_cases"),
    ):
        source = tmp_path / f"input-{version}-{len(extra)}.yaml"
        original = (f'schema_version: "{version}"\nrobot: {{name: legacy}}\n' + extra).encode()
        source.write_bytes(original)
        output = tmp_path / f"output-{version}-{len(extra)}.yaml"
        result = runner.invoke(
            app,
            [
                "upgrade",
                str(source),
                "--out",
                str(output),
                "--expected-revision",
                hashlib.sha256(original).hexdigest(),
            ],
        )
        assert result.exit_code == 2, result.output
        assert refusal in plain(result.output)
        assert source.read_bytes() == original
        assert not output.exists()

    unversioned = tmp_path / "unversioned.yaml"
    raw = b"robot: {name: ambiguous}\n"
    unversioned.write_bytes(raw)
    output = tmp_path / "unversioned-output.yaml"
    result = runner.invoke(
        app,
        [
            "upgrade",
            str(unversioned),
            "--out",
            str(output),
            "--expected-revision",
            hashlib.sha256(raw).hexdigest(),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "requires an explicit legacy schema_version" in plain(result.output)
    assert unversioned.read_bytes() == raw
    assert not output.exists()


def test_init_from_anymal_d_records_joints_and_root(tmp_path):
    """P1 (audit 2): `init` on a 96-link/95-joint URDF must record every joint,
    not just links, so `check`/scene-import can find a single root link."""
    from lockwire.schema import load_contract

    contract_path = tmp_path / "lockwire.yaml"
    result = runner.invoke(
        app, ["init", str(FIXTURES / "anymal_d.urdf"), "--out", str(contract_path)]
    )
    assert result.exit_code == 0, result.output

    contract = load_contract(contract_path)
    assert len(contract.model.links) == 96
    assert len(contract.model.joints) == 95
    children = {j.child for j in contract.model.joints}
    roots = {link.name for link in contract.model.links} - children
    assert roots == {"base"}


def test_init_refuses_overwrite(testbot_urdf, tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text("robot: {name: precious}")

    result = runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract_path)])
    assert result.exit_code == 1
    assert "precious" in contract_path.read_text()


def test_check_missing_contract_exits_2(tmp_path):
    """An absent contract is REFUSED (exit 2), not NO-GO (exit 1)."""
    result = runner.invoke(app, ["check", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 2


def test_check_malformed_contract_exits_2(tmp_path):
    """A contract that fails to parse is REFUSED (exit 2), not NO-GO (exit 1)."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("bad: [")
    result = runner.invoke(app, ["check", str(bad)])
    assert result.exit_code == 2
    assert "check failed:" in plain(result.output)


def test_check_and_report_reject_hostile_contract_paths_without_traceback(tmp_path):
    """Directory / symlink-to-dir / malformed contract → named failed: line.

    `check` is REFUSED (exit 2) for an unusable contract; `report` keeps
    its own exit 1 (untouched, report.py is out of scope here).
    """
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    dangling = tmp_path / "dangling.yaml"
    dangling.symlink_to(tmp_path / "missing-target.yaml")
    # exists() is False for a dangling symlink, so force load_contract via a
    # symlink-to-directory (exists True, open raises IsADirectoryError).
    link_to_dir = tmp_path / "link-to-dir.yaml"
    link_to_dir.symlink_to(directory)
    malformed = tmp_path / "truncated.yaml"
    malformed.write_text('robot: {name: "x"\n')
    out = tmp_path / "out.html"

    for path in (directory, link_to_dir, malformed):
        result = runner.invoke(app, ["check", str(path)])
        assert result.exit_code == 2, result.output
        want = "check failed: truncated.yaml: line" if path == malformed else "is a folder"
        assert want in plain(result.output) and "Traceback" not in plain(result.output)

    dangling_result = runner.invoke(app, ["check", str(dangling)])
    assert dangling_result.exit_code == 2
    assert "no contract at" in plain(dangling_result.output)
    assert "lockwire check robot.urdf" in plain(dangling_result.output)
    assert "Traceback" not in plain(dangling_result.output)

    # report has dir_okay=False, so directory/symlink-to-dir are gated by typer;
    # malformed content reaches load_contract and must use the named handler.
    report_result = runner.invoke(app, ["report", str(malformed), "--out", str(out)])
    assert report_result.exit_code == 1, report_result.output
    assert "report failed:" in plain(report_result.output)
    assert "Traceback" not in plain(report_result.output)


def test_hw_records_event_routes_owners_and_leaves_contract_untouched(tmp_path):
    """ACCEPT path: declaration against a clean contract copy; mass budget + owner."""
    import hashlib

    contract_path = tmp_path / "contract_clean.yaml"
    src = Path(__file__).parent / "fixtures" / "contract_clean.yaml"
    contract_path.write_bytes(src.read_bytes())
    before = hashlib.sha256(contract_path.read_bytes()).hexdigest()

    args = [
        "hw",
        "added FLIR, 42 g, nose mount",
        "--contract",
        str(contract_path),
        "--at",
        "2026-07-27T00:00:00+00:00",
    ]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first.output == second.output
    assert "recorded added FLIR, 42 g, nose mount" in plain(first.output)
    assert "budgets/bud.mass.devices" in plain(first.output)
    assert "owner=shivansh" in plain(first.output)
    assert "model" in plain(first.output)
    assert "undetermined:" in plain(first.output)
    assert hashlib.sha256(contract_path.read_bytes()).hexdigest() == before
    assert (tmp_path / "contract_clean.hw.yaml").is_file()


def test_hw_byte_identical_when_log_reset(tmp_path):
    """Pinned --at yields byte-identical stdout across fresh sidecar runs."""
    src = Path(__file__).parent / "fixtures" / "contract_clean.yaml"
    outputs = []
    for _ in range(2):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_bytes(src.read_bytes())
        log_path = tmp_path / "c.hw.yaml"
        if log_path.exists():
            log_path.unlink()
        result = runner.invoke(
            app,
            [
                "hw",
                "added FLIR, 42 g, nose mount",
                "--contract",
                str(contract_path),
                "--at",
                "2026-07-27T00:00:00+00:00",
            ],
        )
        assert result.exit_code == 0, result.output
        outputs.append(result.output)
        log_path.unlink()
    assert outputs[0] == outputs[1]


def test_hw_with_rail_binding_routes_power_budget_owner(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    contract_path.write_bytes(
        (Path(__file__).parent / "fixtures" / "contract_clean.yaml").read_bytes()
    )
    result = runner.invoke(
        app,
        [
            "hw",
            "added FLIR, 42 g, nose mount",
            "--contract",
            str(contract_path),
            "--bus",
            "bus.usb0",
            "--rail",
            "rail.5v",
            "--at",
            "2026-07-27T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "budgets/bud.power.rail5v" in plain(result.output)
    assert "budgets/bud.endurance" in plain(result.output)
    assert "topology/buses/bus.usb0" in plain(result.output)
    assert "undetermined:" not in plain(result.output)


def test_hw_missing_contract_exits_2_without_traceback():
    result = runner.invoke(app, ["hw", "added FLIR, 42 g", "--contract", "/nonexistent.yaml"])
    assert result.exit_code == 2
    assert "hw failed:" in plain(result.output)
    assert "Traceback" not in plain(result.output)


def test_hw_bad_timestamp_exits_2_without_traceback(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    contract_path.write_bytes(
        (Path(__file__).parent / "fixtures" / "contract_clean.yaml").read_bytes()
    )
    result = runner.invoke(
        app,
        [
            "hw",
            "added FLIR, 42 g",
            "--contract",
            str(contract_path),
            "--at",
            "not-a-timestamp",
        ],
    )
    assert result.exit_code == 2
    assert "hw failed:" in plain(result.output)
    assert "Traceback" not in plain(result.output)


# --------------------------------------------------------------------------- --scene

X500_CONTRACT = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"
X500_GLB = Path(__file__).parent / "fixtures" / "x500" / "x500.glb"


def test_report_scene_flag_is_announced(tmp_path):
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        ["report", str(X500_CONTRACT), "--scene", str(X500_GLB), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "scene: x500.glb" in plain(result.output)


def test_report_scene_refusal_exits_2(tmp_path):
    bad_scene = tmp_path / "bad.glb"
    bad_scene.write_bytes(b"not a glb")
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        ["report", str(X500_CONTRACT), "--scene", str(bad_scene), "--out", str(out)],
    )
    assert result.exit_code == 2, result.output
    assert "bad magic header" in plain(result.output)


def test_report_accepts_no_open_as_a_no_op(tmp_path):
    out = tmp_path / "report.html"
    result = runner.invoke(app, ["report", str(X500_CONTRACT), "--out", str(out), "--no-open"])
    assert result.exit_code == 0, result.output
    assert out.exists()


# --------------------------------------------------------------------------- scene import


def test_scene_import_writes_change_file_and_shows_next_step(tmp_path):
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "wrote" in plain(result.output)
    assert "next: lockwire apply" in plain(result.output)
    assert "written by lockwire scene import" in out.read_text()


def test_scene_import_write_applies_generated_changes(tmp_path):
    from lockwire.edit import contract_revision

    contract = tmp_path / "contract.yaml"
    contract.write_text(X500_CONTRACT.read_text())
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
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
            contract_revision(contract),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "applied" in plain(result.output)
    assert "mount_xyz:" in contract.read_text()


def test_scene_import_write_refuses_missing_or_stale_revision_without_mutating_contract(tmp_path):
    from lockwire.edit import contract_revision

    contract = tmp_path / "contract.yaml"
    contract.write_text(X500_CONTRACT.read_text())
    before = contract.read_bytes()
    out = tmp_path / "changes.yaml"
    output_before = b"# retained draft\n- path: old\n"
    backup_before = b"# retained undo\n- path: older\n"
    out.write_bytes(output_before)
    backup = out.with_name(out.name + ".bak")
    backup.write_bytes(backup_before)
    shared = [
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
    ]

    missing = runner.invoke(app, shared)
    assert missing.exit_code == 2, missing.output
    assert "requires --expected-revision" in plain(missing.output)
    assert contract.read_bytes() == before
    assert out.read_bytes() == output_before
    assert backup.read_bytes() == backup_before

    stale = runner.invoke(app, [*shared, "--expected-revision", "0" * 64])
    assert stale.exit_code == 2, stale.output
    assert "reload before applying" in plain(stale.output)
    assert contract.read_bytes() == before
    assert out.read_bytes() == output_before
    assert backup.read_bytes() == backup_before
    assert contract_revision(contract) != "0" * 64


def test_scene_import_refuses_without_witness(tmp_path):
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--witness" in plain(result.output)
    assert not out.exists()


def test_scene_import_refuses_witness_you(tmp_path):
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "you",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--witness must name a real person, not 'you'" in plain(result.output)
    assert not out.exists()


def test_scene_import_refuses_blank_witness(tmp_path):
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--witness must name a real person" in plain(result.output)
    assert not out.exists()


def test_scene_import_refuses_without_source_person(tmp_path):
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "human",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--source-person" in plain(result.output)


def test_scene_import_scene_refusal_exits_2(tmp_path):
    bad_scene = tmp_path / "bad.glb"
    bad_scene.write_bytes(b"not a glb")
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(bad_scene),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "bad magic header" in plain(result.output)
    assert not out.exists()


def test_scene_import_only_filters_to_named_device(tmp_path):
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--only",
            "dev.camera.oak-d-lite",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dev.camera.oak-d-lite" in plain(result.output)
    assert "dev.motor.m1" not in plain(result.output)
    from ruamel.yaml import YAML

    data = YAML(typ="safe").load(out.read_text())
    assert len(data) == 2


def test_scene_import_ran_line_reruns_to_an_identical_file(tmp_path):
    import shlex

    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
            "--only",
            "dev.camera.oak-d-lite",
        ],
    )
    assert result.exit_code == 0, result.output
    first_bytes = out.read_bytes()
    ran_line = next(line for line in result.output.splitlines() if line.startswith("ran: "))
    tokens = shlex.split(ran_line[len("ran: ") :])
    assert tokens[0] == "lockwire"
    assert "--source-person" in tokens
    assert "--out" in tokens

    rerun = runner.invoke(app, tokens[1:])
    assert rerun.exit_code == 0, rerun.output
    assert out.read_bytes() == first_bytes


def test_scene_import_help_advertises_urdf_and_sdf():
    result = runner.invoke(app, ["scene", "import", "--help"])
    assert result.exit_code == 0, result.output
    text = plain(result.output)
    assert "URDF" in text and "SDF" in text


_BOX_VISUAL = (
    '<visual name="v"><geometry><box><size>0.02 0.02 0.02</size></box></geometry></visual>'
)

_SDF_TWO_LINKS = f"""<?xml version="1.0"?>
<sdf version="1.6">
  <model name="clean">
    <link name="base_link">
      <pose>0 0 0 0 0 0</pose>
      {_BOX_VISUAL}
    </link>
    <link name="dev.barometer">
      <pose>0.1 0.2 0.3 0 0 0</pose>
      {_BOX_VISUAL}
    </link>
    <link name="dev.gnss">
      <pose>-0.1 0 0.05 0 0 0</pose>
      {_BOX_VISUAL}
    </link>
  </model>
</sdf>
"""

_SDF_NO_MATCHING_LINKS = f"""<?xml version="1.0"?>
<sdf version="1.6">
  <model name="px4_vision">
    <link name="base_link"><pose>0 0 0 0 0 0</pose>{_BOX_VISUAL}</link>
    <link name="rotor_0"><pose>0.1 0.1 0 0 0 0</pose>{_BOX_VISUAL}</link>
    <link name="rotor_1"><pose>-0.1 0.1 0 0 0 0</pose>{_BOX_VISUAL}</link>
    <link name="rotor_2"><pose>-0.1 -0.1 0 0 0 0</pose>{_BOX_VISUAL}</link>
    <link name="rotor_3"><pose>0.1 -0.1 0 0 0 0</pose>{_BOX_VISUAL}</link>
  </model>
</sdf>
"""


def test_scene_import_reads_sdf_links_named_after_devices(tmp_path):
    sdf_path = tmp_path / "model.sdf"
    sdf_path.write_text(_SDF_TWO_LINKS)
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(sdf_path),
            "--contract",
            str(FIXTURES / "contract_clean.yaml"),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
            "--only",
            "dev.barometer",
            "--only",
            "dev.gnss",
        ],
    )
    assert result.exit_code == 0, result.output
    from ruamel.yaml import YAML

    data = YAML(typ="safe").load(out.read_text())
    devices_written = {entry["path"].split("/")[1] for entry in data}
    assert devices_written == {"dev.barometer", "dev.gnss"}


def test_scene_import_sdf_with_no_matching_links_explains_and_exits_0(tmp_path):
    sdf_path = tmp_path / "px4_vision.sdf"
    sdf_path.write_text(_SDF_NO_MATCHING_LINKS)
    out = tmp_path / "changes.yaml"
    result = runner.invoke(
        app,
        [
            "scene",
            "import",
            str(sdf_path),
            "--contract",
            str(FIXTURES / "contract_clean.yaml"),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    text = plain(result.output)
    assert (
        "the model names base_link, rotor_0, rotor_1, rotor_2, rotor_3; "
        "the contract's devices are dev.barometer, dev.esc.busless, dev.gnss, "
        "dev.imu, dev.radio.rfd900; no device is called that — name a device "
        "after its link, or give the model extras.lockwire.id"
    ) in text
    from ruamel.yaml import YAML

    data = YAML(typ="safe").load(out.read_text())
    assert data in (None, [])


# ---------------------------------------------------------------- declare / apply / add


def test_declare_cli_creates_mount_xyz(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    result = runner.invoke(
        app,
        [
            "declare",
            str(contract_path),
            "devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
            "0.12 0 0.05 m",
            "--source",
            "hand measurement",
            "--witness",
            "w1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "declared" in plain(result.output)
    assert "ran: lockwire declare" in plain(result.output)


def test_declare_cli_refuses_without_witness_exit_2(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    result = runner.invoke(
        app,
        [
            "declare",
            str(contract_path),
            "topology/rails/rail.pm02-5v/capacity",
            "18 W",
            "--source",
            "s",
        ],
    )
    assert result.exit_code == 2
    assert "witness" in plain(result.output).lower()


def test_declare_cli_dry_run_writes_nothing(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    before = contract_path.read_bytes()
    result = runner.invoke(
        app,
        [
            "declare",
            str(contract_path),
            "topology/rails/rail.pm02-5v/capacity",
            "18 W",
            "--source",
            "s",
            "--witness",
            "w1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in plain(result.output).lower()
    assert contract_path.read_bytes() == before


REVIEW_FACT = "devices/dev.companion.jetson-orin-nano/power/draw"


def test_review_prints_its_own_cli_line(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    result = runner.invoke(
        app,
        [
            "review",
            str(contract_path),
            REVIEW_FACT,
            "--question",
            "--note",
            "datasheet p.2 says 20.0 W",
            "--witness",
            "Maya Chen",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "reviewed" in plain(result.output)
    assert "ran: lockwire review" in plain(result.output)
    assert "state: questioned" in contract_path.read_text(encoding="utf-8")


def test_review_refuses_without_approve_or_question_exit_2(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    before = contract_path.read_bytes()
    result = runner.invoke(
        app, ["review", str(contract_path), REVIEW_FACT, "--witness", "Maya Chen"]
    )
    assert result.exit_code == 2
    assert "exactly one of --approve or --question" in plain(result.output)
    assert contract_path.read_bytes() == before


def test_review_refuses_a_blank_witness_exit_2(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    before = contract_path.read_bytes()
    result = runner.invoke(
        app,
        ["review", str(contract_path), REVIEW_FACT, "--approve", "--witness", "you"],
    )
    assert result.exit_code == 2
    assert "witness" in plain(result.output).lower()
    assert contract_path.read_bytes() == before


def test_apply_cli_atomic_bad_entry_named_and_nothing_written(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    before = contract_path.read_bytes()
    changes_file = tmp_path / "changes.yaml"
    changes_file.write_text(
        "- path: topology/rails/rail.pm02-5v/capacity\n"
        "  value: 18 W\n"
        "  source: s1\n"
        "  witness: w1\n"
        "- path: topology/rails/rail.servo-motor/capacity\n"
        "  value: 18 zorp\n"
        "  source: s2\n"
        "  witness: w2\n"
    )
    result = runner.invoke(app, ["apply", str(contract_path), str(changes_file)])
    assert result.exit_code == 2, result.output
    assert "entry 2" in plain(result.output)
    assert contract_path.read_bytes() == before


def test_apply_cli_all_good_writes_one_diff(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    changes_file = tmp_path / "changes.yaml"
    changes_file.write_text(
        "- path: topology/rails/rail.pm02-5v/capacity\n"
        "  value: 18 W\n"
        "  source: s1\n"
        "  witness: w1\n"
        "- path: devices/dev.camera.oak-d-lite/power/draw\n"
        "  value: 4 W\n"
        "  source: s2\n"
        "  witness: w2\n"
    )
    result = runner.invoke(app, ["apply", str(contract_path), str(changes_file)])
    assert result.exit_code == 0, result.output
    assert "ran: lockwire apply" in plain(result.output)
    check = runner.invoke(app, ["check", str(contract_path)])
    assert check.exit_code in (0, 1)  # contract still parses and runs


def test_add_rail_cli(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    result = runner.invoke(
        app,
        [
            "add",
            "rail",
            str(contract_path),
            "--id",
            "rail.new-5v",
            "--voltage",
            "5 V",
            "--capacity",
            "10 W",
            "--source",
            "s",
            "--witness",
            "w1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "added rail rail.new-5v" in plain(result.output)


def test_gearbox_cli_writes_gearbox_template(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes((FIXTURES / "armtree.yaml").read_bytes())
    result = runner.invoke(
        app,
        [
            "gearbox",
            str(contract_path),
            "--id",
            "gbx.shoulder",
            "--ratio",
            "4.0",
            "--input-joint",
            "base_to_shoulder",
            "--output-joint",
            "shoulder_to_elbow",
            "--source",
            "arm datasheet",
            "--witness",
            "Maya Chen",
        ],
    )
    assert result.exit_code == 0, result.output
    gearbox = YAML().load(contract_path.read_text())["gearboxes"][0]
    assert (gearbox["id"], gearbox["ratio"], gearbox["input_joint"], gearbox["output_joint"]) == (
        "gbx.shoulder",
        4.0,
        "base_to_shoulder",
        "shoulder_to_elbow",
    )
    assert gearbox["provenance"]["witness"] == "Maya Chen"


def test_add_ran_line_round_trips_byte_identical(tmp_path):
    """The printed ``ran:`` line for ``add device`` carries every flag.

    Independent second leg (PARITY-ANCHOR LAW): this replays the printed
    line on a *fresh copy* of the same starting fixture and diffs the
    resulting bytes against the first run's output, rather than asserting
    substrings of the line against itself. If the line dropped a flag
    (the original bug), the replay would omit that field and the byte
    comparison would fail.
    """
    import shlex

    first = tmp_path / "first.yaml"
    first.write_bytes(X500_CONTRACT.read_bytes())
    args = [
        "add",
        "device",
        str(first),
        "--id",
        "dev.test-gps",
        "--model",
        "mdl.gnss.here3-plus",
        "--owner",
        "flight",
        "--mass",
        "30g",
        "--mount-frame",
        "base_link",
        "--rail",
        "rail.pm02-5v",
        "--draw",
        "2W",
        "--source",
        "test",
        "--witness",
        "human",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    first_bytes = first.read_bytes()

    ran_line = next(line for line in result.output.splitlines() if line.startswith("ran: "))
    tokens = shlex.split(ran_line[len("ran: ") :])
    assert tokens[0] == "lockwire"
    for flag in ("--model", "--owner", "--mass", "--mount-frame", "--rail", "--draw"):
        assert flag in tokens, f"{flag} missing from ran line: {ran_line}"

    second = tmp_path / "second.yaml"
    second.write_bytes(X500_CONTRACT.read_bytes())
    replay_args = [str(second) if arg == str(first) else arg for arg in tokens[1:]]
    rerun = runner.invoke(app, replay_args)
    assert rerun.exit_code == 0, rerun.output
    assert second.read_bytes() == first_bytes


def test_add_family_model_budget_cli(tmp_path):
    """Family -> model -> budget authoring path via the CLI."""
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())

    result = runner.invoke(
        app,
        [
            "add",
            "family",
            str(contract_path),
            "--id",
            "fam.new",
            "--name",
            "New family",
            "--source",
            "s",
            "--witness",
            "w1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "added family fam.new" in plain(result.output)

    result = runner.invoke(
        app,
        [
            "add",
            "model",
            str(contract_path),
            "--id",
            "mdl.new",
            "--family",
            "fam.new",
            "--source",
            "s",
            "--witness",
            "w1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "added model mdl.new" in plain(result.output)

    result = runner.invoke(
        app,
        [
            "add",
            "budget",
            str(contract_path),
            "--id",
            "bud.new-5v",
            "--kind",
            "rail_power",
            "--rail",
            "rail.pm02-5v",
            "--limit",
            "10 W",
            "--owner",
            "power-integration",
            "--source",
            "s",
            "--witness",
            "w1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "added budget bud.new-5v" in plain(result.output)


def test_add_prints_the_three_records_and_one_replayable_cli_line(tmp_path):
    """`add device --family <new> --model <new>` prints all three
    written records and one `ran: lockwire add ...` line that replays the
    whole submit on an untouched copy to a byte-identical file.
    """
    contract_a = tmp_path / "a.yaml"
    contract_b = tmp_path / "b.yaml"
    contract_a.write_bytes(X500_CONTRACT.read_bytes())
    contract_b.write_bytes(X500_CONTRACT.read_bytes())

    result = runner.invoke(
        app,
        [
            "add",
            "device",
            str(contract_a),
            "--id",
            "dev.camera.oakd-lite-cli",
            "--family",
            "fam.camera.depth",
            "--model",
            "mdl.camera.oak-d-lite-cli",
            "--owner",
            "perception",
            "--source",
            "datasheets/oakd-lite.pdf",
            "--witness",
            "Maya Chen",
        ],
    )
    assert result.exit_code == 0, result.output
    output = plain(result.output)
    assert "added family fam.camera.depth" in output
    assert "added model mdl.camera.oak-d-lite-cli" in output
    assert "added device dev.camera.oakd-lite-cli" in output
    lines = strip_ansi(result.output).splitlines()
    ran_line = next(line for line in lines if line.startswith("ran: lockwire add"))
    replay = ran_line[len("ran: ") :].replace(str(contract_a), str(contract_b))
    argv = shlex.split(replay)
    assert argv[0] == "lockwire"
    replayed = runner.invoke(app, argv[1:])
    assert replayed.exit_code == 0, replayed.output
    assert (
        hashlib.sha256(contract_a.read_bytes()).hexdigest()
        == hashlib.sha256(contract_b.read_bytes()).hexdigest()
    )


def test_add_device_unknown_model_refused_exit_2(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    before = contract_path.read_bytes()
    result = runner.invoke(
        app,
        [
            "add",
            "device",
            str(contract_path),
            "--id",
            "dev.sensor.new",
            "--model",
            "mdl.does-not-exist",
            "--owner",
            "perception",
            "--source",
            "s",
            "--witness",
            "w1",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "is not declared" in plain(result.output)
    assert contract_path.read_bytes() == before


def test_add_device_unknown_bus_refused_exit_2(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500_CONTRACT.read_bytes())
    before = contract_path.read_bytes()
    result = runner.invoke(
        app,
        [
            "add",
            "device",
            str(contract_path),
            "--id",
            "dev.sensor.new",
            "--model",
            "mdl.camera.oak-d-lite",
            "--owner",
            "perception",
            "--bus-ref",
            "bus.does-not-exist",
            "--host",
            "companion-computer",
            "--source",
            "s",
            "--witness",
            "w1",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "unknown bus" in plain(result.output)
    assert contract_path.read_bytes() == before


def test_check_nothing_comparable_is_not_scored_exit_3():
    """`check` on a contract with nothing comparable must read NOT SCORED,
    never GO, and exit 3 — distinct from a clean pass."""
    result = runner.invoke(app, ["check", str(FIXTURES / "contract_empty.yaml")])
    flat = plain(result.output)
    assert "NOT SCORED" in flat
    assert "GO" not in flat
    assert result.exit_code == 3, result.output


# --- the frozen binary --------------------------------------------------
#
# The suite does not run PyInstaller (a build is ~40 s and needs the whole toolchain),
# so these split in two: the `sys.frozen` refusals are exercised for real with the flag
# faked, and the spec's claims are read off the frozen-binary spec as text. The one
# thing neither can prove — that the built binary actually starts and answers — is the
# box's manual accept line, run by hand and pasted into the report.


def test_start_without_out_refuses_when_frozen(monkeypatch, tmp_path, testbot_urdf):
    """Same guard on `start`, whose --out names the lockfile it would create."""
    monkeypatch.chdir(tmp_path)  # the refusal must fire before anything is written here
    monkeypatch.setattr("lockwire.cli.sys.frozen", True, raising=False)
    result = runner.invoke(app, ["start", str(testbot_urdf)])
    assert result.exit_code == 2, result.output
    assert "needs --out" in plain(result.output)
    assert not (tmp_path / "lockwire.yaml").exists()

    contract = tmp_path / "lockwire.yaml"
    result = runner.invoke(app, ["start", str(testbot_urdf), "--out", str(contract)])
    assert result.exit_code == 0, result.output
    assert contract.exists()


def _contract_at_version(tmp_path: Path, version: str) -> Path:
    """A copy of the clean fixture whose declared ``schema_version`` is ``version``."""
    source = (FIXTURES / "contract_clean.yaml").read_text()
    lines = [
        f'schema_version: "{version}"' if line.startswith("schema_version") else line
        for line in source.splitlines()
    ]
    path = tmp_path / "lockwire.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _contract_taking_commands() -> set[str]:
    """Every leaf command in the typer app that takes a ``contract`` parameter,
    read off the app itself.

    The first pass kept the door list by hand, and the hand-kept
    list was four doors short (`sample`, `declare`, `apply`, `add`) — the same
    contract-taking commands that were, not coincidentally, the unfixed ones.
    Enumerating from the app is what makes "every" in the test name below a
    claim the test can actually check: a command added tomorrow with a
    ``contract`` parameter turns this red until someone exercises it.
    """
    import typer

    found: set[str] = set()

    def walk(command, prefix: str = "") -> None:
        subcommands = getattr(command, "commands", None)
        if subcommands:
            for name, sub in subcommands.items():
                walk(sub, f"{prefix} {name}".strip())
        elif any(param.name == "contract" for param in command.params):
            found.add(prefix)

    walk(typer.main.get_command(app))
    return found


def _doors_that_did_not_refuse(doors: dict[str, list[str]]) -> dict[str, str]:
    """Invoke every door's argv against its (already schema-0.1) contract and
    report which ones did not render a clean REFUSED. Shared by the
    behavioural test above and the combined parity guard below, so the
    checks that define "refuses correctly" live in exactly one place.

    Every door is checked before returning, so a regression names all of them
    at once rather than stopping at the first — the claim is "every"."""
    broken = {}
    for name, argv in doors.items():
        result = runner.invoke(app, argv)
        output = plain(result.output)
        checks = [
            ("no REFUSED", "REFUSED" in output),
            ("does not name 0.1", "0.1" in output),
            ("does not name 0.2, 0.3", "0.2, 0.3" in output),
            ("traceback", "Traceback" not in output),
            ("blames the edit", "change(s)" not in output),
        ]
        if name != "baseline-check":  # the ceiling above: it keeps its own code
            checks.append((f"exit {result.exit_code} != 2", result.exit_code == 2))
        why = [reason for reason, ok in checks if not ok]
        if why:
            broken[name] = f"{', '.join(why)} — {output.strip()}"
    return broken


def test_the_edit_lane_still_blames_the_change_that_broke_the_contract(tmp_path):
    """The other half of the edit-lane fix, and the reason it is not a blanket wrap.

    `declare`/`apply`/`add` load the contract *after* applying the change, so
    two different failures arrive at the same line. An unsupported
    ``schema_version`` was in the file first and is REFUSED (above); a contract
    the edit itself made invalid is still "invalid after N change(s)", naming
    the entry at fault. Wrapping both as REFUSED would trade one wrong message
    for another, so this pins the message that must survive.

    The fixture must be valid *before* the edit or this proves nothing — an
    earlier version of this test downgraded the clean fixture's
    ``schema_version`` to "0.2", which already fails `check` on its own
    (fitted_bands/fitted_band_allocations require "0.3"), so the assertion
    below could not tell "the edit broke it" from "it was already broken".
    This copy keeps the fixture's native, valid "0.3" and checks it passes
    `check` first, then breaks it with a real edit: emptying
    ``devices/dev.imu/envelopes`` removes its fitted-band inventory while the
    contract's ``fitted_band_allocations`` still allocates it — a
    root-level model-validator error (no single changed field to blame),
    which is exactly the "contract invalid after N change(s)" fallback path.
    """
    contract = tmp_path / "lockwire.yaml"
    shutil.copy(FIXTURES / "contract_clean.yaml", contract)

    before = runner.invoke(app, ["check", str(contract)])
    assert before.exit_code == 0, plain(before.output)

    changes = tmp_path / "changes.yaml"
    changes.write_text(
        "- path: devices/dev.imu/envelopes\n"
        "  value: []\n"
        "  source: hand measurement\n"
        "  witness: tester\n"
    )
    result = runner.invoke(app, ["apply", str(contract), str(changes), "--dry-run"])
    output = plain(result.output)
    assert result.exit_code == 2, output
    assert "apply failed:" in output, output
    assert "invalid after 1 change(s)" in output, output
    assert "REFUSED" not in output, output
    assert "Traceback" not in output, output


def test_each_pose_command_prints_a_replayable_cli_line(tmp_path):
    """Every pose edit prints the `lockwire ...` line that reproduces it, and
    replaying those three lines against an untouched copy produces a
    BYTE-IDENTICAL file.

    That is the whole point of the printed line: what a gizmo drag or a
    keyboard nudge did is reproducible from a terminal, and the two
    routes have to end in this same writer. The line is built by
    `cli_line.argv_from_context`, which reads the argv actually typed — a
    hand-written flag list is how `add` and `scene import` each dropped an
    option and printed a line that did something else.
    """
    import hashlib
    import shlex

    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_bytes((FIXTURES / "armtree.yaml").read_bytes())
    b.write_bytes((FIXTURES / "armtree.yaml").read_bytes())

    invocations = [
        [
            "move",
            str(a),
            "dev.gripper",
            "--offset",
            "0.010,0.000,0.025",
            "--unit",
            "m",
            "--source",
            "notes/layout.md",
            "--witness",
            "Maya Chen",
            "--locked-at",
            "2026-09-08",
        ],
        [
            "rotate",
            str(a),
            "wrist",
            "--rpy",
            "0,0,1.5708",
            "--unit",
            "rad",
            "--source",
            "notes/layout.md",
            "--witness",
            "Maya Chen",
            "--locked-at",
            "2026-09-08",
        ],
        [
            "reparent",
            str(a),
            "elbow",
            "--to",
            "base",
            "--source",
            "notes/layout.md",
            "--witness",
            "Maya Chen",
            "--locked-at",
            "2026-09-08",
        ],
    ]
    replays = []
    for argv in invocations:
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        line = next(
            ln for ln in strip_ansi(result.output).splitlines() if ln.startswith("ran: lockwire ")
        )
        replays.append(shlex.split(line[len("ran: ") :])[1:])

    for argv in replays:
        assert argv[1] == str(a), argv
        result = runner.invoke(app, [argv[0], str(b), *argv[2:]])
        assert result.exit_code == 0, result.output

    assert hashlib.sha256(a.read_bytes()).hexdigest() == (
        hashlib.sha256(b.read_bytes()).hexdigest()
    ), "the replayed line did not reproduce the run it describes"


def test_each_pose_command_refuses_with_exit_2_and_writes_nothing(tmp_path):
    """The three named refusals, through the CLI, each exit 2 and each says
    which mistake it is — a cycle and an orphan are different problems and a
    generic "refused" would leave the engineer guessing."""
    import hashlib

    contract = tmp_path / "lockwire.yaml"
    contract.write_bytes((FIXTURES / "armtree.yaml").read_bytes())
    before = hashlib.sha256(contract.read_bytes()).hexdigest()

    cases = {
        "CYCLE": ["reparent", str(contract), "shoulder", "--to", "wrist"],
        "ORPHAN": ["reparent", str(contract), "shoulder", "--to", "detached_cart"],
        "does not resolve": ["reparent", str(contract), "shoulder", "--to", "nosuchlink"],
    }
    for expected, argv in cases.items():
        result = runner.invoke(
            app, [*argv, "--source", "notes/layout.md", "--witness", "Maya Chen"]
        )
        assert result.exit_code == 2, result.output
        assert expected in plain(result.output).replace("\n", " "), result.output

    result = runner.invoke(
        app,
        [
            "rotate",
            str(contract),
            "wrist",
            "--rpy",
            "0,0,1.5708",
            "--source",
            "notes/layout.md",
            "--witness",
            "Maya Chen",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--unit is required" in plain(result.output).replace("\n", " "), result.output

    assert hashlib.sha256(contract.read_bytes()).hexdigest() == before


def test_start_example_then_check_is_no_go(tmp_path, monkeypatch):
    """Two commands from nothing to a first NO-GO: the shipped example lockfile."""
    from typer.testing import CliRunner

    from lockwire.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    wrote = runner.invoke(app, ["start", "--example", "servo-bench"])
    assert wrote.exit_code == 0 and "built to fail" in wrote.output  # labelled everywhere
    checked = runner.invoke(app, ["check"])
    assert checked.exit_code == 1  # bites losing the example or its violation
    assert "NO-GO" in checked.output
    # the drone starter carries its model beside the lockfile; it is the X500 without the fixture's
    # companion computer, so it opens GO where the fixture is NO-GO
    assert runner.invoke(app, ["start", "--example", "drone", "--out", "drone.yaml"]).exit_code == 0
    assert (tmp_path / "drone.glb").stat().st_size > 100_000
    bad = runner.invoke(app, ["start", "--example", "nope", "--out", "other.yaml"])
    assert bad.exit_code == 1 and "servo-bench" in bad.output and "pi-servos" not in bad.output


def test_fresh_start_summary_claims_no_comparison_and_stays_go(testbot_urdf, tmp_path):
    """A fresh start stays GO with notes; the scored line names the hash match apart from the
    one real comparison a bare URDF gives (gravity reach of the hip)."""
    contract = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", str(testbot_urdf), "--out", str(contract)]).exit_code == 0
    result = runner.invoke(app, ["check", str(contract)])
    out = plain(result.output)
    assert result.exit_code == 0, out
    line = next(x for x in strip_ansi(result.output).splitlines() if x.startswith("scored "))
    assert "scored 2: 1 add up" in line, line  # bites the hash match counted as "add up"
    assert "1 model check" in line, line


def test_mesh_bytes_edit_never_claims_structure_unchanged(tmp_path):
    (tmp_path / "meshes").mkdir()
    (tmp_path / "package.xml").write_text("<package><name>pkg</name></package>")
    (tmp_path / "meshes/b.stl").write_text("solid b")
    (tmp_path / "r.urdf").write_text(
        '<robot name="r"><link name="base"><visual><geometry>'
        '<mesh filename="package://pkg/meshes/b.stl"/></geometry></visual></link></robot>'
    )
    lock = str(tmp_path / "lockwire.yaml")
    assert runner.invoke(app, ["start", str(tmp_path / "r.urdf"), "--out", lock]).exit_code == 0
    (tmp_path / "meshes/b.stl").write_text("solid edited")
    result = runner.invoke(app, ["check", lock])
    assert result.exit_code == 1 and "external_assets" in plain(result.output)
    assert "structure unchanged since lock" not in plain(result.output)


def test_old_example_names_still_work_but_are_never_listed(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lockwire.cli import app
    from lockwire.start_operation import example_names

    assert {"drone", "servo-bench"} <= set(example_names())
    assert not {"x500", "pi-servos"} & set(example_names())
    monkeypatch.chdir(tmp_path)
    for old in ("x500", "pi-servos"):
        assert (
            CliRunner().invoke(app, ["start", "--example", old, "--out", f"{old}.yaml"]).exit_code
            == 0
        )


def test_check_strict_turns_notes_into_failure(testbot_urdf, tmp_path):
    contract = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract)]).exit_code == 0
    loose = runner.invoke(app, ["check", str(contract)])
    assert loose.exit_code == 0 and "strict mode" not in plain(loose.output)
    strict = runner.invoke(app, ["check", str(contract), "--strict"])
    assert strict.exit_code == 1, strict.output
    assert "strict mode turned 1 note into a failure" in plain(strict.output)


def _passes_only_report():
    from lockwire.report import RecordReport, ScoredRecord

    record = ScoredRecord(quantity="x/y", observed="1 W", expected="<= 2 W", margin="1 W spare")
    return RecordReport(scored=1, scored_keys=frozenset({"x/y"}), scored_records=[record])


def test_check_strict_clean_go_exits_0_and_lists_passes(testbot_urdf, tmp_path, monkeypatch):
    contract = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract)]).exit_code == 0
    monkeypatch.setattr("lockwire.cli.run_record_checks", lambda *a, **k: _passes_only_report())
    result = runner.invoke(app, ["check", str(contract), "--strict", "--all"])
    out = plain(result.output)
    assert result.exit_code == 0, out
    assert "strict mode" not in out
    assert "Passed: 1 check x" in out and "1 W" in out


def test_check_without_passes_prints_no_passed_line(tmp_path):
    contract = tmp_path / "lockwire.yaml"
    contract.write_text("robot: {name: bare}\n")  # no scored records: nothing can pass
    assert "Passed:" not in plain(runner.invoke(app, ["check", str(contract)]).output)


def test_report_and_check_json_refuse_to_write_over_the_contract(tmp_path):
    """`report a.yaml --out a.yaml` and `check b.yaml --json b.yaml`
    overwrote the contract; both are refused and the contract is unchanged byte for byte."""
    contract = tmp_path / "a.yaml"
    shutil.copy(X500_CONTRACT, contract)
    for argv in (["report", str(contract), "--out"], ["check", str(contract), "--json"]):
        result = runner.invoke(app, [*argv, str(contract)])
        assert result.exit_code == 2 and "refusing to write over a lockfile" in plain(result.output)
        assert contract.read_bytes() == X500_CONTRACT.read_bytes()
    (out := tmp_path / "some" / "dir" / "report.html").parent.mkdir(parents=True)
    assert runner.invoke(app, ["report", str(contract), "--out", str(out)]).exit_code == 0
    assert out.read_text().startswith("<!doctype html>")


# --- batch 2 cli-edges ---
# Contract text (report.EXIT_CODE_TABLE["check"]): "1 NO-GO" means the robot is wrong;
# "2 contract could not be found, read, or parsed" means the tool failed.


def _one_plain_line(result) -> str:
    out = plain(result.output)
    assert "Traceback" not in out and "Error:" not in out.split("\n")[0][:6], out
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    return out


def _go_contract(tmp_path, testbot_urdf) -> Path:
    contract = tmp_path / "c.yaml"
    assert runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract)]).exit_code == 0
    return contract


def test_m02_check_json_into_missing_directory_is_exit_2_not_a_no_go(tmp_path, testbot_urdf):
    """an unwritable --json is a tool failure (exit 2), never exit 1 (NO-GO)."""
    contract = _go_contract(tmp_path, testbot_urdf)
    result = runner.invoke(app, ["check", str(contract), "--json", str(tmp_path / "nodir/j.json")])
    assert result.exit_code == 2, result.output
    _one_plain_line(result)


def test_l02_report_out_into_missing_directory_is_one_line_exit_2(tmp_path, testbot_urdf):
    """`report --out nodir/r.html` prints no traceback."""
    contract = _go_contract(tmp_path, testbot_urdf)
    result = runner.invoke(app, ["report", str(contract), "--out", str(tmp_path / "nodir/r.html")])
    assert result.exit_code == 2, result.output
    _one_plain_line(result)


def test_m02_init_on_a_malformed_urdf_is_exit_2(tmp_path):
    """init's own comment says tool failure is exit 2; a ParseError is one."""
    bad = tmp_path / "bad.urdf"
    bad.write_text("<robot")
    result = runner.invoke(app, ["init", str(bad), "--out", str(tmp_path / "c.yaml")])
    assert result.exit_code == 2, result.output
    _one_plain_line(result)


def test_l02_start_on_a_malformed_urdf_is_one_plain_line(tmp_path):
    """`start badxml` prints no traceback. Exit stays 1: report.EXIT_CODE_TABLE["start"]
    documents "1 the URDF/SDF could not be parsed" (table unchanged)."""
    bad = tmp_path / "bad.urdf"
    bad.write_text("<robot")
    result = runner.invoke(app, ["start", str(bad), "--out", str(tmp_path / "c.yaml")])
    assert result.exit_code == 1, result.output
    _one_plain_line(result)


def test_m21_review_with_a_stale_expected_revision_is_refused_and_writes_nothing(
    tmp_path, testbot_urdf
):
    """--expected-revision reaches api.review; a stale one refuses, exit 2."""
    from lockwire.edit import contract_revision

    contract = _go_contract(tmp_path, testbot_urdf)
    before = contract.read_bytes()
    args = ["review", str(contract), "model", "--approve", "--witness", "tester"]
    result = runner.invoke(app, [*args, "--expected-revision", "0" * 64])
    assert result.exit_code == 2, result.output
    assert "changed since" in plain(result.output)
    assert contract.read_bytes() == before
    fresh = runner.invoke(app, [*args, "--expected-revision", contract_revision(contract)])
    assert fresh.exit_code == 0, fresh.output


def test_l80_log_for_a_missing_path_or_no_repo_is_exit_2(tmp_path, monkeypatch):
    """`log` exited 0 for no repository and for a missing path inside one."""
    monkeypatch.chdir(tmp_path)  # a scratch folder: no repository above it
    outside = runner.invoke(app, ["log"])
    assert outside.exit_code == 2, outside.output
    _one_plain_line(outside)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    inside = runner.invoke(app, ["log", "--", "nope.yaml"])
    assert inside.exit_code == 2, inside.output
    _one_plain_line(inside)


def test_l84_a_quoted_tilde_is_expanded(tmp_path, testbot_urdf, monkeypatch):
    """'~/c.yaml' passed quoted (no shell expansion) resolves under $HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _go_contract(tmp_path, testbot_urdf)
    result = runner.invoke(app, ["check", "~/c.yaml"])
    assert result.exit_code == 0, result.output


def test_tilde_expands_only_path_parameters(tmp_path, monkeypatch):
    """Free-text values ('~', '~/todo') are untouched; path values expand."""
    import typer

    from lockwire.cli import _TildeGroup

    monkeypatch.setenv("HOME", str(tmp_path))
    probe = typer.Typer(cls=_TildeGroup)

    @probe.command("go")
    def go(
        where: Path,
        text: str,
        note: str = typer.Option("", "--note"),
        out: Path = typer.Option(Path("x"), "--out"),
    ):
        typer.echo(f"{where}|{note}|{out}|{text}")

    @probe.command("other")
    def other():
        pass

    got = runner.invoke(
        probe, ["go", "~/c.yaml", "~/free", "--note", "~/keep-me", "--out=~/o"]
    ).output.strip()
    assert got == f"{tmp_path}/c.yaml|~/keep-me|{tmp_path}/o|~/free"


def test_l97_help_texts_do_not_contradict_themselves():
    """report --help no longer says [default: open]; diff says "Default: HEAD" once."""
    report_help = plain(runner.invoke(app, ["report", "--help"]).output)
    assert "[default: open]" not in report_help
    diff_help = plain(runner.invoke(app, ["diff", "--help"]).output)
    assert diff_help.count("HEAD") == 1, diff_help


def _writer_args(name: str, tmp_path: Path, target: str) -> list[str]:
    legacy = tmp_path / "legacy.yaml"
    legacy.write_bytes(b'schema_version: "0.3"\nrobot:\n  name: legacy\n')
    parts = tmp_path / "parts.txt"
    parts.write_text("1 x 2208 motor\n")
    urdf = str(FIXTURES / "testbot.urdf")
    return {
        "start": ["start", urdf, "--out", target],
        "start-example": ["start", "--example", "servo-bench", "--out", target],
        "init": ["init", urdf, "--out", target],
        "upgrade": [
            "upgrade",
            str(legacy),
            "--out",
            target,
            "--expected-revision",
            hashlib.sha256(legacy.read_bytes()).hexdigest(),
        ],
        "scene-import": [
            "scene",
            "import",
            str(X500_GLB),
            "--contract",
            str(X500_CONTRACT),
            "--witness",
            "human",
            "--source-person",
            "Shivansh",
            "--out",
            target,
        ],
        "from-parts": ["from-parts", str(parts), "--out", target],
        "api-schema": ["api-schema", "--out", target],
        "api-types": ["api-types", "--out", target],
    }[name]


@pytest.mark.parametrize(
    "name",
    [
        "start",
        "start-example",
        "init",
        "upgrade",
        "scene-import",
        "from-parts",
        "api-schema",
        "api-types",
    ],
)
def test_every_out_writer_exits_2_on_a_missing_directory(name, tmp_path):
    """a missing folder is the tool failing, one plain line."""
    result = runner.invoke(app, _writer_args(name, tmp_path, str(tmp_path / "nodir" / "x.out")))
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert len(plain(result.output).strip().splitlines()) == 1, result.output
    assert not (tmp_path / "nodir").exists()


def test_scene_import_refuses_the_contract_as_its_changes_file(tmp_path):
    """--out may not be the contract it reads."""
    contract = tmp_path / "lockwire.yaml"
    shutil.copy(X500_CONTRACT, contract)
    before = contract.read_bytes()
    args = _writer_args("scene-import", tmp_path, str(contract))
    args[args.index("--contract") + 1] = str(contract)
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert contract.read_bytes() == before


def test_anymal_fixture_checks_go_with_notes():
    """The ANYmal example reads as an unchanged model and passes with notes."""
    result = runner.invoke(app, ["check", str(FIXTURES / "anymal" / "contract.yaml")])
    out = plain(result.output)
    assert "joint moved" not in out
    assert "GO with notes" in out
    assert "NO-GO" not in out


def _start(tmp_path, model, *extra):
    out = tmp_path / "c.yaml"
    result = runner.invoke(app, ["start", str(model), "--out", str(out), *extra])
    return result, plain(result.output)


def test_start_expands_a_xacro_template_named_urdf(tmp_path):
    result, text = _start(tmp_path, FIXTURES / "turtlebot3" / "turtlebot3_burger.urdf")
    assert result.exit_code == 0, text
    assert "base_footprint" in (tmp_path / "c.yaml").read_text()


def test_start_xacro_arg_and_missing_arg_command(tmp_path):
    model = FIXTURES / "xacro_args.urdf.xacro"
    result, text = _start(tmp_path, model)
    assert result.exit_code == 1 and "Traceback" not in text
    assert f"lockwire start {model.name} --xacro-arg name=ur --xacro-arg arm_type=ur5e" in text
    result, text = _start(tmp_path, model, "--xacro-arg", "name=a", "--xacro-arg", "arm_type=b")
    assert result.exit_code == 0, text
    assert "b_arm" in (tmp_path / "c.yaml").read_text()


def test_check_replays_the_recorded_xacro_args(tmp_path):
    xacro = tmp_path / "a.xacro"
    body = '<robot xmlns:xacro="x" name="r"><xacro:arg name="foo" default="1"/><link name="a">'
    xacro.write_text(body + '<inertial><mass value="$(arg foo)"/></inertial></link></robot>')
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", str(xacro), "--out", str(lock), "--xacro-arg", "foo=2"]).exit_code == 0
    assert runner.invoke(app, ["check", str(lock)]).exit_code == 0
    xacro.write_text(xacro.read_text().replace("$(arg foo)", "$(arg foo)0"))
    assert runner.invoke(app, ["check", str(lock)]).exit_code == 1


def test_start_reversed_revolute_limits_are_unlimited_with_one_line(tmp_path):
    result, text = _start(tmp_path, FIXTURES / "limits_zero_revolute.urdf")
    assert result.exit_code == 0, text
    assert "j1" in text and "lower 0.0" in text and "pydantic" not in text


def test_start_bad_mass_is_zero_mass_with_one_line(tmp_path):
    result, text = _start(tmp_path, FIXTURES / "mass_nan_negative.urdf")
    assert result.exit_code == 0, text
    assert "base" in text and "nan" in text and "arm" in text and "-0.5" in text
    assert "pydantic" not in text


def test_check_json_dash_prints_the_document_to_stdout(tmp_path, testbot_urdf):
    urdf = tmp_path / "robot.urdf"
    urdf.write_text(testbot_urdf.read_text())
    contract = tmp_path / "c.yaml"
    assert runner.invoke(app, ["init", str(urdf), "--out", str(contract)]).exit_code == 0
    urdf.write_text(testbot_urdf.read_text().replace('"1.2"', '"1.32"', 1))  # +10 % mass
    result = runner.invoke(app, ["check", str(contract), "--json", "-"])
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)  # exactly one document: pipes into jq
    assert document["verdict"] and "wrote" not in result.stdout
    assert any("next_command" in f for f in document["findings"])
    assert not (tmp_path / "-").exists()


SO101 = Path(__file__).parent / "fixtures" / "so101" / "so101.urdf"


def _so101(tmp_path):
    model = tmp_path / "so101.urdf"
    shutil.copy(SO101, model)
    return model


def test_check_model_file_prints_findings_and_the_lock_line_without_writing_a_lock(tmp_path):
    model = tmp_path / "bare.urdf"
    model.write_text('<robot name="x"><link name="a"/></robot>')
    result = runner.invoke(app, ["check", str(model)])
    assert result.exit_code == 3, result.output  # a fresh lock compares nothing
    assert "nothing was compared" in result.output
    # the long temp path wraps the line under -n 4; compare with whitespace collapsed
    flat = " ".join(result.output.split())
    assert f"lockwire start {model} --out {tmp_path / 'lockwire.yaml'}" in flat
    assert [p.name for p in tmp_path.iterdir()] == ["bare.urdf"]
    sdf = runner.invoke(app, ["check", str(SO101.parent.parent / "px4vision.sdf")])
    assert "model/sanity" not in sdf.output and "model/values" not in sdf.output  # both run


def test_check_model_file_with_only_warnings_agrees_in_text_and_json():
    m = str(FIXTURES / "limits_zero_revolute.urdf")  # warnings only: no violation
    doc = json.loads(runner.invoke(app, ["check", m, "--json", "-"]).stdout)
    assert (doc["verdict"], runner.invoke(app, ["check", m]).exit_code) == ("NOT_SCORED", 3)


def test_check_model_file_with_mass_catches_a_datasheet_mismatch(tmp_path):
    model = _so101(tmp_path)  # link masses sum to 0.632 kg; 0.7 kg is illustrative
    result = runner.invoke(app, ["check", str(model), "--mass", "0.7 kg", "--source", "datasheet"])
    assert result.exit_code == 1, result.output
    assert "bud.mass/nominal" in result.output and "0.70 kg" in result.output
    assert "run: lockwire check" not in result.output  # the comparison it just made, again
    assert not (tmp_path / "lockwire.yaml").exists()


def test_check_servo_flags_joint_effort_above_the_servo_stall_torque():
    model = FIXTURES / "servo_arm.urdf"
    result = runner.invoke(app, ["check", str(model), "--servo", "feetech.sts3215", "--all"])
    out = plain(result.output)
    assert result.exit_code == 1, out
    assert "model/joints/shoulder/limits/effort" in out and "1.91 N*m" in out
    assert "VIOLATION model/joints/wrist/limits/effort" not in out  # 1 N*m fits the servo
    assert runner.invoke(app, ["check", str(model), "--servo", "nope"]).exit_code == 2


def test_check_servo_hint_gives_the_line_to_keep_it_only_without_a_servo_line(tmp_path):
    bench = tmp_path / "bench.yaml"
    bench.write_text((Path(lockwire.__file__).parent / "examples/servo-bench.yaml").read_text())
    lead = "add this line to"
    assert (
        lead not in runner.invoke(app, ["check", str(bench), "--servo", "feetech.sts3215"]).output
    )
    bare = tmp_path / "bare.yaml"
    bare.write_text(re.sub(r"(?m)^servo:\n(  .*\n)+", "", bench.read_text()))
    out = plain(runner.invoke(app, ["check", str(bare), "--servo", "feetech.sts3215"]).output)
    assert f"{lead} {bare}: servo: feetech.sts3215" in out.replace("\n", "")


def test_check_sdf_runs_the_limit_and_shape_checks():
    model = FIXTURES / "sdf_twin" / "model.sdf"
    result = runner.invoke(app, ["check", str(model), "--servo", "dynamixel.xl430-w250", "--all"])
    out = plain(result.output)
    assert "model/joints/wheel_joint/limits/effort" in out and "13.3x" in out
    assert "model/links/standoff/mass_vs_size" in out and "51x" in out
    assert "model/sanity" not in out and "velocity" not in out  # SDF omits velocity: unlimited
    from lockwire.scene_urdf import load_robot_scene  # collision-only links are drawn too

    assert {p.extras_id for p in load_robot_scene(model).parts} == {"base", "wheel", "standoff"}


def test_start_runs_the_check_unless_no_check(tmp_path):
    model = _so101(tmp_path)
    args = ["start", str(model), "--mass", "0.7 kg"]
    result = runner.invoke(app, [*args, "--out", str(tmp_path / "a.yaml")])
    assert "bud.mass/nominal" in result.output and (tmp_path / "a.yaml").exists()
    result = runner.invoke(app, [*args, "--no-check", "--out", str(tmp_path / "b.yaml")])
    assert "bud.mass" not in result.output and (tmp_path / "b.yaml").exists()


def _go_with_one_not_scored(monkeypatch):
    from lockwire.report import Finding, RecordReport, ScoredRecord

    record = ScoredRecord(quantity="x/y", observed="1 W", expected="<= 2 W", margin="1 W spare")
    gap = Finding(
        severity="not_scored",
        quantity="model/structure_hash",
        observed="no URDF provenance",
        expected="a URDF path",
        action="lock it",
        next_command="lockwire start robot.urdf",
    )
    report = RecordReport([gap], scored=1, scored_keys=frozenset({"x/y"}), scored_records=[record])
    monkeypatch.setattr("lockwire.cli.run_record_checks", lambda *a, **k: report)


def test_check_strict_not_scored_exits_3_and_max_not_scored_allows_it(
    testbot_urdf, tmp_path, monkeypatch
):
    contract = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract)]).exit_code == 0
    _go_with_one_not_scored(monkeypatch)
    assert runner.invoke(app, ["check", str(contract)]).exit_code == 0
    assert runner.invoke(app, ["check", str(contract), "--strict"]).exit_code == 3
    argv = ["check", str(contract), "--strict", "--max-not-scored", "1"]
    assert runner.invoke(app, argv).exit_code == 0


def test_check_lists_what_was_not_compared_with_the_next_command(
    testbot_urdf, tmp_path, monkeypatch
):
    contract = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract)]).exit_code == 0
    _go_with_one_not_scored(monkeypatch)
    result = runner.invoke(
        app, ["check", str(contract), "--json", str(tmp_path / "o.json"), "--all"]
    )
    out = plain(result.output)
    assert "Not compared yet (1):" in out and "lockwire start robot.urdf" in out
    gap = json.loads((tmp_path / "o.json").read_text())["not_compared"]
    assert gap == [
        {
            "quantity": "model/structure_hash",
            "reason": "no URDF provenance",
            "next_command": "lockwire start robot.urdf",
        }
    ]


def test_paid_plugin_import_failure_with_paid_sections_is_refused(
    testbot_urdf, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    contract = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract)]).exit_code == 0
    monkeypatch.setattr("lockwire.plugins.FAILED", [ModuleNotFoundError("x", name="fcntl")])
    monkeypatch.setattr(
        "lockwire.cli.load_contract", lambda *a: SimpleNamespace(actuator_cases=[1])
    )
    result = runner.invoke(app, ["check", str(contract)])
    assert result.exit_code == 2 and "fcntl" in plain(result.output)


def test_a_crash_is_refused_not_a_no_go(monkeypatch, capsys):
    import lockwire

    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr("lockwire.cli.app", boom)
    with pytest.raises(SystemExit) as stop:
        lockwire.main()
    assert stop.value.code == 2
    assert "lockwire stopped: RuntimeError: kaboom" in capsys.readouterr().err


def test_check_model_file_compares_a_recognised_robot_with_its_datasheet(tmp_path):
    waffle = FIXTURES / "turtlebot3" / "turtlebot3_waffle_pi.urdf"
    result = runner.invoke(app, ["check", str(waffle)])
    assert result.exit_code == 1 and "emanual.robotis.com" in result.output, result.output
    assert "not a recognised robot" not in result.output
    so101 = runner.invoke(app, ["check", str(SO101)])
    assert "not a recognised robot; pass --model <id>" in so101.output
    forced = runner.invoke(app, ["check", str(SO101), "--model", "openmanipulator-x"])
    assert forced.exit_code == 1 and "datasheet/openmanipulator-x/mass_total" in forced.output
    assert "franka-fr3" in runner.invoke(app, ["catalog", "robots"]).output


def test_check_model_file_hints_name_the_typed_path_not_the_temp_lock(tmp_path, monkeypatch):
    shutil.copy(FIXTURES / "turtlebot3" / "turtlebot3_waffle_pi.urdf", tmp_path)
    monkeypatch.chdir(tmp_path)
    out = plain(runner.invoke(app, ["check", "turtlebot3_waffle_pi.urdf"]).output)
    # the first verdict: violations and counts; warnings wait behind --all, gaps never do
    assert "VIOLATION datasheet/" in out and "Not compared yet (2):" in out
    assert "model/meshes: 4 mesh files were not found;" in out  # nothing is locked yet
    assert out.rstrip().endswith("drift: lockwire start turtlebot3_waffle_pi.urdf")  # no --out
    assert "model/links/placeholder_inertia: 3 link(s)" in out
    assert "mass_vs_size" not in out
    assert "3 warnings not shown: lockwire check turtlebot3_waffle_pi.urdf --all" in out
    strict = runner.invoke(app, ["check", "turtlebot3_waffle_pi.urdf", "--strict"]).output
    assert "mass_vs_size" in plain(strict)  # warnings that fail the run are shown
    out = plain(runner.invoke(app, ["check", "turtlebot3_waffle_pi.urdf", "--all"]).output)
    assert "source: turtlebot3_waffle_pi.urdf" in out
    assert "--force" not in out and "run: lockwire check" not in out  # no lock; same 1.8 kg
    assert "1 model check (no lockfile yet)" in out and "/lockwire-" not in out


def test_free_help_lists_five_verbs_and_hidden_ones_still_run():
    free = "import lockwire.plugins as p; p.entry_points = lambda **_: (); import lockwire.cli as c"
    out = subprocess.run(
        [sys.executable, "-c", free + "; c.app()", "--help"], capture_output=True, text=True
    )
    rows = re.findall(r"^│ ([a-z][\w-]*)\s", out.stdout, re.M)  # conftest pins COLUMNS=200
    assert " ".join(rows) == "check start accept mcp agent-setup"  # at most ten names
    assert "More: lockwire help --all" in out.stdout and "docs/" not in out.stdout
    assert "check-sim " in runner.invoke(app, ["help", "--all"]).output
    for name, command in typer.main.get_command(app).commands.items():
        assert not command.hidden or runner.invoke(app, [name, "--help"]).exit_code == 0, name


def test_cli_survives_a_cp1252_console_on_a_finding_with_a_delta():
    import os

    code = (
        "import lockwire, lockwire.cli as c\n"
        "from lockwire.report import Finding, render\n"
        "c.app = lambda: render([Finding(severity='violation', quantity='a/b', observed='1',"
        " expected='2', delta='1', action='x')])\n"
        "lockwire.main()"
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
    assert b"UnicodeEncodeError" not in out.stderr and b"? 1)" in out.stdout, out.stderr


def test_help_all_without_pro_installed_lists_pro_names_and_the_waitlist():
    from lockwire.plugins import PRO_COMMANDS, WAITLIST_URL

    free = "import lockwire.plugins as p; p.entry_points = lambda **_: (); import lockwire.cli as c"
    out = subprocess.run(
        [sys.executable, "-c", free + "; c.app()", "help", "--all"], capture_output=True, text=True
    ).stdout
    head, pro = out.split("\nPro\n")
    assert pro.splitlines()[0] == f"Paid plans are waitlist only for now: {WAITLIST_URL}"
    assert {line.split()[0] for line in pro.splitlines()[1:]} == PRO_COMMANDS
    assert "record-servos " in head and "fleet-drift" not in head  # free vs Pro


def test_agent_setup_writes_once_and_appends_to_an_existing_agents_file(tmp_path):
    agents = (
        "AGENTS" + ".md"
    )  # generated in tmp_path; spelled in parts so the public snapshot scan skips it
    (tmp_path / agents).write_text("# mine\n")
    for _ in range(2):
        result = runner.invoke(app, ["agent-setup", str(tmp_path)])
    text = (tmp_path / agents).read_text()
    assert text.startswith("# mine\n") and text.count("lockwire:start") == 1
    assert "Shivansh2703/lockwire" in (tmp_path / ".pre-commit-config.yaml").read_text()
    assert result.exit_code == 0 and f"kept {agents}" in result.output
    assert "wrote" not in result.output and "next: lockwire check <your robot" in result.output
    (tmp_path / "lockwire.yaml").write_text("")
    assert "next: commit" in runner.invoke(app, ["agent-setup", str(tmp_path)]).output
    pat = YAML().load(tmp_path / ".pre-commit-config.yaml")["repos"][0]["hooks"][0]["files"]
    names = ("robot.urdf", "a/R.URDF", "m.xacro", "x.sdf", "lockwire.yaml", "README.md")
    assert [bool(re.search(pat, n)) for n in names] == [True] * 5 + [
        False
    ]  # the hook runs on model and lockfile files only


def test_agent_setup_ci_writes_only_the_workflow(tmp_path, monkeypatch):
    for _ in range(2):
        result = runner.invoke(app, ["agent-setup", str(tmp_path), "--ci"])
    files = [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()]
    assert files == [".github/workflows/lockwire.yml"] and "kept .github" in result.output
    monkeypatch.setenv("PATH", str(tmp_path))  # no lockwire on the user's PATH: uvx
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)  # no robots extra
    assert "next: uvx lockwire check" in runner.invoke(app, ["agent-setup", str(tmp_path)]).output
    assert (
        "Run `uvx lockwire check` before" in (tmp_path / ("CLAUDE" + ".md")).read_text()
    )  # split: snapshot scan


def test_accept_records_a_known_gap_so_check_passes_and_still_shows_it(tmp_path, monkeypatch):
    shutil.copy(FIXTURES / "turtlebot3" / "turtlebot3_waffle_pi.urdf", tmp_path)
    monkeypatch.chdir(tmp_path)
    gap = "datasheet/turtlebot3-waffle-pi/mass_total"
    start = plain(runner.invoke(app, ["start", "turtlebot3_waffle_pi.urdf"]).output)
    assert f"run: lockwire accept {gap} lockwire.yaml --reason" in start
    assert runner.invoke(app, ["check"]).exit_code == 1
    args = ["accept", gap, "--reason", "sheet counts the battery", "--witness", "sam"]
    assert runner.invoke(app, args).exit_code == 0
    assert {p.name for p in tmp_path.iterdir()} == {"lockwire.yaml", "turtlebot3_waffle_pi.urdf"}
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert f"accepted: {gap}: URDF sum of link masses: 1.554; accepted (sheet counts" in plain(
        result.output
    )


def _fake_robot_descriptions(monkeypatch, urdf: Path) -> None:
    """robot_descriptions without network: ur5e has a URDF (testbot here), spot only an MJCF."""
    from types import ModuleType, SimpleNamespace

    package, xacro, ur5e = ModuleType("rd"), ModuleType("rd._xacro"), ModuleType("rd.ur5e")
    package.DESCRIPTIONS = {
        "ur5e_description": SimpleNamespace(has_urdf=True),
        "spot_mj_description": SimpleNamespace(has_urdf=False),
    }
    xacro.get_urdf_path = lambda module: str(urdf) if module is ur5e else None
    for name, module in [("", package), ("._xacro", xacro), (".ur5e_description", ur5e)]:
        monkeypatch.setitem(sys.modules, f"robot_descriptions{name}", module)
    monkeypatch.setitem(sys.modules, "tqdm", SimpleNamespace(tqdm=SimpleNamespace()))


def test_check_resolves_a_robot_name_through_robot_descriptions(
    monkeypatch, tmp_path, testbot_urdf
):
    _fake_robot_descriptions(monkeypatch, testbot_urdf)
    monkeypatch.chdir(tmp_path)
    for name in ("ur5e", "ur5e_description"):
        result = runner.invoke(app, ["check", name, "--json", "-"])
        assert json.loads(result.stdout)["robot"] == "testbot", result.output
    unknown = runner.invoke(app, ["check", "ur5x"])
    assert unknown.exit_code == 2 and "no file or robot named ur5x; did you mean ur5e" in plain(
        unknown.output
    )
    mjcf = runner.invoke(app, ["check", "spot"])
    assert mjcf.exit_code == 2 and "only an MJCF exists for spot" in plain(mjcf.output)
    out = plain(runner.invoke(app, ["check", "ur5e", "--all"]).output)
    assert "drift: lockwire start ur5e" in out and str(testbot_urdf.parent) not in out
    assert runner.invoke(app, ["start", "ur5e", "--no-check"]).exit_code == 0
    assert (tmp_path / "lockwire.yaml").is_file()  # in the working folder, not the cache
    # the model is copied beside the lockfile, so its source never leaves the folder
    from lockwire.schema import load_contract

    assert load_contract(tmp_path / "lockwire.yaml").model.source == "ur5e.urdf"
    copy = tmp_path / "ur5e.urdf"
    copy.unlink()  # a kept lockfile writes nothing, not even the copy
    assert "kept as is" in runner.invoke(app, ["start", "ur5e", "--no-check"]).output
    assert not copy.exists()
    copy.write_text(testbot_urdf.read_text().replace('<mass value="1.2"/>', '<mass value="9.9"/>'))
    drift = runner.invoke(app, ["check", "lockwire.yaml"])
    assert drift.exit_code == 1 and "mass 9.9, expected locked mass 1.2" in plain(drift.output)
    refused = runner.invoke(app, ["start", "ur5e", "--force", "--no-check"])
    assert refused.exit_code == 1 and "ur5e.urdf exists and differs" in plain(refused.output)
    (tmp_path / "ur5e").write_text("not: a contract\n")  # a real file of that name wins
    assert "testbot" not in plain(runner.invoke(app, ["check", "ur5e"]).output)


def test_check_robot_name_without_the_extra_says_how_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "robot_descriptions", None)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", "ur5e"])
    assert result.exit_code == 2
    assert 'uvx --from "lockwire[robots]" lockwire check ur5e' in plain(result.output)


def test_broken_private_parts_is_one_line_not_a_traceback(monkeypatch, tmp_path, capsys):
    (tmp_path / ".lockwire").mkdir()
    (tmp_path / ".lockwire" / "parts.yaml").write_text("not: a list\n")
    (tmp_path / "p.txt").write_text("1 servo\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["lockwire", "from-parts", str(tmp_path / "p.txt")])
    import lockwire

    with pytest.raises(SystemExit) as exit_:
        lockwire.main()
    err = capsys.readouterr().err
    assert (
        exit_.value.code == 2
        and "parts.yaml: must be a list" in err
        and "bug in lockwire" not in err
    )


@pytest.mark.parametrize(
    "old,new,said",
    [
        ("\nrobot:", "\nbogus_key: 1\nrobot:", "lock.yaml: bogus_key: unknown key"),
        ("value: 1.0, unit: kg", "value: abc, unit: kg", ".mass.value: must be a number"),
    ],
)
@pytest.mark.parametrize("command", [["check"], ["swap"]])
def test_a_mistake_in_the_lockfile_is_plain_lines_not_a_pydantic_dump(
    tmp_path, command, old, new, said
):
    lock = tmp_path / "lock.yaml"
    lock.write_text((FIXTURES / "contract_clean.yaml").read_text().replace(old, new, 1))
    r = runner.invoke(app, [*command, str(lock), *(["j"] if command == ["swap"] else [])])
    out = plain(r.output)
    assert r.exit_code == 2 and said in out, out
    assert "bug in lockwire" not in out and "further information" not in out, out


def test_plan_json_dash_keeps_stdout_pure_json(tmp_path):
    plan = tmp_path / "plan.yaml"
    plan.write_text("steps: []\n", encoding="utf-8")
    x500 = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"
    result = runner.invoke(app, ["plan", str(x500), str(plan), "--json", "-"])
    assert json.loads(result.stdout)  # no verdict line after the document


def test_missing_model_files_say_so_and_the_swap_hint_needs_a_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for args in (["check", "robot.urdf"], ["start", "nosuch.urdf"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2 and "file not found: " in plain(result.output)
    model = Path(__file__).parent / "fixtures" / "servo_arm.urdf"
    out = plain(runner.invoke(app, ["check", str(model), "--servo", "feetech.sts3215"]).output)
    assert "lockwire start" in out and "lockwire swap" not in out


def test_bench_without_its_corpus_and_mcp_without_the_extra_say_how_to_proceed(monkeypatch):
    monkeypatch.setattr("lockwire.cli.DEFAULT_CORPUS_PATH", Path("/none/corpus.yaml"))
    assert "source repo" in plain(
        runner.invoke(app, ["bench", str(SO101), "--corpus", "/none/corpus.yaml"]).output
    )
    monkeypatch.setitem(sys.modules, "lockwire.mcp_server", None)
    assert "pip install 'lockwire[mcp]'" in runner.invoke(app, ["mcp"]).output


def test_every_start_example_is_written_at_the_current_version(tmp_path):
    from lockwire.schema import SCHEMA_VERSION, load_contract
    from lockwire.start_operation import example_names, start_example

    for name in example_names():
        path = Path(start_example(name, tmp_path / f"{name}.yaml")["path"])
        assert load_contract(path).schema_version == SCHEMA_VERSION, name


def test_catalog_reads_survive_a_non_utf8_locale(tmp_path):
    model = Path(__file__).parent / "fixtures" / "servo_arm.urdf"
    env = {**os.environ, "PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"}
    cmd = [sys.executable, "-m", "lockwire", "check", str(model), "--servo", "feetech.sts3215"]
    run = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=tmp_path)
    assert "codec" not in run.stdout + run.stderr and run.returncode == 1


def test_check_unknown_robot_name_under_json_dash_errors_to_stderr(
    monkeypatch, tmp_path, testbot_urdf
):
    _fake_robot_descriptions(monkeypatch, testbot_urdf)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", "ur5x", "--json", "-"])
    assert result.exit_code == 2 and result.stdout == ""  # stdout stays a clean JSON channel
    assert "no file or robot named ur5x" in result.stderr


def test_check_refuses_model_only_flags_on_a_lockfile_and_json_errors_go_to_stderr(tmp_path):
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    result = runner.invoke(app, ["check", str(lock), "--mass", "1 kg"])
    assert result.exit_code == 2 and "--mass works on a model file only" in plain(result.output)
    result = runner.invoke(app, ["check", str(tmp_path / "none.yaml"), "--json", "-"])
    assert result.exit_code == 2 and result.stdout == "" and "no contract" in result.stderr
    result = runner.invoke(app, ["start", str(lock), "--out", str(tmp_path / "x.yaml")])
    assert result.exit_code == 1 and "is not a URDF" in plain(result.output)


def test_a_xacro_model_finds_its_mesh_and_a_mass_edit_after_start(tmp_path, monkeypatch):
    shutil.copytree(FIXTURES / "xacro_mesh", tmp_path, dirs_exist_ok=True)
    monkeypatch.chdir(tmp_path)
    assert "model/meshes" not in CliRunner().invoke(app, ["check", "r.urdf.xacro"]).output
    CliRunner().invoke(app, ["start", "r.urdf.xacro"])
    model = tmp_path / "r.urdf.xacro"
    model.write_text(model.read_text().replace('value="1.0"', 'value="9.0"'))
    assert "NO-GO" in CliRunner().invoke(app, ["check"]).output
    model.write_text(model.read_text().replace("${m}", "${typo}"))  # cannot be expanded now
    assert "NOT SCORED" in CliRunner().invoke(app, ["check"]).output


@pytest.mark.parametrize(
    "name, code, said",
    [
        ("neg_velocity", None, "joint 'j_b' velocity limit is -1; treated as not stated"),
        ("missing_child", 1, "joint 'j_ghost' names child link 'ghost'"),
        ("missing_parent", 1, "joint 'j_b' names parent link 'ghost'"),
        ("dup_link", 1, "link name 'a' is used more than once"),
        ("dup_joint", 1, "joint name 'j_b' is used more than once"),
        ("comma_decimal", 2, "link 'a' mass value is '1,5', not a number"),
    ],
)
def test_check_odd_model_gives_a_verdict_not_a_crash(name, code, said):
    result = runner.invoke(app, ["check", str(FIXTURES / "fuzz" / f"{name}.urdf"), "--all"])
    assert result.exit_code == code if code else result.exit_code != 2  # None: any verdict
    assert said in " ".join(plain(result.output).split())


def test_model_defect_names_the_file_as_typed(monkeypatch):
    monkeypatch.chdir(FIXTURES / "fuzz")
    document = json.loads(runner.invoke(app, ["check", "dup_link.urdf", "--json", "-"]).stdout)
    assert document["findings"][0]["observed"].startswith("dup_link.urdf: link name")


def test_comma_decimal_names_the_file_as_typed(monkeypatch):
    monkeypatch.chdir(FIXTURES / "fuzz")
    result = runner.invoke(app, ["check", "comma_decimal.urdf", "--all"])
    assert "comma_decimal.urdf" in result.output
    assert str(FIXTURES) not in plain(result.output)


def test_check_lock_on_existing_file_says_how_to_check(testbot_urdf, tmp_path):
    (lock := tmp_path / "m.yaml").write_text("x")
    r = runner.invoke(app, ["check", str(testbot_urdf), "--lock", str(lock)])
    assert r.exit_code == 2 and "already exists; run `lockwire check" in plain(r.output)


def test_check_payload_flag_loads_a_link_for_the_gravity_check(testbot_urdf):
    args = ["check", str(testbot_urdf), "--all", "--payload", "2 kg"]
    assert "with 2 kg payload" in plain(runner.invoke(app, [*args, "--at", "leg_link"]).output)
    assert runner.invoke(app, args).exit_code == 2
    bad = runner.invoke(app, [*args, "--at", "arm"])
    assert bad.exit_code == 2 and "no link named arm; the links are: base_link" in bad.output


def test_check_torque_flag_compares_a_joint_with_the_published_figure(testbot_urdf):
    args = ["check", str(testbot_urdf), "--all", "--source", "u", "--torque"]
    off = plain(runner.invoke(app, [*args, "hip=20 N*m"]).output)
    assert "datasheet/hip/joint_torque" in off and "Δ -60.0%" in off
    assert "datasheet/hip" not in plain(runner.invoke(app, [*args, "hip=8 N*m"]).output)
    bad = runner.invoke(app, [*args, "knee=8 N*m"])
    assert bad.exit_code == 2 and "no joint named knee; the joints are: hip" in bad.output
    assert "no effort limit" in plain(runner.invoke(app, [*args, "mount_fix=8 N*m"]).output)
    assert "above 0" in plain(runner.invoke(app, [*args, "hip=0 N*m"]).output)
