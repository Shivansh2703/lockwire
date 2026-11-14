"""The scene pick: a lockfile that declares shapes is drawn from them, never from a stand-in glb."""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lockwire.start_operation as ops
from lockwire import api
from lockwire.cli import _find_scene, app
from lockwire.schema import load_contract

EXAMPLES = Path(__file__).parents[1] / "src" / "lockwire" / "examples"
SHAPED = {"arm-power": 4, "hexapod-param": 19, "rover-param": 5}  # example -> link count


@pytest.mark.parametrize("name", SHAPED)
def test_a_shape_example_has_no_stand_in_scene_and_draws_its_real_links(name, tmp_path):
    assert not (EXAMPLES / name / "scene.glb").exists()
    lock = tmp_path / "lockwire.yaml"
    ops.start_example(name, lock)
    assert not lock.with_suffix(".glb").exists()
    page = api.page(load_contract(lock), contract_path=lock, scene=api.load_scene(lock, None)[0])
    assert page["scene"] is None and len(page["links"]) == SHAPED[name]
    assert all(link["solids"] for link in page["links"])


def test_a_shape_added_in_design_shows_up_even_beside_a_stale_scene(tmp_path):
    lock = tmp_path / "lockwire.yaml"
    ops.start_example("rover-param", lock)
    shutil.copy(EXAMPLES / "drone" / "scene.glb", lock.with_suffix(".glb"))  # someone's old scene
    args = ["--source", "sketch", "--witness", "Maya Chen"]
    out = CliRunner().invoke(
        app, ["add", "link", str(lock), "--name", "bracket", "--shape", "box 0.1 0.1 0.1", *args]
    )
    assert out.exit_code == 0, out.output
    scene, _ = api.load_scene(lock, None)
    page = api.page(load_contract(lock), contract_path=lock, scene=scene)
    new = next(link for link in page["links"] if link["name"] == "bracket")
    assert scene is None and new["solids"][0]["kind"] == "box"


def test_a_real_mesh_scene_is_still_picked_when_no_shapes_are_declared(tmp_path):
    lock = tmp_path / "lockwire.yaml"
    ops.start_example("drone", lock)
    assert _find_scene(lock, None) == (lock.with_suffix(".glb"), True)
    assert len(api.load_scene(lock, None)[0].parts) == 124


def _drone_plus_shape(tmp_path):
    lock = tmp_path / "lockwire.yaml"
    ops.start_example("drone", lock)
    text = lock.read_text().replace("source: docs/reference_robot_specs.md", "source: lockfile")
    lock.write_text(text)  # a lockfile-owned model: the Design panel may add shapes to it
    args = ["--source", "sketch", "--witness", "Maya Chen"]
    cmd = ["add", "link", str(lock), "--name", "bracket", "--shape", "box 0.1 0.1 0.1", *args]
    out = CliRunner().invoke(app, cmd)
    assert out.exit_code == 0, out.output
    return lock


def test_a_mesh_scene_keeps_its_parts_and_draws_a_shape_added_in_design(tmp_path):
    lock = _drone_plus_shape(tmp_path)
    assert _find_scene(lock, None) == (lock.with_suffix(".glb"), True)
    scene, _ = api.load_scene(lock, None)
    assert len(scene.parts) == 124 + 1
    assert [p.node for p in scene.parts if p.extras_id == "bracket"] == ["bracket_shape0"]
    assert len(api.load_scene(lock, None)[0].parts) == len(scene.parts)  # no growth on reload


def test_a_glb_that_names_none_of_the_lockfiles_parts_is_still_ignored(tmp_path):
    lock = tmp_path / "lockwire.yaml"
    ops.start_example("rover-param", lock)
    shutil.copy(EXAMPLES / "drone" / "scene.glb", lock.with_suffix(".glb"))
    assert _find_scene(lock, None) == (None, False)
