"""Opening a robot file must never run code from it or read files outside its package (0.5.0
XACRO-EVAL). Each hostile file would write a sentinel in tmp_path or leak a secret it reads."""

import time
from pathlib import Path

import pytest

from lockwire.urdf import UnresolvedXacroError, read_model_bytes

NS = 'xmlns:xacro="http://www.ros.org/wiki/xacro"'


def robot(tmp_path: Path, body: str, name="robot.urdf") -> Path:
    p = tmp_path / "bot" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f'<robot name="r" {NS}>{body}<link name="a"/></robot>')
    return p


def refused(p: Path) -> str:
    with pytest.raises(UnresolvedXacroError) as e:
        read_model_bytes(p)
    return str(e.value)


def test_eval_assembled_from_properties_is_refused_and_runs_nothing(tmp_path):
    sentinel = tmp_path / "PWNED"
    p = robot(  # the auditor's file: the parentheses come from properties, so no grep sees them
        tmp_path,
        '<xacro:property name="l" value="("/><xacro:property name="r" value=")"/>'
        f'<link name=\'$(eval exec${{l}}"open${{l}}\\"{sentinel}\\",\\"w\\"${{r}}.write${{l}}\\"x\\"${{r}}"${{r}}or"b")\'/>',
    )
    msg = refused(p)
    assert not sentinel.exists()
    assert "$(eval" in msg and "does not run code from robot files" in msg
    assert "xacro robot.urdf > model.urdf" in msg


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('touch {s}')",
        "().__class__.__base__.__subclasses__()",
        "open('{s}', 'w').write('x')",
        "(lambda: open('{s}', 'w').write('x'))()",
    ],
)
def test_property_expressions_cannot_run_code(tmp_path, expr):
    sentinel = tmp_path / "PWNED"
    p = robot(tmp_path, f'<link name="${{{expr.format(s=sentinel)}}}"/>')
    refused(p)
    assert not sentinel.exists()


@pytest.mark.parametrize("sub", ["$(env HOME)", "$(optenv HOME x)"])
def test_environment_reads_are_refused(tmp_path, sub):
    assert "environment variable" in refused(robot(tmp_path, f'<link name="{sub}"/>'))


@pytest.mark.parametrize("target", ["{out}/secret.xacro", "../secret.xacro"])
def test_include_outside_the_package_is_refused(tmp_path, target):
    (tmp_path / "secret.xacro").write_text(f'<robot {NS}><link name="LEAKED"/></robot>')
    p = robot(tmp_path, f'<xacro:include filename="{target.format(out=tmp_path)}"/>')
    msg = refused(p)
    assert "outside the robot's package" in msg and "LEAKED" not in msg


@pytest.mark.parametrize("call", ["xacro.load_yaml", "load_yaml"])
def test_load_yaml_outside_the_package_is_refused(tmp_path, call):
    (tmp_path / "secret.yaml").write_text("name: LEAKED\n")
    p = robot(tmp_path, f"<link name=\"${{{call}('{tmp_path}/secret.yaml').name}}\"/>")
    assert "outside the robot's package" in refused(p)


def test_endless_macro_ends_in_a_plain_refusal(tmp_path):
    p = robot(tmp_path, '<xacro:macro name="m"><xacro:m/></xacro:macro><xacro:m/>')
    t = time.monotonic()
    assert "recursion" in refused(p)
    assert time.monotonic() - t < 20


def test_arithmetic_include_and_yaml_inside_the_folder_still_load(tmp_path):
    (tmp_path / "bot").mkdir()
    (tmp_path / "bot" / "package.xml").write_text("<package><name>bot</name></package>")
    (tmp_path / "bot" / "part.xacro").write_text(f'<robot {NS}><link name="part"/></robot>')
    (tmp_path / "bot" / "d.yaml").write_text("n: yamlname\n")
    p = robot(
        tmp_path,
        '<xacro:include filename="$(find bot)/part.xacro"/><xacro:include filename="part.xacro"/>'
        '<xacro:property name="y" value="${xacro.load_yaml(\'d.yaml\')}"/>'
        '<link name="${y.n}"/><link name="c${2*pi*0.1}"/>',
        name="robot.urdf.xacro",
    )
    out = read_model_bytes(p).decode()
    assert 'name="part"' in out and 'name="yamlname"' in out and 'name="c0.6283' in out


def test_included_file_with_doctype_entity_is_refused(tmp_path):
    (tmp_path / "bot").mkdir()
    ent = f'<!DOCTYPE r [<!ENTITY e "LEAKED">]><robot {NS}><link name="&e;"/></robot>'
    (tmp_path / "bot" / "inc.xacro").write_text(ent)
    msg = refused(robot(tmp_path, '<xacro:include filename="inc.xacro"/>', "robot.urdf.xacro"))
    assert "DOCTYPE or ENTITY" in msg and "LEAKED" not in msg


@pytest.mark.parametrize("name", ["robot.urdf", "robot.urdf.xacro"])
def test_find_include_climbing_out_is_refused_not_skipped(tmp_path, name):
    (tmp_path / "secret.xacro").write_text(f'<robot {NS}><link name="LEAKED"/></robot>')
    (tmp_path / "bot").mkdir()
    (tmp_path / "bot" / "package.xml").write_text("<package><name>bot</name></package>")
    msg = refused(
        robot(tmp_path, '<xacro:include filename="$(find bot)/../../secret.xacro"/>', name)
    )
    assert "outside the robot's package" in msg


def test_expression_over_ten_thousand_characters_is_refused_in_one_plain_line(tmp_path):
    msg = refused(robot(tmp_path, f'<link name="${{{"1+" * 5001}1}}"/>'))
    assert "too big to compute" in msg and "\n" not in msg


@pytest.mark.parametrize(
    "rel, pkg_xml, ok",
    [
        ("../config/x.yaml", 1, 1),
        ("../../o.yaml", 1, 0),
        ("../config/link.yaml", 1, 0),
        ("../config/x.yaml", 0, 0),
    ],
)
def test_yaml_must_stay_inside_the_package(tmp_path, rel, pkg_xml, ok):
    (tmp_path / "pkg/urdf").mkdir(parents=True)
    (tmp_path / "pkg/config").mkdir()
    (tmp_path / "pkg/config/x.yaml").write_text("n: yamlname\n")
    (tmp_path / "o.yaml").write_text("n: LEAKED\n")
    (tmp_path / "pkg/config/link.yaml").symlink_to(tmp_path / "o.yaml")
    if pkg_xml:
        (tmp_path / "pkg/package.xml").write_text("<package/>")
    p = tmp_path / "pkg/urdf/r.xacro"
    p.write_text(f'<robot name="r" {NS}><link name="${{xacro.load_yaml(\'{rel}\').n}}"/></robot>')
    assert (
        ('name="yamlname"' in read_model_bytes(p).decode())
        if ok
        else "robot's package" in refused(p)
    )


def test_normal_arithmetic_still_loads(tmp_path):
    p = robot(
        tmp_path, "<link name=\"c${2*pi*0.1}d${radians(90)}e${2**8}f${'ab'*3}g${0.5*0.04*0.04}\"/>"
    )
    assert (
        'name="c0.6283' in read_model_bytes(p).decode()
        and "abababg0.0008" in read_model_bytes(p).decode()
    )
