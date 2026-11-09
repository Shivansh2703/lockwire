"""Field paths into a ROS message: `linear_acceleration.z`, `effort[0]`, `position[name=knee]`.

One resolver for every reader of a contract's `driver.signals.*.field`: the
schema (syntax), the rosbag2 loader and the live capture node (values). This
file is vendored byte-for-byte as `ros/lockwire_ros/lockwire_ros/fieldpath.py`,
because that package must never import lockwire; a test compares the two files.
Keep it dependency-free and Python 3.10 compatible.

`name[2]` indexes an array. `name[key=value]` indexes it by position of `value`
in the sibling array `key` — sensor_msgs/JointState keeps `position`,
`velocity` and `effort` parallel to `name`, so `position[name=knee_joint]`
survives a driver that reorders its joints. Nothing is ever defaulted: a joint
that is absent, or an `effort` array the driver leaves empty, is an error.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple, Union

_SEGMENT = re.compile(r"([A-Za-z_]\w*)(?:\[(?:(\d+)|([A-Za-z_]\w*)=([^\]]+))\])?")
_MISSING = object()

Index = Union[None, int, Tuple[str, str]]


class FieldPathError(ValueError):
    """A field path that is malformed, or does not resolve on this message."""


def parse(path: str) -> List[Tuple[str, Index]]:
    """Split a path into (field, index) steps, or raise naming what is wrong."""
    steps: List[Tuple[str, Index]] = []
    for part in path.split(".") if path else [""]:
        match = _SEGMENT.fullmatch(part)
        if match is None:
            raise FieldPathError(
                "field path {0!r}: bad segment {1!r} (expected name, name[2] or "
                "name[key=value])".format(path, part)
            )
        field, number, key, value = match.groups()
        index: Index = int(number) if number is not None else (key, value) if key else None
        steps.append((field, index))
    return steps


def _get(node: Any, field: str) -> Any:
    if isinstance(node, dict):
        return node.get(field, _MISSING)
    return getattr(node, field, _MISSING)


def resolve(payload: Any, path: str) -> Any:
    """Walk `path` on a decoded message (dicts or attribute objects); never substitute a default."""
    node = payload
    walked = "<message root>"
    for field, index in parse(path):
        parent, node = node, _get(node, field)
        if node is _MISSING:
            raise FieldPathError(
                "field path {0!r} does not resolve (no {1!r} at {2})".format(path, field, walked)
            )
        walked = field if walked == "<message root>" else walked + "." + field
        if index is None:
            continue
        if isinstance(node, (str, dict)) or not hasattr(node, "__len__"):
            raise FieldPathError(
                "field path {0!r} does not resolve ({1} is not an array)".format(path, walked)
            )
        position: Optional[int] = index if isinstance(index, int) else None
        if isinstance(index, tuple):
            key, value = index
            keys = _get(parent, key)
            if keys is _MISSING or isinstance(keys, (str, dict)) or not hasattr(keys, "__len__"):
                raise FieldPathError(
                    "field path {0!r} does not resolve (no array {1!r} beside {2})".format(
                        path, key, field
                    )
                )
            matches = [i for i, k in enumerate(keys) if str(k) == value]
            if len(matches) != 1:
                raise FieldPathError(
                    "field path {0!r} does not resolve ({1!r} appears {2} times in {3}; "
                    "present: {4})".format(path, value, len(matches), key, list(keys))
                )
            position = matches[0]
        assert position is not None
        if position >= len(node):
            raise FieldPathError(
                "field path {0!r} does not resolve ({1} has {2} entries, index {3})".format(
                    path, walked, len(node), position
                )
            )
        node = node[position]
        walked = "{0}[{1}]".format(walked, position)
    return node
