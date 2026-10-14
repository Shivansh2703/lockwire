"""One provenance row per declared record, optionally attributed by git."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from lockwire import gitref
from lockwire.schema import load_contract


def _path_part(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for key in ("id", "name"):
            if key in item:
                return str(item[key])
    return str(index)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_plain(child) for child in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _declared(node: Any, path: tuple[str, ...] = ()) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        provenance = node.get("provenance")
        if isinstance(provenance, dict):
            lines = {node.lc.key("provenance")[0] + 1}
            lines.update(provenance.lc.key(key)[0] + 1 for key in provenance)
            row = {
                "path": "/".join(path),
                "value": _plain({key: value for key, value in node.items() if key != "provenance"}),
                "witness": str(provenance["witness"]),
                "source": str(provenance["source"]),
                "locked_at": str(provenance["locked_at"]),
                "lines": lines,
            }
            if provenance.get("compared_from") is not None:
                row["compared_from"] = str(provenance["compared_from"])
            yield row
        for key, child in node.items():
            if key != "provenance":
                yield from _declared(child, (*path, str(key)))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _declared(child, (*path, _path_part(child, index)))


def _commits_by_line(porcelain: str) -> dict[int, str]:
    """Per-line commit shas from a ``git blame --porcelain`` transcript."""
    commits: dict[int, str] = {}
    for line in porcelain.splitlines():
        fields = line.split()
        if (
            len(fields) >= 3
            and len(fields[0]) == 40
            and all(c in "0123456789abcdef" for c in fields[0])
        ):
            commits[int(fields[2])] = fields[0]
    return commits


def rows(contract: Path) -> tuple[list[dict[str, Any]], bool]:
    """Validated provenance rows and whether the path belongs to a repository.

    ``last_changing_commit`` is omitted when git has no honest attribution (for
    example an untracked file). The stored witness is rendered verbatim: a
    persisted ``" (default)"`` marker is evidence; machine configuration is not.
    """
    load_contract(contract)
    document = YAML(typ="rt").load(contract)
    result = list(_declared(document))

    repo = gitref.repo_root(contract)
    relpath = gitref.relpath_in_repo(contract, repo) if repo is not None else None
    porcelain = gitref.blame(relpath, repo=repo) if relpath and repo else None
    commits = _commits_by_line(porcelain or "")
    history = (
        gitref.log(None, [relpath], repo=repo, follow=True)
        if porcelain and relpath and repo
        else None
    )
    order = {sha: index for index, (sha, _, _, _) in enumerate(history or [])}
    for row in result:
        candidates = {commits[line] for line in row.pop("lines") if line in commits}
        candidates.discard("0" * 40)
        attributed = [(order[sha], sha) for sha in candidates if sha in order]
        if attributed:
            row["last_changing_commit"] = min(attributed)[1]
    return result, repo is not None
