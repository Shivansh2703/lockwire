"""The action's default install must never reach PyPI (a stranger could register the name)."""

import re
from pathlib import Path


def test_default_install_does_not_reach_pypi() -> None:
    text = (Path(__file__).parent.parent / "action.yml").read_text()
    run = text.split("name: Install lockwire", 1)[1].split("- name:", 1)[0]
    # drop the version-guarded branch ("if [ -n ... ]; then <pinned>;"), keep the default path
    default = re.sub(
        r"^\s*if \[ -n \"\$LW_VERSION\" \]; then .*?; else ", "", run, flags=re.S | re.M
    )
    installs = re.findall(r"pip install\s+(\S+)", default)
    assert installs, "no default install found"
    assert all("GITHUB_ACTION_PATH" in i for i in installs), f"default path: {default!r}"
