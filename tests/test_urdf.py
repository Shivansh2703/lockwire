import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.urdf import UnresolvedXacroError, parse_urdf, read_urdf_topology

from conftest import FIXTURES, plain

runner = CliRunner()


def test_links_and_masses(testbot_urdf):
    model = parse_urdf(testbot_urdf)
    assert len(model.links) == 4
    base = next(link for link in model.links if link.name == "base_link")
    assert base.mass is not None
    assert base.mass.value == 1.2
    assert base.mass.unit == "kg"
    assert base.frame == "base_link"
    assert base.com == (0.01, 0.0, 0.02)


def test_zero_mass_flagged(testbot_urdf):
    model = parse_urdf(testbot_urdf)
    assert model.zero_mass_links == ["sensor_mount"]


def test_principal_moments_ascending(testbot_urdf):
    model = parse_urdf(testbot_urdf)
    base = next(link for link in model.links if link.name == "base_link")
    m = base.inertia.moments_kgm2
    assert m[0] <= m[1] <= m[2]
    # off-diagonal terms are small: principal moments stay near diagonal values
    assert abs(m[2] - 0.02) < 1e-3


def test_structure_hash_stable_and_sensitive(testbot_urdf, tmp_path):
    original = parse_urdf(testbot_urdf)
    assert parse_urdf(testbot_urdf).structure_hash == original.structure_hash

    # moving a mount is a structural change — the hash must move too
    moved = tmp_path / "moved.urdf"
    moved.write_text(testbot_urdf.read_text().replace('xyz="0.0 0.0 0.05"', 'xyz="0.0 0.0 0.08"'))
    assert parse_urdf(moved).structure_hash != original.structure_hash

    # provenance is carried
    assert original.provenance.witness == "w2"
    assert original.source_sha256 != moved.read_text()  # sanity: fields populated


ANYMAL_D_URDF = FIXTURES / "anymal_d.urdf"


def test_anymal_d_records_all_links_and_joints():
    """P1 (audit 2): `parse_urdf` used to build links only and drop every joint.

    The fixture has 96 `<link>` and 96 `<joint` substrings, but one joint
    (`base_to_hatch`) is commented out in the source XML — so the real,
    parseable count is 95 joints, matching `read_urdf_topology`.

    Mutation-bite: drop the `joints=` line from `parse_urdf` -> this goes red.
    """
    model = parse_urdf(ANYMAL_D_URDF)
    assert len(model.links) == 96
    assert len(model.joints) == 95
    assert "base_to_hatch" not in {j.name for j in model.joints}  # commented out, correctly skipped

    topology = read_urdf_topology(ANYMAL_D_URDF)
    assert len(topology.joints) == 95
    assert {j.name for j in model.joints} == {j.name for j in topology.joints}


def test_anymal_d_root_link_is_base():
    model = parse_urdf(ANYMAL_D_URDF)
    children = {j.child for j in model.joints}
    roots = {link.name for link in model.links} - children
    assert roots == {"base"}


def test_unresolved_xacro_prop_in_link_name_refused():
    """`${…}` in a link name is unexpanded xacro — refuse, do not invent links.

    Mutation-bite: remove `_refuse_unresolved_xacro` from `parse_urdf` → this goes red.
    """
    path = FIXTURES / "unresolved_xacro.urdf"
    with pytest.raises(UnresolvedXacroError, match=r"unresolved xacro") as caught:
        parse_urdf(path)
    message = str(caught.value)
    assert "${" in message or "${…}" in message or "substitutions" in message
    assert "xacro model.urdf.xacro > model.urdf" in message
    # Must not silently produce template-named links.
    with pytest.raises(UnresolvedXacroError):
        read_urdf_topology(path)


def test_unresolved_xacro_element_refused(tmp_path):
    path = tmp_path / "with_xacro_el.urdf"
    path.write_text(
        """<?xml version="1.0"?>
<robot name="macro_stub" xmlns:xacro="http://www.ros.org/wiki/xacro">
  <xacro:include filename="other.xacro"/>
  <link name="base_link"/>
</robot>
"""
    )
    with pytest.raises(UnresolvedXacroError, match=r"unresolved xacro"):
        parse_urdf(path)


