"""Guard that GitHub Actions are pinned by full commit SHA, not by tag.

A tag can move (force-push), so the action reviewed is not necessarily the
action that runs. Every `uses:` must pin to a full 40-hex commit SHA with
the tag in a trailing comment for readability.
"""

import re
from pathlib import Path


def test_all_workflow_actions_pinned_by_sha() -> None:
    """Every `uses:` in every workflow file must be 40-hex SHA with tag comment."""
    workflows_dir = Path(__file__).parent.parent / ".github" / "workflows"
    assert workflows_dir.exists(), f"Workflows directory not found: {workflows_dir}"

    unpinned = []
    total_actions = 0

    # the public CI template lives in scripts/ so it never runs on this repo
    # (absent in the public snapshot, where it is installed as the workflow itself)
    templates = list((workflows_dir.parent.parent / "scripts").glob("*.yml"))
    files = [*workflows_dir.glob("*.yml"), *templates]
    for workflow_file in sorted(files):
        with open(workflow_file) as f:
            content = f.read()

        # Find all uses lines
        for line_no, line in enumerate(content.split("\n"), 1):
            if "uses:" in line and not line.strip().startswith("#"):
                total_actions += 1
                # Check if this line is pinned to a 40-hex SHA
                # Pattern: uses: owner/repo@<ref> or owner/repo/action@<ref> [# optional-tag-name]
                # We want: ref is exactly 40 hex characters (a commit SHA)
                match = re.search(
                    r"uses:\s+([\w\-./]+)@([a-zA-Z0-9]+)",
                    line,
                )
                if match:
                    ref = match.group(2)
                    # Must be exactly 40 hex characters (commit SHA), not a tag or version
                    if not (len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)):
                        unpinned.append(f"{workflow_file.name}:{line_no} {line.strip()}")

    if unpinned:
        msg = f"Found {len(unpinned)} action(s) not pinned to 40-hex SHA:\n" + "\n".join(unpinned)
        raise AssertionError(msg)

    # Guard on the guard: ensure we actually found and checked actions
    assert total_actions > 0, "No workflow actions found — did the glob pattern work?"
