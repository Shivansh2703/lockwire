"""The standing check: feet, stance, and leg torques."""

import math
import re
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from lockwire import standing, statics
from lockwire.checks import run_record_checks
from lockwire.schema import Contract, RobotInfo
from lockwire.sdf import parse_sdf
from lockwire.urdf import parse_urdf


def _feet(*shapes):
    body = "".join(
        f'<collision name="{n}"><origin xyz="0 0 -0.5" rpy="{rpy}"/><geometry>{g}</geometry></collision>'
        for n, g, rpy in shapes
    )
    return standing.find_feet(ET.fromstring(f'<robot><link name="shin">{body}</link></robot>'))


def test_feet_are_collisions_named_foot_any_case_and_a_mesh_is_listed_unsupported():
    mesh = '<mesh filename="f.stl"/>'
    box = '<box size="1 1 1"/>'
    feet, skipped = _feet(
        ("FOOT", box, "0 0 0"),
        ("heel_foot", box, "0 0 0"),
        ("foot_l", mesh, "0 0 0"),
        ("pad", box, "0 0 0"),
    )
    assert [f.label for f in feet] == ["shin/FOOT", "shin/heel_foot"] and skipped == ["shin/foot_l"]


@pytest.mark.parametrize(
    "geometry, rpy, extent",
    [
        ('<sphere radius="0.1"/>', "0 0 0", 0.1),
        ('<box size="0.2 0.3 0.1"/>', "0.4 0.3 0.2", None),
        ('<cylinder radius="0.05" length="0.4"/>', "0.7 0 0", None),
    ],
)
def test_the_contact_point_is_the_lowest_point_of_the_shape(geometry, rpy, extent):
    (foot,), _ = _feet(("foot", geometry, rpy))
    link_rot = statics._rpy(0.1, -0.2, 0.3)  # the link itself is tilted too
    p = standing.lowest_point(foot, link_rot[None], np.array([[0.0, 0.0, 1.0]]))[0]
    rot = link_rot @ foot.rot
    centre = np.array([0.0, 0.0, 1.0]) + link_rot @ foot.xyz
    if extent is None:  # the shape's half height by its own formula, not the code's
        if foot.kind == "box":
            extent = float(np.abs(rot[2]) @ (np.array(foot.dims) / 2))
        else:
            az = abs(rot[2, 2])
            extent = 0.2 * az + 0.05 * math.sqrt(1 - az**2)
    assert p[2] == pytest.approx(centre[2] - extent, abs=1e-12)


def test_four_feet_share_the_weight_by_where_the_centre_of_mass_sits():
    feet = [(0.1, 0.05, 0), (0.1, -0.05, 0), (-0.1, 0.05, 0), (-0.1, -0.05, 0)]
    f, _ = standing.split_forces(feet, (0.01, 0.0, 0.5), 20.0)  # 10 mm ahead: front pair 55 %
    assert f == pytest.approx([5.5, 5.5, 4.5, 4.5])
    f, why = standing.split_forces(feet, (0.2, 0.0, 0.5), 20.0)
    assert f is None and "tips" in why
    assert standing.split_forces(feet[:2], (0.1, 0.0, 0), 20.0)[0] == pytest.approx([10.0, 10.0])


def test_a_four_footed_robot_gets_all_feet_two_diagonal_pairs_and_one_leg():
    feet = [(0.1, 0.05, 0), (0.1, -0.05, 0), (-0.1, 0.05, 0), (-0.1, -0.05, 0)]
    cases, margin = standing.load_cases(["fl", "fr", "hl", "hr"], feet, (0.01, 0.0, 0.5), 20.0)
    got = {c["case"]: c for c in cases}
    assert list(got) == ["all_feet", "diag_fl_hr", "diag_fr_hl", "one_leg"]
    assert got["diag_fl_hr"]["feet"] == ["fl", "hr"] and "line" in got["diag_fl_hr"]["label"]
    # t = (0.09 * 0.2 + 0.05 * 0.1) / 0.05 = 0.46 of the way from fl to hr
    assert got["diag_fl_hr"]["forces"] == pytest.approx([10.8, 9.2])
    assert got["one_leg"]["forces"] == [20.0] * 4 and margin == pytest.approx(0.05)


G = statics.STANDARD_G


@pytest.mark.parametrize(
    "name, foot",
    [("foot", 1), ("FOOT", 1), ("left_foot", 1), ("foot_fl", 1), ("FL_foot", 1), ("leftFoot", 1)]
    + [("left_foot_collision", 1), ("foot2", 1), ("foot_1", 1), ("footrest", 0), ("barefoot", 0)]
    + [("footprint", 0), ("base_footprint", 0), ("barefoot_pad", 0), ("", 0)],
)
def test_a_foot_is_a_collision_with_the_word_foot_in_its_name(name, foot):
    assert standing.is_foot_name(name) == bool(foot)