def test_unresolved_ros_subst_refused(tmp_path):
    path = tmp_path / "with_find.urdf"
    path.write_text(
        """<?xml version="1.0"?>
<robot name="find_stub">
  <link name="base_link">
    <visual>
      <geometry>
        <mesh filename="package://$(find my_robot)/meshes/base.stl"/>
      </geometry>
    </visual>
  </link>
</robot>
"""
    )
    with pytest.raises(UnresolvedXacroError, match=r"\$\(arg|\$\(find|unresolved xacro"):
        parse_urdf(path)


def test_bare_dollar_in_link_name_is_not_xacro(tmp_path):
    """A `$` without braces is not xacro property syntax — must not false-positive."""
    path = tmp_path / "dollar_name.urdf"
    path.write_text(
        """<?xml version="1.0"?>
<robot name="dollar_stub">
  <link name="cost_$link">
    <inertial>
      <mass value="1.0"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/>
    </inertial>
  </link>
</robot>
"""
    )
    model = parse_urdf(path)
    assert [link.name for link in model.links] == ["cost_$link"]


def test_expanded_urdf_unaffected_by_xacro_guard(testbot_urdf):
    """False-positive guard: finished URDFs must parse exactly as before."""
    model = parse_urdf(testbot_urdf)
    assert len(model.links) == 4
    assert {link.name for link in model.links} == {
        "base_link",
        "leg_link",
        "sensor_mount",
        "imu_link",
    }
    assert model.zero_mass_links == ["sensor_mount"]


def test_urdf_with_doctype_is_refused(tmp_path):
    # MUTATION-BITE: billion-laughs via <!DOCTYPE ... <!ENTITY> must be refused
    # before ET.fromstring ever expands it, not merely handled if it happens to
    # crash cleanly.
    path = tmp_path / "doctype.urdf"
    path.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY lol "lol">
]>
<robot name="bomb">
  <link name="base_link"/>
</robot>
"""
    )
    with pytest.raises(ValueError, match=r"DOCTYPE|ENTITY"):
        parse_urdf(path)
    with pytest.raises(ValueError, match=r"DOCTYPE|ENTITY"):
        read_urdf_topology(path)


def test_urdf_with_entity_only_is_refused(tmp_path):
    path = tmp_path / "entity.urdf"
    path.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY xxe "expanded">
]>
<robot name="bomb">
  <link name="&xxe;"/>
</robot>
"""
    )
    with pytest.raises(ValueError, match=r"ENTITY"):
        parse_urdf(path)


def test_init_refuses_doctype_urdf_no_traceback(tmp_path):
    contract = tmp_path / "lockwire.yaml"
    bomb = tmp_path / "bomb.urdf"
    bomb.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY lol "lol">
]>
<robot name="bomb">
  <link name="base_link"/>
