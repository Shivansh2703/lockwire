"""Rules every bundled starter folder must follow; CONTRIBUTING "Add a starter" lists them."""

import json
import re
import shutil
from pathlib import Path

import pytest
from ruamel.yaml import YAML

EXAMPLES = Path(__file__).parent.parent / "src" / "lockwire" / "examples"
KEYS = ("title", "based_on", "licence", "licence_url", "source_repo")
OWN_LICENCE = "Apache-2.0"
MAX_GLB = 2 * 1024 * 1024
FOLDERS = sorted(p.parent for p in EXAMPLES.glob("*/contract.yaml"))


def _sources(node):
    """Yield the `source` of every `provenance` block, however deep."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "provenance":
                yield (v or {}).get("source") if isinstance(v, dict) else None
            else:
                yield from _sources(v)
    elif isinstance(node, list):
        for v in node:
            yield from _sources(v)


def starter_problems(folder: Path) -> list[str]:
    """Every rule the folder breaks, in plain words (empty = fine)."""
    bad = []
    try:
        meta = json.loads((folder / "starter.json").read_text())
    except (OSError, ValueError):
        return ["starter.json missing or not JSON"]
    for k in KEYS:
        if not str(meta.get(k) or "").strip():
            bad.append(f"starter.json needs a non-empty {k}")
    if meta.get("licence") != OWN_LICENCE and not any(
        (folder / n).is_file() for n in ("NOTICE", "LICENSE")
    ):
        bad.append("NOTICE or LICENSE file needed for a licence other than " + OWN_LICENCE)
    glb = folder / "scene.glb"
    if not glb.is_file():
        bad.append("scene.glb missing")
    elif glb.stat().st_size > MAX_GLB:
        bad.append("scene.glb is over 2 MB")
    contract = YAML(typ="safe").load((folder / "contract.yaml").read_text())
    if not all(str(s or "").strip() for s in _sources(contract)):
        bad.append("a provenance block has no source")
    name = str((contract.get("robot") or {}).get("name") or "")
    if name != meta.get("title"):
        bad.append("robot.name must equal starter.json title")
    models = set(re.findall(r"\w+", str(meta.get("based_on", "")).lower()))
    if models & set(re.findall(r"\w+", name.lower())):
        bad.append("robot.name must be an archetype name, not a model name")
    links = {k["name"] for k in (contract.get("model") or {}).get("links", [])}
    if any(
        ((d.get("physical") or {}).get("mount_frame")) not in links
        for d in contract.get("devices", [])
    ):
        bad.append(
            "every device needs physical.mount_frame naming a model link (drag-to-place needs it)"
        )
    return bad


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_starter_follows_rules(folder):
    assert starter_problems(folder) == []


def test_rules_catch_a_broken_copy(tmp_path):
    broken = tmp_path / "bad"
    shutil.copytree(FOLDERS[0], broken)
    assert starter_problems(broken) == []
    (broken / "scene.glb").unlink()
    (broken / "starter.json").write_text(json.dumps({"title": "X"}))
    bad = starter_problems(broken)
    assert "scene.glb missing" in bad and len(bad) >= 5