def _open_quadruped(
    tmp_path, hip_effort=2.0, knee_effort=2.0, foot="foot", base_x=0.0, mesh=None, fl_shape=None
):
    """Base 4 kg; each corner a hip then a knee (axes +y), thigh 0.2 kg, shin 0.1 kg whose CoM and
    foot sit 25 and 50 mm ahead of the knee axis. Hips at x = +/-0.1, y = +/-0.05."""
    links = f'<link name="base"><inertial><mass value="4"/><origin xyz="{base_x} 0 0"/><inertia ixx="1" iyy="1" izz="1" ixy="0" ixz="0" iyz="0"/></inertial></link>'
    joints = ""
    for n, (x, y) in {
        "fl": (0.1, 0.05),
        "fr": (0.1, -0.05),
        "hl": (-0.1, 0.05),
        "hr": (-0.1, -0.05),
    }.items():
        i = '<inertia ixx="1e-3" iyy="1e-3" izz="1e-3" ixy="0" ixz="0" iyz="0"/>'
        links += f'<link name="thigh_{n}"><inertial><mass value="0.2"/><origin xyz="0 0 -0.05"/>{i}</inertial></link>'
        links += f'<link name="shin_{n}"><inertial><mass value="0.1"/><origin xyz="0.025 0 -0.05"/>{i}</inertial>'
        shape = '<mesh filename="f.stl"/>' if n == mesh else '<sphere radius="0.01"/>'
        shape = fl_shape if n == "fl" and fl_shape else shape
        name = foot[n] if isinstance(foot, dict) else foot
        links += f'<collision name="{name}"><origin xyz="0.05 0 -0.1"/><geometry>{shape}</geometry></collision></link>'
        for j, (p, c, at, eff) in {
            "hip": ("base", "thigh", f"{x} {y} 0", hip_effort),
            "knee": ("thigh", "shin", "0 0 -0.1", knee_effort),
        }.items():
            joints += f'<joint name="{j}_{n}" type="revolute"><parent link="{p if j == "hip" else "thigh_" + n}"/><child link="{c}_{n}"/><origin xyz="{at}"/><axis xyz="0 1 0"/><limit lower="-1" upper="1" effort="{eff}" velocity="9"/></joint>'
    path = tmp_path / "quad.urdf"
    path.write_text(f'<robot name="q">{links}{joints}</robot>')
    return Contract(robot=RobotInfo(name="q"), model=parse_urdf(path)), tmp_path / "lockwire.yaml"


def _rows(report, joint):
    return {r["case"]: r for r in report.standing["rows"] if r["joint"] == joint}


def test_open_chain_quadruped_matches_the_hand_worked_stance_and_hip_torques(tmp_path):
    contract, where = _open_quadruped(tmp_path)
    report = run_record_checks(contract, contract_path=where)
    weight = 5.2 * G
    front = (0.01 / 5.2 + 0.05) / 0.2  # lever rule: (x_cm - x_hind) / 0.2
    f_front, f_hind = front * weight / 2, (1 - front) * weight / 2
    shin_gravity = 0.1 * G * 0.025  # the shin's weight 25 mm ahead of both axes
    rows = {n: _rows(report, f"hip_{n}") for n in ("fl", "hl")}
    assert rows["fl"]["all_feet"]["at_file_nm"] == pytest.approx(
        abs(shin_gravity - 0.05 * f_front), rel=0.01
    )
    assert rows["hl"]["all_feet"]["at_file_nm"] == pytest.approx(
        abs(shin_gravity - 0.05 * f_hind), rel=0.01
    )
    assert rows["fl"]["all_feet"]["foot_force_n"] == pytest.approx(f_front, rel=0.01)
    assert _rows(report, "knee_fl")["one_leg"]["at_file_nm"] == pytest.approx(
        abs(shin_gravity - 0.05 * weight), rel=0.01
    )


_DROP = math.sqrt(
    0.06**2 - 0.03**2
)  # the apex of two 60 mm bars on tips 60 mm apart: 51.96 mm down