</robot>
"""
    )
    result = runner.invoke(app, ["init", str(bomb), "--out", str(contract)])
    assert result.exit_code == 2, result.output
    flat = plain(result.output)
    assert "DOCTYPE" in flat or "ENTITY" in flat
    assert "Traceback" not in result.output
    assert not contract.exists()


def test_init_refuses_unresolved_xacro_exit_2(tmp_path):
    """§3.3: unreadable/unusable source → exit 2, never confusable with NO-GO."""
    contract = tmp_path / "lockwire.yaml"
    result = runner.invoke(
        app,
        ["init", str(FIXTURES / "unresolved_xacro.urdf"), "--out", str(contract)],
    )
    assert result.exit_code == 2, result.output
    flat = plain(result.output)
    assert "unresolved xacro" in flat
    assert "xacro model.urdf.xacro > model.urdf" in flat
    assert not contract.exists()


def test_a_reparent_moves_the_structure_hash_and_says_so(tmp_path):
    """A re-parent changes the link tree, so it changes `structure_hash`. If it
    moved silently, every later model import would look like drift to
    `describe_structure_drift`, which compares the contract's recorded hash
    against the on-disk URDF's. So the command PRINTS the move, old and new.

    And the diagnostic itself is guarded (LEGIBILITY law's second clause: ask
    what happens when the DIAGNOSTIC is wrong). The printed "new" hash is
    computed by `edit.reparent` from the in-memory model; the file's own value
    is derived by `ModelRecord._derive_structure_hash` on the next load. Those
    two can silently separate, so this asserts the printed hash IS what the
    reloaded contract carries — a confidently wrong hash on the screen is worse
    than none, because someone would act on it.
    """
    from pathlib import Path

    from lockwire.schema import load_contract

    contract = tmp_path / "lockwire.yaml"
    contract.write_bytes((FIXTURES / "armtree.yaml").read_bytes())
    before = load_contract(contract).model.structure_hash

    result = runner.invoke(
        app,
        [
            "reparent",
            str(contract),
            "elbow",
            "--to",
            "base",
            "--source",
            "notes/layout.md",
            "--witness",
            "Maya Chen",
        ],
    )
    assert result.exit_code == 0, result.output
    output = plain(result.output).replace("\n", " ")

    after = load_contract(contract).model.structure_hash
    assert after != before, "the link tree changed but the structure hash did not"
    assert "structure hash moved" in output, result.output
    # Both hashes, in full, and the new one is the one the file actually carries.
    assert before in output.replace(" ", ""), result.output
    assert after in output.replace(" ", ""), result.output
    assert Path(contract).read_text().count(after) == 1, "the recorded hash was not written"


def test_a_device_reparent_leaves_the_structure_hash_alone(tmp_path):
    """The other half of the claim above: re-mounting a DEVICE does not touch
    the link tree, so it must not move the structure hash and must not print a
    move — a hash-move line that fired on every re-parent would train people to
    ignore it."""
    from lockwire.schema import load_contract

    contract = tmp_path / "lockwire.yaml"
    contract.write_bytes((FIXTURES / "armtree.yaml").read_bytes())
    before = load_contract(contract).model.structure_hash

    result = runner.invoke(
        app,
        [
            "reparent",
            str(contract),
            "dev.gripper",
            "--to",
            "elbow",
            "--source",
            "notes/layout.md",
            "--witness",
            "Maya Chen",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "structure hash moved" not in plain(result.output), result.output
    assert load_contract(contract).model.structure_hash == before
    assert load_contract(contract).devices[0].physical.mount_frame == "elbow"


_UTF16_DOC = (
    '<?xml version="1.0" encoding="UTF-16"?>\n{dt}<robot name="r"><link name="base_&e;"/></robot>'
)
_DT = '<!DOCTYPE robot [<!ENTITY e "EXPANDED">]>\n'


@pytest.mark.parametrize("enc", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_doctype_is_refused(tmp_path, enc):
    """the guard must see through the encoding."""
    path = tmp_path / "u16.urdf"
    path.write_bytes(_UTF16_DOC.format(dt=_DT).encode(enc))
    with pytest.raises(ValueError, match=r"DOCTYPE/ENTITY"):
        parse_urdf(path)
    with pytest.raises(ValueError, match=r"DOCTYPE/ENTITY"):
        read_urdf_topology(path)


def test_utf16_without_doctype_still_loads(tmp_path):
    """encoding alone is not a refusal."""
    path = tmp_path / "u16ok.urdf"
    path.write_bytes(_UTF16_DOC.format(dt="").replace("&e;", "x").encode("utf-16"))
    assert read_urdf_topology(path).link_names == {"base_x"}


def test_joint_without_axis_defaults_to_x(tmp_path):
    """The URDF default axis is (1, 0, 0)."""
    p = tmp_path / "r.urdf"
    p.write_text(
        '<robot name="r"><link name="a"/><link name="b"/>'
        '<joint name="j" type="revolute"><parent link="a"/><child link="b"/>'
        '<limit lower="-1" upper="1" effort="1" velocity="1"/></joint></robot>'
    )
    assert parse_urdf(p).joints[0].axis == (1.0, 0.0, 0.0)


def test_fixed_joint_without_axis_has_no_axis(tmp_path):
    """A fixed joint has no axis, so leaving it out records none."""
    p = tmp_path / "f.urdf"
    p.write_text(
        '<robot name="r"><link name="a"/><link name="b"/>'
        '<joint name="j" type="fixed"><parent link="a"/><child link="b"/></joint></robot>'
    )
    assert parse_urdf(p).joints[0].axis is None


def test_mimic_joint_with_zero_limits_loads(tmp_path):
    """A mimic joint's own lower=upper=0 limit is ignored, not refused (OnRobot RG6)."""
    p = tmp_path / "m.urdf"
    p.write_text(
        '<robot name="r"><link name="a"/><link name="b"/><joint name="j" type="revolute">'
        '<parent link="a"/><child link="b"/><limit lower="0" upper="0"/><mimic joint="k"/>'
        "</joint></robot>"
    )
    assert parse_urdf(p).joints[0].limits is None
