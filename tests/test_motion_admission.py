"""Static safety contract for E5.9a; live runtime proof waits for the build window."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lockwire_pro import jobs
from lockwire_pro.jobs import build_motion_argv, motion_evidence_is_genuine


ROOT = Path(__file__).parent
MOTION = ROOT.parent / "packaging" / "motion"
FIXTURE = ROOT / "fixtures" / "motion_admission"
IMAGE = "example.invalid/lockwire-motion@sha256:" + "a" * 64
LOCAL_IMAGE_ID = "sha256:" + "b" * 64
RUN_ID = "38cb5cc3-868b-4f24-8b39-583cc2e1b678"


def stamped(timestamp: str, raw: str) -> str:
    return "".join(f"{timestamp} {line}\n" for line in (raw + "\n---").splitlines())


def feedback(mode="enabled"):
    state, effort, tracking = "", "", ""
    for second, position in ((1, 0.2), (2, 0.8)):
        timestamp = f"2026-09-15T00:00:0{second}Z"
        value = 1.2 if mode == "enabled" else 0.0
        header = f"header: {{stamp: {{sec: {second}, nanosec: 0}}, frame_id: ''}}\n"
        state += stamped(
            timestamp,
            header
            + f"name:\n- pendulum_joint\nposition:\n- {position}\nvelocity:\n- 0.1\neffort:\n- {value}",
        )
        effort += stamped(timestamp, f"effort:\n- {value}")
        if mode == "enabled":
            optional = (
                "  accelerations: []\n  effort: []\n  time_from_start: {sec: 0, nanosec: 0}\n"
            )
            points = f"feedback:\n  positions:\n  - {position}\n  velocities:\n  - 0.1\n" + optional
            points += (
                f"reference:\n  positions:\n  - {position}\n  velocities:\n  - 0.1\n" + optional
            )
            points += "error:\n  positions:\n  - 0.0\n  velocities:\n  - 0.0\n" + optional
            points += "output: {positions: [], velocities: [], accelerations: [], effort: [1.2], time_from_start: {sec: 0, nanosec: 0}}\n"
            points += "multi_dof_joint_names: []\n"
            for field in ("reference", "feedback", "error", "output"):
                points += f"multi_dof_{field}: {{transforms: [], velocities: [], accelerations: [], time_from_start: {{sec: 0, nanosec: 0}}}}\n"
            tracking += stamped(
                timestamp,
                header + "joint_names:\n- pendulum_joint\n" + points + "speed_scaling_factor: 1.0",
            )
    return {"state": state, "effort": effort, "tracking": tracking}


@pytest.fixture
def admitted_profile(tmp_path, monkeypatch):
    profile = {
        "image": LOCAL_IMAGE_ID,
        "engine": "gazebo_harmonic",
        "ros": "jazzy",
        "architecture": "linux/arm64",
        "message_shape": "reference-feedback-error",
        "control_msgs_version": "5.9.0-1noble.20260902.141921",
        "controller_version": "4.42.1-1noble.20260904.012550",
        "baked_inputs": {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in {
                "runner": MOTION / "run-admission.sh",
                "packages": MOTION / "packages.lock",
                "robot": FIXTURE / "robot.urdf.xacro",
                "world": FIXTURE / "world.sdf",
                "controllers": FIXTURE / "controllers.yaml",
            }.items()
        },
        "trajectory_tolerance": 0.10,
        "goal_tolerance": 0.05,
        "goal_position": 0.8,
    }
    manifest = tmp_path / "trusted-test-profile.json"
    manifest.write_text(
        json.dumps(
            {
                "manifest_version": "1",
                "resolution_status": "admitted",
                "profiles": {"synthetic_gravity_pendulum": profile},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs, "_MOTION_PROFILE_PATH", manifest)
    return profile


def test_motion_argv_is_fixed_and_isolated(tmp_path, admitted_profile):
    argv = build_motion_argv(
        RUN_ID,
        output_dir=tmp_path,
        command=("enabled",),
        image=LOCAL_IMAGE_ID,
    )
    rendered = " ".join(argv)
    for required in (
        "--rm",
        "--network none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=1024",
        "--cpus=2",
        "--memory=4g",
    ):
        assert required in rendered
    assert "--privileged" not in argv and "--publish" not in argv
    assert "/var/run/docker.sock" not in rendered
    assert "lockwire.synthetic_runtime_admission=true" in argv
    assert "--pull=never" in argv


def test_motion_requires_recorded_image_not_merely_digest_syntax(tmp_path, admitted_profile):
    assert LOCAL_IMAGE_ID in build_motion_argv(
        RUN_ID, output_dir=tmp_path, image=LOCAL_IMAGE_ID, command=("enabled",)
    )
    for image in (IMAGE, "sha256:" + "a" * 64, "example.invalid/lockwire:latest"):
        with pytest.raises(ValueError, match="recorded immutable profile"):
            build_motion_argv(RUN_ID, output_dir=tmp_path, image=image, command=("enabled",))


def test_checked_in_profile_is_explicitly_unadmitted(tmp_path):
    with pytest.raises(ValueError, match="unadmitted"):
        build_motion_argv(RUN_ID, output_dir=tmp_path, image=LOCAL_IMAGE_ID, command=("enabled",))


@pytest.mark.parametrize(
    "override",
    [
        {"command": ("enabled", ";id")},
        {"command": ()},
        {"entrypoint": "/bin/sh"},
        {"plugins": ("foreign-plugin",)},
    ],
)
def test_motion_refuses_foreign_runner_plugins_or_argv(tmp_path, admitted_profile, override):
    options = {"command": ("enabled",)} | override
    with pytest.raises(ValueError, match="baked runner"):
        build_motion_argv(RUN_ID, output_dir=tmp_path, image=LOCAL_IMAGE_ID, **options)


@pytest.mark.parametrize("mount", ["relative", "/workspace", "/tmp,bad"])
def test_motion_refuses_unsafe_source_mounts(tmp_path, mount):
    with pytest.raises(ValueError):
        build_motion_argv(
            RUN_ID, output_dir=tmp_path, source_mounts={mount: "/workspace"}, image=IMAGE
        )


def test_motion_fixture_requires_real_feedback_and_no_hold():
    robot = (FIXTURE / "robot.urdf.xacro").read_text(encoding="utf-8")
    controllers = (FIXTURE / "controllers.yaml").read_text(encoding="utf-8")
    runner = (MOTION / "run-admission.sh").read_text(encoding="utf-8")
    assert '<command_interface name="effort"/>' in robot
    assert '<state_interface name="position"/>' in robot
    assert '<state_interface name="effort"/>' in robot
    assert "<hold_joints>false</hold_joints>" in robot
    assert (
        "command_interfaces: [effort]" in controllers and "open_loop_control: false" in controllers
    )
    assert "joint_states.raw" in runner and "controller_state" in runner
    assert "if (getline) print" in runner and "grep --line-buffered 'effort:'" not in runner
    assert 'case "$mode" in enabled|disabled)' in runner
    assert '[ "$mode" = enabled ]' in runner


def test_motion_runner_effort_extractor_keeps_headers_and_numeric_scalars():
    runner = (MOTION / "run-admission.sh").read_text(encoding="utf-8")
    extractor = runner.split("extract_effort() {\n", 1)[1].split("\n}", 1)[0]
    raw = "position:\n- 0.2\neffort:\n- 0.0\nvelocity:\n- 0.1\neffort:\n- 1.2\n"
    result = subprocess.run(
        ["bash", "-c", extractor], input=raw, text=True, capture_output=True, check=True
    )
    assert result.stdout == "effort:\n- 0.0\n---\neffort:\n- 1.2\n---\n"


def test_fake_feedback_and_hold_mutations_do_not_pass_admission():
    raw = feedback()
    assert motion_evidence_is_genuine("enabled", **raw)
    assert not motion_evidence_is_genuine("enabled", **(raw | {"effort": ""}))
    assert not motion_evidence_is_genuine("enabled", **(raw | {"tracking": ""}))
    assert not motion_evidence_is_genuine("disabled", **(raw | {"tracking": "held"}))


def test_disabled_motion_requires_zero_effort_and_evolution_without_tracking():
    raw = feedback("disabled")
    assert motion_evidence_is_genuine("disabled", **raw)
    assert not motion_evidence_is_genuine(
        "disabled", **(raw | {"state": raw["state"].replace("- 0.8", "- 0.2")})
    )
    assert not motion_evidence_is_genuine(
        "disabled", **(raw | {"effort": raw["effort"].replace("- 0.0", "- 0.1")})
    )
    assert not motion_evidence_is_genuine(
        "disabled", **(raw | {"tracking": feedback()["tracking"]})
    )


def test_timestamped_effort_requires_a_finite_numeric_scalar():
    raw = feedback()
    assert motion_evidence_is_genuine("enabled", **raw)
    assert not motion_evidence_is_genuine(
        "enabled", **(raw | {"effort": raw["effort"].replace("1.2", ".nan")})
    )


class FinishedProcess:
    def __init__(self, exit_code=0):
        self.returncode = exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def write_motion_artifacts(path, mode, profile, raw, exit_code=0):
    for field, name in {
        "state": "joint_states.raw",
        "effort": "effort.raw",
        "tracking": "tracking.raw",
    }.items():
        (path / name).write_text(raw[field], encoding="utf-8")
    document = {
        "label": "synthetic_runtime_admission",
        "mode": mode,
        "exit_code": exit_code,
        "evidence": {
            "joint_state": "joint_states.raw",
            "effort": "effort.raw",
            "tracking": "tracking.raw",
        },
        "verdict": "GO",
        "fixture_admission": "PASSED",
        "baked_inputs": profile["baked_inputs"],
        "control_msgs_version": profile["control_msgs_version"],
        "controller_version": profile["controller_version"],
    }
    (path / "document.json").write_text(json.dumps(document), encoding="utf-8")
    return document


def motion_job(path, mode, profile, monkeypatch, exit_code=0, evidence_dir=None):
    monkeypatch.setattr(jobs.MotionJob, "_owned_container_ids", lambda self: [])
    return jobs.MotionJob(
        "preflight",
        FinishedProcess(exit_code),
        path,
        container_name=f"lockwire-motion-{RUN_ID}",
        run_id=RUN_ID,
        mode=mode,
        profile=jobs._MotionProfile.model_validate(profile),
        evidence_dir=evidence_dir,
    )


@pytest.mark.parametrize("mode", ["enabled", "disabled"])
def test_actual_extractor_stamp_reader_and_shared_verifier_path(
    tmp_path, admitted_profile, monkeypatch, mode
):
    runner = (MOTION / "run-admission.sh").read_text(encoding="utf-8")
    extractor = runner.split("extract_effort() {\n", 1)[1].split("\n}", 1)[0]
    stamp_function = runner.split("stamp() {\n", 1)[1].split("\n}", 1)[0]
    raw = feedback(mode)
    unstamped_state = "\n".join(line.split(" ", 1)[1] for line in raw["state"].splitlines()) + "\n"
    extracted = subprocess.run(
        ["bash", "-c", extractor], input=unstamped_state, text=True, capture_output=True, check=True
    ).stdout
    stamp_input = "\n".join(line.split(" ", 1)[1] for line in raw["effort"].splitlines()) + "\n"
    assert extracted == stamp_input
    # Feed actual extractor output and real UTC stamp output into the production
    # reader; no substituted helper or fabricated stamp is the admission proof.
    for stream in ("state", "effort", "tracking"):
        if not raw[stream]:
            continue
        plain = (
            extracted
            if stream == "effort"
            else "\n".join(line.split(" ", 1)[1] for line in raw[stream].splitlines()) + "\n"
        )
        observed = ""
        parts = plain.split("---\n")[:-1]
        for index, part in enumerate(parts):
            if index:
                time.sleep(1.05)
            observed += subprocess.run(
                ["bash", "-c", stamp_function],
                input=part + "---\n",
                env=os.environ | {"TZ": "Pacific/Honolulu"},
                text=True,
                capture_output=True,
                check=True,
                timeout=5,
            ).stdout
        frames = jobs._motion_frames(observed)
        assert frames[-1][0].utcoffset().total_seconds() == 0
        assert abs((frames[-1][0] - jobs.datetime.now(jobs.timezone.utc)).total_seconds()) < 5
        raw[stream] = observed
    write_motion_artifacts(tmp_path, mode, admitted_profile, raw)
    calls = []
    original = jobs.motion_evidence_is_genuine

    def observed_verifier(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs, "motion_evidence_is_genuine", observed_verifier)
    job = motion_job(tmp_path, mode, admitted_profile, monkeypatch)
    document = job.result()
    assert calls == [mode]
    assert document["fixture_admission"] == "PASSED"
    assert document["verdict"] == ("GO" if mode == "enabled" else "NO-GO")
    assert document["physical_conformance"] == document["simulation_validity"] == "NOT_SCORED"
    assert job.snapshot()["document"] == document


@pytest.mark.parametrize("mode", ["enabled", "disabled"])
@pytest.mark.parametrize(
    "stream,old,new",
    [
        ("effort", "- 1.2", "- 0.0"),
        ("effort", "effort:", "effort: invalid"),
        ("effort", "- 0.0", "- .inf"),
        ("state", "velocity:", "missing_velocity:"),
        ("state", "- 0.1", "- .nan"),
        ("state", "- 0.8", "- 0.2"),
        ("state", "2026-09-15", "2026-02-30"),
        ("state", "00:00:02Z", "00:00:01Z"),
    ],
)
def test_production_reader_refuses_bad_feedback(
    tmp_path, admitted_profile, monkeypatch, mode, stream, old, new
):
    raw = feedback(mode)
    # Each mutation must actually change this mode's positive control.
    if old not in raw[stream]:
        old = "- 0.0" if mode == "disabled" else "- 1.2"
        if mode == "disabled" and new == "- 0.0":
            new = "- 0.1"
    assert old in raw[stream]
    raw[stream] = raw[stream].replace(old, new)
    write_motion_artifacts(tmp_path, mode, admitted_profile, raw)
    job = motion_job(tmp_path, mode, admitted_profile, monkeypatch)
    document = job.snapshot()["document"]
    assert document["fixture_admission"] == "REFUSED"
    assert document["verdict"] == "NO-GO"
    assert document["admission_reason"]


@pytest.mark.parametrize(
    "old,new",
    [
        ("feedback:", "actual:"),
        ("reference:", "desired:"),
        ("header:", "missing_header:"),
        ("nanosec: 0", "nanosec: 1000000000"),
        ("sec: 2", "sec: 1"),
        ("- 0.8", "- 0.2"),
        ("  - 0.1", "  - .inf"),
        ("effort: [1.2]", "effort: [.nan]"),
        ("speed_scaling_factor: 1.0", "speed_scaling_factor: .inf"),
        ("  accelerations: []", "  accelerations: [.inf]"),
        ("  - 0.0", "  - 0.2"),
        ("- 0.8", "- 0.7"),
        ("multi_dof_joint_names: []", "multi_dof_joint_names: [foreign]"),
        ("  positions:", "  positions: invalid"),
        ("00:00:02Z reference:", "00:00:01Z reference:"),
    ],
)
def test_production_reader_refuses_unknown_nonfinite_or_untracked_controller(
    tmp_path, admitted_profile, monkeypatch, old, new
):
    raw = feedback()
    assert old in raw["tracking"]
    raw["tracking"] = raw["tracking"].replace(old, new)
    write_motion_artifacts(tmp_path, "enabled", admitted_profile, raw)
    document = motion_job(tmp_path, "enabled", admitted_profile, monkeypatch).result()
    assert document["fixture_admission"] == "REFUSED" and document["verdict"] == "NO-GO"
    assert document["admission_reason"]


def test_tracking_joint_frame_mismatch_is_refused_by_production_reader(
    tmp_path, admitted_profile, monkeypatch
):
    raw = feedback()
    raw["tracking"] = raw["tracking"].replace("pendulum_joint", "foreign_joint")
    write_motion_artifacts(tmp_path, "enabled", admitted_profile, raw)
    job = motion_job(tmp_path, "enabled", admitted_profile, monkeypatch)
    document = job.result()
    assert document["fixture_admission"] == "REFUSED"
    assert document["verdict"] == "NO-GO"
    assert document["physical_conformance"] == document["simulation_validity"] == "NOT_SCORED"
    assert document["admission_reason"]
    snapshot = job.snapshot()
    assert snapshot["status"] == "completed"
    assert snapshot["document"] == document


def test_nonmonotonic_state_header_time_is_refused_by_production_reader(
    tmp_path, admitted_profile, monkeypatch
):
    raw = feedback()
    assert "sec: 2" in raw["state"]
    raw["state"] = raw["state"].replace("sec: 2", "sec: 1")
    write_motion_artifacts(tmp_path, "enabled", admitted_profile, raw)
    job = motion_job(tmp_path, "enabled", admitted_profile, monkeypatch)
    document = job.result()
    snapshot = job.snapshot()
    assert snapshot["status"] == "completed"
    assert snapshot["document"] == document
    assert document["fixture_admission"] == "REFUSED"
    assert document["verdict"] == "NO-GO"
    assert (
        document["admission_reason"]
        == "motion joint-state ROS header time has no monotonic evolution"
    )


@pytest.mark.parametrize("field", ["control_msgs_version", "controller_version", "message_shape"])
def test_profile_refuses_unknown_versions_and_shapes(tmp_path, admitted_profile, field):
    manifest = json.loads(jobs._MOTION_PROFILE_PATH.read_text())
    manifest["profiles"]["synthetic_gravity_pendulum"][field] = "unknown"
    jobs._MOTION_PROFILE_PATH.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid trusted motion profile"):
        build_motion_argv(RUN_ID, output_dir=tmp_path, image=LOCAL_IMAGE_ID, command=("enabled",))


@pytest.mark.parametrize(
    "mutation",
    ["foreign-image", "foreign-baked", "foreign-path", "exit-forgery", "symlink", "oversize"],
)
def test_public_start_result_refuses_foreign_identity_and_outputs(
    tmp_path, admitted_profile, monkeypatch, mutation
):
    monkeypatch.setattr(jobs.MotionJob, "_owned_container_ids", lambda self: [])

    def start(argv, **kwargs):
        path = Path(argv[argv.index("--mount") + 1].split("src=", 1)[1].split(",dst=", 1)[0])
        document = write_motion_artifacts(path, "enabled", admitted_profile, feedback())
        if mutation == "foreign-baked":
            document["baked_inputs"] = dict(document["baked_inputs"], runner="f" * 64)
        elif mutation == "foreign-path":
            document["evidence"]["effort"] = "../foreign.raw"
        elif mutation == "exit-forgery":
            document["exit_code"] = 1
        elif mutation == "symlink":
            effort = path / "effort.raw"
            effort.unlink()
            effort.symlink_to(path / "joint_states.raw")
        elif mutation == "oversize":
            (path / "joint_states.raw").write_bytes(b"x" * (jobs._MOTION_MAX_BYTES + 1))
        (path / "document.json").write_text(json.dumps(document), encoding="utf-8")
        return FinishedProcess()

    monkeypatch.setattr(jobs.subprocess, "Popen", start)
    if mutation == "foreign-image":
        with pytest.raises(ValueError, match="recorded immutable"):
            jobs.start_motion_job(RUN_ID, image=IMAGE, command=("enabled",))
        return
    job = jobs.start_motion_job(RUN_ID, image=LOCAL_IMAGE_ID, command=("enabled",))
    with pytest.raises(RuntimeError, match="admission refused"):
        job.result()
    assert job.snapshot()["status"] == "failed"


def test_motion_retains_hash_bound_artifacts_on_result_and_cancel(
    tmp_path, admitted_profile, monkeypatch
):
    for cancelled in (False, True):
        scratch = tmp_path / f"scratch-{cancelled}"
        scratch.mkdir()
        retained = tmp_path / f"retained-{cancelled}"
        retained.mkdir()
        write_motion_artifacts(scratch, "disabled", admitted_profile, feedback("disabled"))
        job = motion_job(scratch, "disabled", admitted_profile, monkeypatch, evidence_dir=retained)
        if cancelled:
            job.cancel()
        else:
            assert job.result()["fixture_admission"] == "PASSED"
        receipt = json.loads((retained / "receipt.json").read_text())
        assert receipt["cancelled"] == cancelled
        for name, digest in receipt["sha256"].items():
            assert hashlib.sha256((retained / name).read_bytes()).hexdigest() == digest
        assert not scratch.exists()


@pytest.mark.parametrize("failure", ["process", "document", "cleanup", "preservation"])
def test_failure_retention_never_resurrects_cached_go(
    tmp_path, admitted_profile, monkeypatch, failure
):
    scratch, retained = tmp_path / "scratch", tmp_path / "retained"
    scratch.mkdir()
    retained.mkdir()
    exit_code = 1 if failure == "process" else 0
    write_motion_artifacts(scratch, "enabled", admitted_profile, feedback(), exit_code)
    job = motion_job(scratch, "enabled", admitted_profile, monkeypatch, exit_code, retained)
    if failure == "document":
        (scratch / "document.json").write_text("malformed", encoding="utf-8")
    elif failure == "cleanup":

        def failed_cleanup(self):
            raise RuntimeError("owned container cleanup failed")

        monkeypatch.setattr(jobs.MotionJob, "_owned_container_ids", failed_cleanup)
    elif failure == "preservation":
        (retained / "receipt.json").write_text("foreign", encoding="utf-8")
    if failure == "process":
        assert job.result()["fixture_admission"] == "REFUSED"
    else:
        with pytest.raises((RuntimeError, OSError)):
            job.result()
        snapshot = job.snapshot()
        assert snapshot["status"] == "failed" and snapshot["document"] is None
        with pytest.raises((RuntimeError, OSError)):
            job.result()
    assert not scratch.exists()
    if failure == "preservation":
        assert (retained / "receipt.json").read_text() == "foreign"
    else:
        receipt = json.loads((retained / "receipt.json").read_text())
        assert receipt["sha256"]
        assert receipt["document"] is None or receipt["document"]["verdict"] == "NO-GO"


@pytest.mark.parametrize("mode", ["enabled", "disabled"])
@pytest.mark.parametrize("mutation", ["none", "malformed", "nonfinite", "held", "actuation"])
def test_public_library_motion_result_uses_admission_for_both_modes(
    tmp_path, admitted_profile, monkeypatch, mode, mutation
):
    monkeypatch.setattr(jobs.MotionJob, "_owned_container_ids", lambda self: [])

    def start(argv, **kwargs):
        path = Path(argv[argv.index("--mount") + 1].split("src=", 1)[1].split(",dst=", 1)[0])
        raw = feedback(mode)
        if mutation == "malformed":
            raw["state"] = raw["state"].replace("header:", "missing_header:")
        elif mutation == "nonfinite":
            raw["effort"] = raw["effort"].replace(
                "- 1.2" if mode == "enabled" else "- 0.0", "- .inf"
            )
        elif mutation == "held":
            raw["state"] = raw["state"].replace("- 0.8", "- 0.2")
        elif mutation == "actuation":
            raw["effort"] = raw["effort"].replace(
                "- 1.2" if mode == "enabled" else "- 0.0", "- 0.0" if mode == "enabled" else "- 0.1"
            )
        write_motion_artifacts(path, mode, admitted_profile, raw)
        return FinishedProcess()

    monkeypatch.setattr(jobs.subprocess, "Popen", start)
    document = jobs.start_motion_job(RUN_ID, image=LOCAL_IMAGE_ID, command=(mode,)).result()
    passed = mutation == "none"
    assert document["fixture_admission"] == ("PASSED" if passed else "REFUSED")
    assert document["verdict"] == ("GO" if passed and mode == "enabled" else "NO-GO")
    assert document["physical_conformance"] == document["simulation_validity"] == "NOT_SCORED"


@pytest.mark.parametrize("occupied", ["directory", "file", "symlink"])
def test_public_start_refuses_foreign_evidence_target(tmp_path, admitted_profile, occupied):
    target = tmp_path / "foreign"
    if occupied == "directory":
        target.mkdir()
    elif occupied == "file":
        target.write_text("foreign", encoding="utf-8")
    else:
        target.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="fresh absolute"):
        jobs.start_motion_job(
            RUN_ID, image=LOCAL_IMAGE_ID, command=("enabled",), evidence_dir=target
        )


def test_motion_closure_batches_exact_selectors_and_compares_identity_sets():
    dockerfile = (MOTION / "Dockerfile").read_text(encoding="utf-8")
    readme = (MOTION / "README.md").read_text(encoding="utf-8")

    assert "xargs -r -n 64 apt-cache show" in dockerfile
    assert "apt-show-selectors.txt" in dockerfile
    assert "expected-package-version-architecture.tsv" in dockerfile
    assert "actual-package-version-architecture.tsv" in dockerfile
    assert "cmp -s /opt/lockwire/receipt/expected-package-version-architecture.tsv" in dockerfile
    assert "while IFS=\"$(printf '\\t')\" read -r package version _" not in dockerfile
    assert "dpkg-query(1)" in readme and "apt-cache(8)" in readme
    selectors = (
        "ros-jazzy-gz-ros2-control=1.2.20-1noble.20260904.032933",
        "ros-jazzy-joint-trajectory-controller=4.42.1-1noble.20260904.012550",
        "ros-jazzy-ros-gz-sim=1.0.24-1noble.20260904.032840",
        "ros-jazzy-gz-sim-vendor=0.0.13-1noble.20260904.022149",
    )
    assert all(dockerfile.count(selector) == 1 for selector in selectors)
    assert dockerfile.count("$LOCKWIRE_E5_9_DIRECT_SELECTORS") == 2
    download_selectors = dockerfile.split("install --download-only --no-install-recommends -y", 1)[
        1
    ].split("&& deb_count", 1)[0]
    install_selectors = dockerfile.split("--no-download install --no-install-recommends -y", 1)[
        1
    ].split("&& printf", 1)[0]
    assert download_selectors.replace("\\", "").split() == ["$LOCKWIRE_E5_9_DIRECT_SELECTORS"]
    assert install_selectors.replace("\\", "").split() == ["$LOCKWIRE_E5_9_DIRECT_SELECTORS"]


def test_direct_artifact_verifier_binds_selectors_and_control_metadata(tmp_path):
    dockerfile = (MOTION / "Dockerfile").read_text(encoding="utf-8")
    script = dockerfile.split("# E5_9_DIRECT_VERIFY_PY_BEGIN", 1)[1].split(
        "# E5_9_DIRECT_VERIFY_PY_END", 1
    )[0]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dpkg_deb = fake_bin / "dpkg-deb"
    fake_dpkg_deb.write_text(
        """#!/bin/sh