def _five_bar_quadruped(tmp_path, effort=2.0, lower=-0.5, upper=0.5, foot="foot", base_x=0.01):
    """Four five-bar legs: cranks 50 mm straight down from hips 60 mm apart, 60 mm bars meeting at
    the foot sphere (r 10 mm). Base 4 kg with its CoM 10 mm ahead; cranks 0.03 kg, bars 0.05 kg."""

    def inertial(x, z, m):
        return f"<inertial><pose>{x} 0 {z} 0 0 0</pose><mass>{m}</mass></inertial>"

    limit = f"<limit><lower>{lower}</lower><upper>{upper}</upper><effort>{effort}</effort><velocity>8</velocity></limit>"
    body = f"<link name='base'>{inertial(base_x, 0, 4)}</link>"
    for n, (hx, hy) in {
        "fl": (0.1, 0.05),
        "fr": (0.1, -0.05),
        "hl": (-0.1, 0.05),
        "hr": (-0.1, -0.05),
    }.items():
        for s, side in (("r", 0.03), ("l", -0.03)):
            sign = 1 if s == "r" else -1
            body += f"<link name='crank_{s}_{n}'><pose>{hx + side} {hy} 0 0 0 0</pose>{inertial(0, -0.025, 0.03)}</link>"
            shape = f"<collision name='{foot}'><pose>0.03 0 {-_DROP} 0 0 0</pose><geometry><sphere><radius>0.01</radius></sphere></geometry></collision>"
            body += f"<link name='lower_{s}_{n}'><pose>{hx + side} {hy} -0.05 0 0 0</pose>{inertial(-sign * 0.015, -_DROP / 2, 0.05)}{shape if s == 'l' else ''}</link>"
            body += f"<joint name='{'a' if s == 'r' else 'b'}_{n}' type='revolute'><parent>base</parent><child>crank_{s}_{n}</child><axis><xyz>0 1 0</xyz>{limit}</axis></joint>"
            body += f"<joint name='elbow_{s}_{n}' type='revolute'><parent>crank_{s}_{n}</parent><child>lower_{s}_{n}</child><axis><xyz>0 1 0</xyz><limit><lower>-3.1</lower><upper>3.1</upper><velocity>20</velocity></limit></axis></joint>"
        body += f"<joint name='closure_{n}' type='revolute'><parent>lower_r_{n}</parent><child>lower_l_{n}</child><pose>0.03 0 {-_DROP} 0 0 0</pose><axis><xyz>0 1 0</xyz></axis></joint>"
    path = tmp_path / "quad5.sdf"
    path.write_text(f"<sdf version='1.7'><model name='q5'>{body}</model></sdf>")
    return Contract(robot=RobotInfo(name="q5"), model=parse_sdf(path)), tmp_path / "lockwire.yaml"


def test_five_bar_quadruped_matches_the_hand_worked_lever_arms_and_hip_torques(tmp_path):
    """By hand. Crank tip r = (0.03 - 0.05 sin a, -0.05 cos a); the apex P is 60 mm from both tips,
    so (P - T_r) . (dP - dT_r) = 0 and (P - T_l) . dP = 0 when only a moves. At a = b = 0
    P - T_r = (-0.03, -D), P - T_l = (0.03, -D), D = 0.05196: dPx = (D / 0.03) dPz, then
    -0.06 D dPz... = -0.03 * 0.05 da gives dPz/da = -0.03 * 0.05 / (2 D) = -0.014434 m/rad, the same
    for b. Gravity: only the two bars' centres move (each half the apex's rise), so
    Q = -dU/da = -2 * 0.05 g * (-0.014434 / 2) = +0.0070775 N*m. The base CoM sits 10 mm ahead, so
    the CoM is 4 * 0.01 / 4.64 ahead of centre and the front pair carries (x + 0.1) / 0.2 of W."""
    contract, where = _five_bar_quadruped(tmp_path)
    report = run_record_checks(contract, contract_path=where)
    weight, lever = 4.64 * G, -0.03 * 0.05 / (2 * _DROP)
    front = (0.04 / 4.64 + 0.1) / 0.2
    for leg, share in (("fl", front), ("hl", 1 - front)):
        force = share * weight / 2
        want = abs(0.05 * G * -lever + force * lever)  # Q = -2 * 0.05 g * (lever / 2)
        for hip in ("a", "b"):
            row = _rows(report, f"{hip}_{leg}")["all_feet"]
            assert row["foot_force_n"] == pytest.approx(force, rel=0.01)
            assert row["at_file_nm"] == pytest.approx(want, rel=0.01)
            assert row["peak_nm"] >= row["at_file_nm"] and set(row["peak_pose_deg"]) == {
                f"a_{leg}",
                f"b_{leg}",
            }


def _findings(report, contains="/standing/"):
    return {f.quantity: f for f in report if contains in f.quantity}


def test_all_feet_over_the_limit_at_the_file_pose_is_a_violation_the_rest_are_warnings(tmp_path):
    # hind hips carry 0.92 N*m with all feet down (hand-worked above); front ones 0.31
    report = run_record_checks(*_open_quadruped(tmp_path, hip_effort=0.5))
    got = _findings(report)
    hind = got["model/joints/hip_hl/standing/all_feet"]
    assert hind.severity == "violation" and "cannot stand as drawn" in hind.observed
    assert got["model/standing/one_leg"].severity == "warning"  # 2.5 N*m alone
    swept = got["model/standing/all_feet"]  # front hips: 0.31 here, more in the sweep
    assert swept.severity == "warning" and swept.observed.startswith("hip_f")


