from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.urdf import parse_urdf
from lockwire.webots import parse_webots

FIX = Path(__file__).parent / "fixtures" / "webots"
WBT = FIX / "two_link.wbt"


def _rows(model):
    links = [(x.name, x.mass.value if x.mass else None, x.com, x.inertia and x.inertia.moments_kgm2) for x in model.links]  # fmt: skip
    joints = [(j.name, j.type, j.parent, j.child, j.axis, j.origin.xyz, j.limits) for j in model.joints]  # fmt: skip
    return links, joints


def test_wbt_matches_urdf_twin_links_and_joints():
    got, want = parse_webots(WBT), parse_urdf(FIX / "two_link.urdf")
    assert [x.name for x in got.links] == [x.name for x in want.links]
    assert [(j.name, j.type, j.parent, j.child) for j in got.joints] == [(j.name, j.type, j.parent, j.child) for j in want.joints]  # fmt: skip


def test_masses_match_urdf_twin():
    got, want = parse_webots(WBT), parse_urdf(FIX / "two_link.urdf")
    assert [x.mass.value for x in got.links] == pytest.approx([x.mass.value for x in want.links])
    assert got.links[0].com == pytest.approx(want.links[0].com)


def test_limits_match_urdf_twin_in_radians():
    got, want = parse_webots(WBT), parse_urdf(FIX / "two_link.urdf")
    for a, b in zip(got.joints, want.joints):
        assert (a.limits.lower, a.limits.upper) == pytest.approx((b.limits.lower, b.limits.upper))
        assert (a.limits.effort, a.limits.velocity) == pytest.approx((b.limits.effort, b.limits.velocity))  # fmt: skip


def test_axis_origin_and_inertia_match_urdf_twin():
    (gl, gj), (wl, wj) = _rows(parse_webots(WBT)), _rows(parse_urdf(FIX / "two_link.urdf"))
    assert [j[4] + j[5] for j in gj] == pytest.approx([j[4] + j[5] for j in wj])
    assert [x[3] for x in gl] == pytest.approx([x[3] for x in wl])


def test_proto_body_is_read_and_massless_solid_is_flagged(tmp_path):
    p = tmp_path / "arm.proto"
    p.write_text('#VRML_SIM R2023b utf8\nPROTO arm [ ] {\n Robot { name "arm" children [ ] }\n}\n')
    model = parse_webots(p)
    assert (model.links[0].name, model.zero_mass_links) == ("arm", ["arm"])


@pytest.mark.parametrize(
    "text, why",
    [
        ('EXTERNPROTO "x.proto"\nRobot { name "r" }', "EXTERNPROTO"),
        ('Robot { name "r" children [ Pioneer3dx { } ] }', "Pioneer3dx"),
        ('Robot { name "r" children [ DEF A Solid { name "a" } ] }', "DEF/USE"),
        ('Robot { name "r" ', "middle of a node"),
        ("WorldInfo { }", "no Robot"),
        (
            'PROTO p [ field SFFloat m 1 ] { Robot { name "r" physics Physics { mass IS m } } }',
            "IS",
        ),
    ],
)
def test_unreadable_file_is_one_plain_sentence_naming_the_file(tmp_path, text, why):
    p = tmp_path / "bad.wbt"
    p.write_text(text)
    with pytest.raises(ValueError) as caught:
        parse_webots(p)
    message = str(caught.value)
    assert (str(p) in message, why in message, "\n" in message) == (True, True, False)


def test_check_runs_a_wbt_as_a_model():
    result = CliRunner().invoke(app, ["check", str(WBT)])
    assert (result.exit_code, "GO" in result.output, "two_link" in result.output) == (0, True, True)


@pytest.mark.parametrize(
    "body, why",
    [(b'Robot { name "r" ', "middle of a node"), (b"\xff\xfe\x00", "is not text")],
    ids=["syntax", "not-utf8"],
)
def test_check_refuses_unreadable_wbt_in_one_line(tmp_path, body, why):
    p = tmp_path / "bad.wbt"
    p.write_bytes(body)
    result = CliRunner().invoke(app, ["check", str(p)])
    text = " ".join(result.output.split())  # rich wraps lines
    assert (result.exit_code, "cannot read this Webots file" in text, why in text) == (2, True, True)  # fmt: skip
    assert "Traceback" not in result.output


def test_mcp_load_contract_names_a_wbt_a_robot_model():
    from lockwire import mcp_server

    with pytest.raises(mcp_server.ToolError) as caught:
        mcp_server._load_contract(str(WBT))
    assert "robot model, not a lockfile" in str(caught.value)


def test_hook_check_blocks_a_wbt_edit_that_breaks_the_locked_mass(tmp_path, monkeypatch):
    import json

    from lockwire.hook_check import hook_check

    model = tmp_path / "two_link.wbt"
    model.write_text(WBT.read_text())
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["start", "two_link.wbt"]).exit_code == 0
    heavier = model.read_text().replace("mass 0.5 ", "mass 0.9 ")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(model), "content": heavier}}
    code, message = hook_check(json.dumps(payload))
    assert (code, "VIOLATION" in message) == (2, True)


@pytest.mark.parametrize("suffix", [".wbt", ".proto"])
def test_scene_refuses_webots_in_one_plain_line(tmp_path, suffix):
    from lockwire.scene_urdf import SceneRefusal, load_scene_any

    p = tmp_path / f"a{suffix}"
    p.write_text("x")
    with pytest.raises(SceneRefusal) as caught:
        load_scene_any(p)
    assert ("Webots" in str(caught.value), "\n" in str(caught.value)) == (True, False)