test "$1" = -f || exit 64
cat "$2.control"
""",
        encoding="utf-8",
    )
    fake_dpkg_deb.chmod(0o755)

    def write_artifact(path, payload, package, version, architecture="arm64"):
        path.write_bytes(payload)
        Path(f"{path}.control").write_text(
            f"Package: {package}\nVersion: {version}\nArchitecture: {architecture}\n",
            encoding="utf-8",
        )

    def run(case, mutation):
        case_dir = tmp_path / case
        archives = case_dir / "archives"
        archives.mkdir(parents=True)
        artifacts = {
            "alpha": archives / "alpha_1_arm64.deb",
            "beta": archives / "beta_2_arm64.deb",
        }
        write_artifact(artifacts["alpha"], b"alpha artifact", "alpha", "1")
        write_artifact(artifacts["beta"], b"beta artifact", "beta", "2")
        transitive = archives / "alpha-looking-transitive.deb"
        write_artifact(transitive, b"gamma artifact", "gamma", "3", "all")
        records = {
            package: (
                f"{package}={'1' if package == 'alpha' else '2'} architecture=arm64 "
                f"sha256={hashlib.sha256(path.read_bytes()).hexdigest()} size={path.stat().st_size}"
            )
            for package, path in artifacts.items()
        }
        state = {"selectors": "alpha=1 beta=2"}
        mutation(artifacts, records, state)
        lock = case_dir / "packages.lock"
        lock.write_text("\n".join(records.values()) + "\n", encoding="utf-8")
        env = os.environ | {
            "LOCKWIRE_E5_9_LOCK_PATH": str(lock),
            "LOCKWIRE_E5_9_ARCHIVES": str(archives),
            "LOCKWIRE_E5_9_DIRECT_SELECTORS": state["selectors"],
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }
        return subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env
        )

    assert run("valid", lambda _artifacts, _records, _state: None).returncode == 0

    unlisted = run(
        "unlisted-selector",
        lambda _artifacts, _records, state: state.update(selectors="alpha=1 beta=2 gamma=3"),
    )
    assert unlisted.returncode != 0 and "do not exactly match lock pins" in unlisted.stderr

    duplicate_selector = run(
        "duplicate-selector",
        lambda _artifacts, _records, state: state.update(selectors="alpha=1 alpha=1 beta=2"),
    )
    assert (
        duplicate_selector.returncode != 0
        and "duplicate direct selector" in duplicate_selector.stderr
    )

    unrequested_pin = run(
        "unrequested-pin",
        lambda _artifacts, records, _state: records.update(
            gamma="gamma=3 architecture=arm64 sha256=" + "0" * 64 + " size=0"
        ),
    )
    assert (
        unrequested_pin.returncode != 0
        and "do not exactly match lock pins" in unrequested_pin.stderr
    )

    missing = run("missing", lambda artifacts, _records, _state: artifacts["beta"].unlink())
    assert missing.returncode != 0 and "missing direct artifacts" in missing.stderr

    def add_extra(artifacts, _records, _state):
        duplicate = artifacts["alpha"].parent / "duplicate.deb"
        write_artifact(duplicate, artifacts["alpha"].read_bytes(), "alpha", "1")

    extra = run("extra", add_extra)
    assert extra.returncode != 0 and "duplicate direct artifact" in extra.stderr

    def rename_direct(artifacts, _records, _state):
        renamed = artifacts["alpha"].parent / "renamed.deb"
        artifacts["alpha"].rename(renamed)
        Path(f"{artifacts['alpha']}.control").rename(Path(f"{renamed}.control"))

    renamed = run("renamed", rename_direct)
    assert renamed.returncode != 0 and "renamed direct artifact" in renamed.stderr

    def change_package(artifacts, _records, _state):
        Path(f"{artifacts['alpha']}.control").write_text(
            "Package: gamma\nVersion: 1\nArchitecture: arm64\n", encoding="utf-8"
        )

    wrong_package = run("package", change_package)
    assert wrong_package.returncode != 0 and "identity mismatch" in wrong_package.stderr

    def change_version(artifacts, _records, _state):
        Path(f"{artifacts['alpha']}.control").write_text(
            "Package: alpha\nVersion: 9\nArchitecture: arm64\n", encoding="utf-8"
        )

    wrong_version = run("version", change_version)
    assert wrong_version.returncode != 0 and "identity mismatch" in wrong_version.stderr

    def change_architecture(artifacts, _records, _state):
        Path(f"{artifacts['alpha']}.control").write_text(
            "Package: alpha\nVersion: 1\nArchitecture: amd64\n", encoding="utf-8"
        )

    wrong_architecture = run("architecture", change_architecture)
    assert wrong_architecture.returncode != 0 and "identity mismatch" in wrong_architecture.stderr

    def change_hash(artifacts, _records, _state):
        artifacts["alpha"].write_bytes(b"Alpha artifact")

    wrong_hash = run("hash", change_hash)
    assert wrong_hash.returncode != 0 and "sha256 mismatch" in wrong_hash.stderr

    def change_size(artifacts, _records, _state):
        artifacts["alpha"].write_bytes(b"alpha artifact!")

    wrong_size = run("size", change_size)
    assert wrong_size.returncode != 0 and "size mismatch" in wrong_size.stderr