def test_over_only_at_another_pose_or_only_on_a_pair_is_a_warning_naming_case_and_pose(tmp_path):
    rows = _rows(run_record_checks(*_open_quadruped(tmp_path, 9, 9)), "hip_fl")
    at, peak = rows["all_feet"]["at_file_nm"], rows["all_feet"]["peak_nm"]
    assert peak > at  # the sweep finds a worse pose than the file's
    warn = _findings(run_record_checks(*_open_quadruped(tmp_path, (at + peak) / 2, 9)))
    swept = warn["model/standing/all_feet"]
    assert swept.severity == "warning" and "held constant while the" in swept.observed
    assert "hip_f" in swept.observed and "all feet down" in swept.observed
    pair = rows["diag_fl_hr"]["peak_nm"]  # a limit between the all-feet peak and the pair's
    assert pair > peak
    warn = _findings(run_record_checks(*_open_quadruped(tmp_path, (peak + pair) / 2, 9)))
    assert warn["model/standing/diagonal_pair"].severity == "warning"
    assert "hip_fl" not in getattr(warn.get("model/standing/all_feet"), "observed", "")


def test_a_centre_of_mass_outside_the_feet_is_reported_as_tips(tmp_path):
    report = run_record_checks(*_open_quadruped(tmp_path, base_x=0.5))
    assert _findings(report, "standing/tips")["model/standing/tips"].severity == "warning"
    assert "all_feet" not in {r["case"] for r in report.standing["rows"]}
    assert report.standing["stance"]["cases"][0]["forces"] is None


def _quad_edit(tmp_path, mesh=None, keep=("fl", "fr", "hl", "hr")):
    names = {n: "foot" if n in keep else "pad" for n in ("fl", "fr", "hl", "hr")}
    return run_record_checks(*_open_quadruped(tmp_path, foot=names, mesh=mesh))


def test_a_mesh_foot_stops_the_scoring_with_one_line_naming_it(tmp_path):
    report = _quad_edit(tmp_path, "fl")
    assert report.standing["rows"] == [] and not _findings(report, "standing/tips")
    line = _findings(report, "model/standing")["model/standing"]
    assert line.severity == "not_scored" and "shin_fl's foot is a mesh" in line.observed
    assert "named foot or on a link named foot or toe, to score standing" in line.observed
    assert "shin_fl's foot is a mesh" in report.standing["stance"]["reason"]
    assert not _findings(report, "/standing/")  # no joint rows or warnings


@pytest.mark.parametrize(
    "keep, words", [(("fl", "fr"), "mm beside the line through the feet"), (("fl",), "no area")]
)
def test_feet_on_a_line_with_the_centre_of_mass_off_it_tip_with_true_words(tmp_path, keep, words):
    report = _quad_edit(tmp_path, keep=keep)
    tips = _findings(report, "standing/tips")["model/standing/tips"].observed
    assert words in tips and "support polygon" not in tips
    assert "all_feet" not in {r["case"] for r in report.standing["rows"]}


def test_two_feet_with_the_centre_of_mass_beside_the_line_tip_past_ten_millimetres():
    feet = [(0.1, 0.05, 0), (0.1, -0.05, 0)]
    near = standing.load_cases(["a", "b"], feet, (0.095, 0.0, 0.5), 20.0)[0][0]
    far = standing.load_cases(["a", "b"], feet, (0.085, 0.0, 0.5), 20.0)[0][0]
    assert near["forces"] == pytest.approx([10.0, 10.0]) and "5 mm beside" in near["note"]
    assert far["forces"] is None and "15 mm beside the line" in far["note"]


def test_a_passed_standing_row_does_not_run_the_case_into_the_limit(tmp_path):
    report = run_record_checks(*_open_quadruped(tmp_path, 99, 99))
    row = next(r for r in report.scored_records if r.quantity.endswith("hip_fl/standing/all_feet"))
    assert row.observed.startswith("worst ") and ", all feet down" in row.observed
    assert " over the swept leg poses (" in row.observed and " at the drawn pose)" in row.observed
    one = next(r for r in report.scored_records if r.quantity.endswith("hip_fl/standing/one_leg"))
    assert "one leg carrying everything (an upper bound)" in one.observed
    from lockwire.page import _scored_title

    title = _scored_title(Contract(robot=RobotInfo(name="q")), row)
    assert title.startswith("hip_fl: worst ") and "; limit 99.00 N*m, headroom " in title


