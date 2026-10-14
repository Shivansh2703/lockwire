import hashlib
import shlex
import shutil
from pathlib import Path

from conftest import plain, strip_ansi
from ruamel.yaml import YAML
from typer.testing import CliRunner

from lockwire.cli import app

runner = CliRunner()
BAGS = Path(__file__).parent / "fixtures" / "bags"
FIXTURES = Path(__file__).parent / "fixtures"


def test_init_then_check(testbot_urdf, tmp_path):
    contract_path = tmp_path / "lockwire.yaml"

    result = runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract_path)])
    assert result.exit_code == 0, result.output
    assert contract_path.exists()
    assert "zero-mass link(s)" in plain(result.output)

    # zero-mass sensor_mount is a warning -> still GO (exit 0)
    result = runner.invoke(app, ["check", str(contract_path)])
    assert result.exit_code == 0, result.output
    assert "GO" in plain(result.output)
    assert "sensor_mount" in plain(result.output)


def test_explicit_upgrade_writes_new_v04_without_changing_legacy_bytes(tmp_path):
    from lockwire.schema import load_contract

    for version in ("0.2", "0.3"):
        source = tmp_path / f"legacy-{version}.yaml"
        original = (
            f'# retained legacy comment\nschema_version: "{version}"\n'
            "robot:\n  name: legacy\n"
        ).encode()
        source.write_bytes(original)
        output = tmp_path / f"upgraded-{version}.yaml"
        digest = hashlib.sha256(original).hexdigest()

        result = runner.invoke(app, [
            "upgrade", str(source), "--out", str(output), "--expected-revision", digest,
        ])

        assert result.exit_code == 0, result.output
        assert source.read_bytes() == original
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        assert load_contract(source).schema_version == version
        assert load_contract(output).schema_version == "0.4"
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
        result = runner.invoke(app, [
            "upgrade", str(selected_source), "--out", str(selected_output),
            "--expected-revision", selected_digest,
        ])
        assert result.exit_code == 2, result.output
        assert refusal in plain(result.output)
        assert source.read_bytes() == original
        assert output.read_bytes() == b"existing history must remain\n"


def test_explicit_upgrade_refuses_nonlegacy_and_unsupported_content(tmp_path):
    for version, extra, refusal in (
        ("0.4", "", "requires an explicit legacy"),
        ("0.5", "", "REFUSED"),
        ("0.3", "structural: {}\n", "must not contain structural"),
        ("0.3", "actuator_cases: [{id: hidden}]\n", "actuator_cases"),
    ):
        source = tmp_path / f"input-{version}-{len(extra)}.yaml"
        original = (f'schema_version: "{version}"\nrobot: {{name: legacy}}\n' + extra).encode()
        source.write_bytes(original)
        output = tmp_path / f"output-{version}-{len(extra)}.yaml"
        result = runner.invoke(app, [
            "upgrade", str(source), "--out", str(output),
            "--expected-revision", hashlib.sha256(original).hexdigest(),
        ])
        assert result.exit_code == 2, result.output
        assert refusal in plain(result.output)
        assert source.read_bytes() == original
        assert not output.exists()

    unversioned = tmp_path / "unversioned.yaml"
    raw = b"robot: {name: ambiguous}\n"
    unversioned.write_bytes(raw)
    output = tmp_path / "unversioned-output.yaml"
    result = runner.invoke(app, [
        "upgrade", str(unversioned), "--out", str(output),
        "--expected-revision", hashlib.sha256(raw).hexdigest(),
    ])
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
        assert "check failed:" in plain(result.output)
        assert "Traceback" not in plain(result.output)

    dangling_result = runner.invoke(app, ["check", str(dangling)])
    assert dangling_result.exit_code == 2
    assert "no contract at" in plain(dangling_result.output)
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
    result = runner.invoke(app, [
        "gearbox", str(contract_path), "--id", "gbx.shoulder", "--ratio", "4.0",
        "--input-joint", "base_to_shoulder", "--output-joint", "shoulder_to_elbow",
        "--source", "arm datasheet", "--witness", "Maya Chen",
    ])
    assert result.exit_code == 0, result.output
    gearbox = YAML().load(contract_path.read_text())["gearboxes"][0]
    assert (gearbox["id"], gearbox["ratio"], gearbox["input_joint"], gearbox["output_joint"]) == (
        "gbx.shoulder", 4.0, "base_to_shoulder", "shoulder_to_elbow"
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
    assert hashlib.sha256(contract_a.read_bytes()).hexdigest() == hashlib.sha256(
        contract_b.read_bytes()
    ).hexdigest()


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
        ["move", str(a), "dev.gripper", "--offset", "0.010,0.000,0.025", "--unit", "m",
         "--source", "notes/layout.md", "--witness", "Maya Chen",
         "--locked-at", "2026-09-08"],
        ["rotate", str(a), "wrist", "--rpy", "0,0,1.5708", "--unit", "rad",
         "--source", "notes/layout.md", "--witness", "Maya Chen",
         "--locked-at", "2026-09-08"],
        ["reparent", str(a), "elbow", "--to", "base",
         "--source", "notes/layout.md", "--witness", "Maya Chen",
         "--locked-at", "2026-09-08"],
    ]
    replays = []
    for argv in invocations:
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        line = next(
            ln
            for ln in strip_ansi(result.output).splitlines()
            if ln.startswith("ran: lockwire ")
        )
        replays.append(shlex.split(line[len("ran: "):])[1:])

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
        ["rotate", str(contract), "wrist", "--rpy", "0,0,1.5708",
         "--source", "notes/layout.md", "--witness", "Maya Chen"],
    )
    assert result.exit_code == 2, result.output
    assert "--unit is required" in plain(result.output).replace("\n", " "), result.output

    assert hashlib.sha256(contract.read_bytes()).hexdigest() == before