@pytest.mark.parametrize(
    "shape", ['<box size="0 0 0"/>', "<sphere/>", '<sphere radius="0"/>', '<box size="0.1 0.1"/>']
)
def test_a_foot_with_no_size_stops_the_scoring_like_a_mesh_foot(tmp_path, shape):
    report = run_record_checks(*_open_quadruped(tmp_path, fl_shape=shape))
    assert report.standing["rows"] == [] and not _findings(report, "/standing/")
    line = _findings(report, "model/standing")["model/standing"]
    assert (
        line.severity == "not_scored"
        and "shin_fl's foot is a mesh or a shape without a size" in line.observed
    )


@pytest.mark.parametrize("shape", ['<box size="0 0 0.02"/>', '<sphere radius="0.01"/>'])
def test_a_foot_with_a_real_size_is_still_scored(tmp_path, shape):
    assert run_record_checks(*_open_quadruped(tmp_path, fl_shape=shape)).standing["rows"]


def test_a_robot_with_no_foot_collision_is_untouched(tmp_path, monkeypatch):
    from lockwire import checks

    contract, where = _open_quadruped(tmp_path, foot="pad")
    got = run_record_checks(contract, contract_path=where)
    assert got.standing["rows"] == [] and got.standing["stance"]["feet"] == []
    assert "foot" in got.standing["stance"]["reason"]
    monkeypatch.setattr(checks, "check_standing_sweep", lambda *a, **k: [])
    off = run_record_checks(contract, contract_path=where)
    assert (list(got), got.scored) == (list(off), off.scored)


def test_check_all_prints_the_standing_block_and_the_data_carries_the_rows(tmp_path):
    import json

    from typer.testing import CliRunner

    from lockwire.cli import app

    _open_quadruped(tmp_path)
    out = tmp_path / "o.json"
    res = CliRunner().invoke(
        app, ["check", str(tmp_path / "quad.urdf"), "--all", "--json", str(out)]
    )
    assert (
        "Standing: 4 feet" in res.output
        and "hip_hl all_feet: 0.92 N*m at the file's pose" in res.output
    )
    assert res.output.rstrip().count("not covered: sideways foot forces") == 1
    forces = json.loads(out.read_text())["forces"]
    assert len(forces["standing"]) == 4 * 2 * 3 and forces["stance"]["feet"][0]["height_range_m"]
    plain = CliRunner().invoke(app, ["check", str(tmp_path / "quad.urdf")])
    assert "Standing:" not in plain.output


@pytest.mark.parametrize("example", ["arm", "rover"])
def test_starters_without_feet_print_exactly_what_they_printed(example, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lockwire import checks
    from lockwire.cli import app

    lock, run = tmp_path / "lockwire.yaml", CliRunner()
    assert run.invoke(app, ["start", "--example", example, "--out", str(lock)]).exit_code == 0

    def text():
        return [
            (r.exit_code, r.output)
            for r in (run.invoke(app, ["check", str(lock), *a]) for a in ([], ["--all"]))
        ]

    got = text()
    monkeypatch.setattr(checks, "check_standing_sweep", lambda *a, **k: [])
    assert got == text() and "Standing:" not in got[1][1]


@pytest.mark.parametrize(
    "name, foot",
    [("FL_foot", 1), ("LF_FOOT", 1), ("RR_foot", 1), ("left_toe_link", 1), ("leftFoot", 1)]
    + [("toe2", 1), ("foot", 1), ("base_footprint", 0), ("${namespace}base_footprint", 0)]
    + [("football", 0), ("tiptoe", 0), ("footrest", 0), ("FL_calf", 0), ("toes", 0), ("", 0)],
)
def test_a_foot_link_has_the_word_foot_or_toe_in_its_name(name, foot):
    assert standing.is_foot_link_name(name) == bool(foot)


def _tree(links: dict[str, str], joints: list[tuple[str, str, str]]):
    """find_feet on links {name: inner xml} joined by (type, parent, child) joints."""
    body = "".join(f'<link name="{n}">{x}</link>' for n, x in links.items()) + "".join(
        f'<joint name="{p}_{c}" type="{t}"><parent link="{p}"/><child link="{c}"/></joint>'
        for t, p, c in joints
    )
    return standing.find_feet(ET.fromstring(f"<robot>{body}</robot>"))


BALL = '<collision><geometry><sphere radius="0.01"/></geometry></collision>'


def _legs(**ends):
    """base with one two-joint leg (hip, knee) per keyword, ending in the given link names."""
    links, joints = {"base": BALL}, []
    for leg, tail in ends.items():
        links |= {f"{leg}_thigh": "", f"{leg}_shin": "", **{n: BALL for n, _ in tail}}
        joints += [
            ("revolute", "base", f"{leg}_thigh"),
            ("revolute", f"{leg}_thigh", f"{leg}_shin"),
        ]
        up = f"{leg}_shin"
        for n, t in tail:
            joints.append((t, up, n))
            up = n
    return links, joints


def test_feet_by_link_name_are_leg_ends_with_a_moving_joint_above_and_none_below():
    # link names avoid Pro command words (such as "tip"): the public-copy drop pass reads a tuple starting with one as a command
    links, joints = _legs(
        l=[("l_foot", "revolute"), ("l_toe", "revolute")],
        r=[("r_toe", "revolute"), ("r_nub", "fixed")],
    )
    feet, skipped = _tree(links, joints)  # l_foot moves l_toe below it: not a leg end
    assert sorted(f.label for f in feet) == ["l_toe", "r_toe"] and skipped == []
    assert all(f.by_link for f in feet)


def test_a_toe_fixed_to_its_foot_is_the_same_foot():
    links, joints = _legs(
        l=[("left_foot", "revolute"), ("left_toe", "fixed")],
        r=[("right_foot", "revolute"), ("right_toe", "fixed")],
    )
    feet, _ = _tree(links, joints)  # a humanoid: one foot per leg, both shapes are candidates
    assert [(f.label, f.link) for f in feet] == [
        ("left_foot", "left_foot"), ("left_foot", "left_toe"),
        ("right_foot", "right_foot"), ("right_foot", "right_toe"),
    ]  # fmt: skip


def test_a_collision_named_foot_wins_over_foot_link_names():
    named = '<collision name="foot"><geometry><sphere radius="0.01"/></geometry></collision>'
    links = {"base": "", "shin": named, "fl_foot": BALL, "fr_foot": BALL}
    joints = [("revolute", "base", n) for n in ("shin", "fl_foot", "fr_foot")]
    feet, _ = _tree(links, joints)
    assert [f.label for f in feet] == ["shin"] and not feet[0].by_link


@pytest.mark.parametrize(
    "links, joints",
    [  # a rover with foot pads bolted to its body; an arm with one stray toe at its tip
        ({"base": BALL, "l_foot": BALL, "r_foot": BALL, "wheel": BALL},
         [("fixed", "base", "l_foot"), ("fixed", "base", "r_foot"), ("continuous", "base", "wheel")]),
        ({"base": BALL, "arm": BALL, "toe": BALL},
         [("revolute", "base", "arm"), ("revolute", "arm", "toe")]),
        ({"base": "", "l_foot": "", "r_foot": ""},  # no collision anywhere (as an MJCF view)
         [("revolute", "base", "l_foot"), ("revolute", "base", "r_foot")]),
        # a fixed-base arm whose wrist carries foot_plate and toe_link (the feet meet on the wrist)
        ({"base": BALL, "upper": "", "wrist": "", "foot_plate": BALL, "toe_link": BALL},
         [("revolute", "base", "upper"), ("revolute", "upper", "wrist"),
          ("revolute", "wrist", "foot_plate"), ("revolute", "wrist", "toe_link")]),
        # a gripper whose fingers are named finger_toe_left and finger_toe_right
        ({"base": BALL, "arm": "", "hand": "", "finger_toe_left": BALL, "finger_toe_right": BALL},
         [("revolute", "base", "arm"), ("revolute", "arm", "hand"),
          ("prismatic", "hand", "finger_toe_left"), ("prismatic", "hand", "finger_toe_right")]),
        # an arm's hand with two two-joint fingers ending in l_toe and r_toe (they meet on the hand)
        ({"arm": "", **_legs(l=[("l_toe", "revolute")], r=[("r_toe", "revolute")])[0]},
         [("revolute", "arm", "base"), *_legs(l=[("l_toe", "revolute")], r=[("r_toe", "revolute")])[1]]),
        # two legs of one joint each: not legs the robot stands on
        ({"base": BALL, "l_foot": BALL, "r_foot": BALL},
         [("revolute", "base", "l_foot"), ("revolute", "base", "r_foot")]),
        # a two-legged robot bolted to the world
        ({"world": "", **_legs(l=[("l_foot", "revolute")], r=[("r_foot", "revolute")])[0]},
         [("fixed", "world", "base"), *_legs(l=[("l_foot", "revolute")], r=[("r_foot", "revolute")])[1]]),
    ],
)  # fmt: skip
def test_a_wheeled_or_fixed_base_robot_gets_no_feet_from_a_stray_name(links, joints):
    assert _tree(links, joints) == ([], [])


def _link_quad(tmp_path, extra="", bare=None, edit=lambda t: t, base_x=0.0):
    """_open_quadruped with unnamed foot collisions on shins named fl_foot ...; ``extra`` adds a
    collision to every foot link; ``bare`` drops that leg's collision."""
    _open_quadruped(tmp_path, foot="", base_x=base_x)
    path = tmp_path / "quad.urdf"
    text = re.sub(r"shin_(fl|fr|hl|hr)", r"\1_foot", path.read_text())
    text = text.replace('<collision name="">', extra + "<collision>")
    if bare:
        text = re.sub(rf'(<link name="{bare}_foot">.*?)<collision>.*?</collision>', r"\1", text)
    path.write_text(edit(text))
    return run_record_checks(
        Contract(robot=RobotInfo(name="q"), model=parse_urdf(path)),
        contract_path=tmp_path / "lockwire.yaml",
    )


def test_feet_found_by_link_name_score_exactly_as_feet_named_by_collision(tmp_path):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    named = run_record_checks(*_open_quadruped(tmp_path / "a"))
    by_link = _link_quad(tmp_path / "b")
    rename = {f"shin_{n}": f"{n}_foot" for n in ("fl", "fr", "hl", "hr")}
    want = [
        {**r, "leg": rename[r["leg"]], "feet": [rename[f] for f in r["feet"]]}
        for r in named.standing["rows"]
    ]
    assert by_link.standing["rows"] == want and len(want) == 4 * 2 * 3


def test_of_a_foot_links_several_shapes_the_lowest_is_the_foot(tmp_path):
    shank = '<collision><origin xyz="0 0 -0.05"/><geometry><cylinder radius="0.01" length="0.05"/></geometry></collision>'
    feet = _link_quad(tmp_path, extra=shank).standing["stance"]["feet"]
    assert [f["shape"] for f in feet] == ["sphere"] * 4


def test_a_foot_link_with_no_collision_stops_the_scoring_with_true_words(tmp_path):
    report = _link_quad(tmp_path, bare="fl")
    line = _findings(report, "model/standing")["model/standing"]
    assert line.severity == "not_scored" and "fl_foot's foot has no collision" in line.observed
    assert report.standing["rows"] == []


def test_a_robot_floating_in_the_world_still_stands_on_its_feet():
    links, joints = _legs(l=[("l_foot", "revolute")], r=[("r_foot", "revolute")])
    feet, _ = _tree({"world": "", **links}, [("floating", "world", "base"), *joints])
    assert sorted(f.label for f in feet) == ["l_foot", "r_foot"]


def test_a_quadruped_with_a_fixed_toe_on_each_foot_scores_as_without(tmp_path):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    toe = '<link name="{n}_toe"><collision><origin xyz="0 0 0.05"/><geometry><sphere radius="0.01"/></geometry></collision></link><joint name="{n}_toe_fix" type="fixed"><parent link="{n}_foot"/><child link="{n}_toe"/></joint>'
    plain = _link_quad(tmp_path / "a")
    toed = _link_quad(
        tmp_path / "b",
        edit=lambda t: t.replace(
            "</robot>", "".join(toe.format(n=n) for n in ("fl", "fr", "hl", "hr")) + "</robot>"
        ),
    )
    assert toed.standing["rows"] == plain.standing["rows"] and len(plain.standing["rows"]) == 24
    assert "model/joints/standing" not in {f.quantity for f in toed}  # no "shared by several legs"


def test_feet_by_name_on_one_side_only_are_scored_as_drawn_and_tip(tmp_path):
    report = _link_quad(tmp_path, edit=lambda t: re.sub(r"(fr|hr)_foot", r"\1_shin", t))
    assert [f["name"] for f in report.standing["stance"]["feet"]] == ["fl_foot", "hl_foot"]
    assert _findings(report, "standing/tips")["model/standing/tips"].severity == "warning"
    assert "model/standing" not in {f.quantity for f in report}  # scored, not "not all its feet"


def _standing_set(report, text=False):
    """The standing sweep's findings (not check_standing_load's, which reads leg-end names)."""
    return {
        (f.quantity, f.severity) + ((f.observed,) if text else ())
        for f in report
        if f.quantity == "model/standing" or "/standing/" in f.quantity
    }


def test_a_tipping_quadruped_gets_the_same_findings_whether_feet_are_named_by_link_or_collision(
    tmp_path,
):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    named = run_record_checks(*_open_quadruped(tmp_path / "a", base_x=0.5))
    by_link = _link_quad(tmp_path / "b", base_x=0.5)
    assert "model/standing/tips" in {q for q, _ in _standing_set(named)}
    assert _standing_set(by_link) == _standing_set(named)


def _biped(tmp_path, foot_attr):
    """Two two-joint legs; each foot link a box with a toe box fixed ahead of it, 5 mm higher,
    so the foot box is the foot body's lowest shape. ``foot_attr`` names the foot box only."""
    i = '<inertia ixx="1e-3" iyy="1e-3" izz="1e-3" ixy="0" ixz="0" iyz="0"/>'
    links = f'<link name="base"><inertial><mass value="4"/><origin xyz="0.02 0 0"/>{i}</inertial></link>'
    joints = ""
    for s, y in (("left", 0.1), ("right", -0.1)):
        for n in ("thigh", "shin"):
            links += f'<link name="{s}_{n}"><inertial><mass value="0.2"/><origin xyz="0 0 -0.1"/>{i}</inertial></link>'
        links += f'<link name="{s}_foot"><collision {foot_attr}><origin xyz="0 0 -0.01"/><geometry><box size="0.1 0.08 0.02"/></geometry></collision></link>'
        links += f'<link name="{s}_toe"><collision><origin xyz="0 0 -0.005"/><geometry><box size="0.05 0.08 0.02"/></geometry></collision></link>'
        for j, p, c, at, t in (
            ("hip", "base", "thigh", f"0 {y} 0", "revolute"),
            ("knee", f"{s}_thigh", "shin", "0 0 -0.2", "revolute"),
            ("ankle", f"{s}_shin", "foot", "0 0 -0.2", "fixed"),
            ("toefix", f"{s}_foot", "toe", "0.075 0 0", "fixed"),
        ):
            joints += f'<joint name="{s}_{j}" type="{t}"><parent link="{p}"/><child link="{s}_{c}"/><origin xyz="{at}"/><axis xyz="0 1 0"/><limit lower="-1" upper="1" effort="50" velocity="9"/></joint>'
    path = tmp_path / "biped.urdf"
    path.write_text(f'<robot name="b">{links}{joints}</robot>')
    return run_record_checks(
        Contract(robot=RobotInfo(name="b"), model=parse_urdf(path)),
        contract_path=tmp_path / "l.yaml",
    )


def test_a_biped_with_fixed_toes_gets_the_same_findings_by_link_name_as_with_its_lowest_shape_named_foot(
    tmp_path,
):
    """Same result means: feet by link name equal the same model with the lowest shape of each
    foot body named foot (one contact per leg)."""
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    named, by_link = _biped(tmp_path / "a", 'name="foot"'), _biped(tmp_path / "b", "")
    assert [f["name"] for f in by_link.standing["stance"]["feet"]] == ["left_foot", "right_foot"]
    assert _standing_set(by_link, text=True) == _standing_set(named, text=True)
    assert "model/standing/tips" in {q for q, _, _ in _standing_set(named, text=True)}
    assert by_link.standing["rows"] == named.standing["rows"]


def test_standing_warnings_are_one_per_stance_kind_naming_the_worst_and_the_count(tmp_path):
    report = run_record_checks(*_open_quadruped(tmp_path, hip_effort=0.5, knee_effort=0.5))
    rows, warn = report.standing["rows"], [f for f in report if f.severity == "warning"]
    kinds = {
        "all_feet": "all_feet",
        "diag_fl_hr": "diagonal_pair",
        "diag_fr_hl": "diagonal_pair",
        "one_leg": "one_leg",
    }
    for kind in ("all_feet", "diagonal_pair", "one_leg"):
        over = [r for r in rows if kinds[r["case"]] == kind and r["peak_nm"] > r["limit_nm"]]
        over = [
            r for r in over if not (r["case"] == "all_feet" and r["at_file_nm"] > r["limit_nm"])
        ]
        mine = [f for f in warn if f.quantity == f"model/standing/{kind}"]
        worst = max(over, key=lambda r: r["share"])
        n = len({r["joint"] for r in over}) - 1
        assert len(mine) == 1 and mine[0].observed.startswith(
            f"{worst['joint']} would carry {worst['peak_nm']:.2f} N*m"
        )
        span = {"all_feet": "in this case", "diagonal_pair": "across the diagonal-pair cases"}
        span |= {"one_leg": "across the one-leg cases"}
        got = re.search(
            rf"; (\d+) more joints are over the limit {span[kind]}: ([^(]*) \(", mine[0].observed
        )
        assert got and int(got[1]) == n and "..." not in mine[0].observed
        assert set(got[2].split(", ")) == {r["joint"] for r in over} - {worst["joint"]}
    assert not [
        f for f in warn if f.quantity.startswith("model/joints/") and "/standing/" in f.quantity
    ]
    assert len(rows) == 24  # every joint stays in the detail


def test_a_grouped_all_feet_warning_says_when_its_pose_is_past_a_half_turn():
    from lockwire.checks import _standing_group

    row = {"joint": "kfe", "case": "all_feet", "feet": ["a", "b"], "leg": "a", "foot_force_n": 10.0}
    row |= {"at_file_nm": 1.0, "peak_nm": 9.0, "limit_nm": 8.0, "share": 9 / 8}
    cases = [{"case": "all_feet", "label": "all feet down"}]
    far = _standing_group(
        "all_feet", [{**row, "peak_pose_deg": {"hfe": 90.0, "kfe": 382.0}}], cases
    )
    near = _standing_group(
        "all_feet", [{**row, "peak_pose_deg": {"hfe": 90.0, "kfe": 40.0}}], cases
    )
    assert "That pose has kfe at 382 deg, more than a half turn" in far.observed
    assert "half turn" not in near.observed and "more joint" not in near.observed
