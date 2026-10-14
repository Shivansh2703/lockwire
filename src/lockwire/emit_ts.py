"""Emit `app/src/shared/api.gen.ts` directly from the payload union (e1-d3).

Plan v8 line 265, verbatim: *"There is no TypeScript generator in the list,
deliberately: the mainstream generator's own documentation says a
discriminated union like ours comes out in a shape TypeScript cannot narrow.
The Python step that builds the union emits the TypeScript directly (about
forty lines), and CI fails if the committed file is stale."*

Measured 2026-09-08: the alternative (`api-schema`'s JSON Schema piped through
the `openapi-typescript` npm package, per `packaging/generate-types.md`) does
not run on this repo's toolchain today — `openapi-typescript@7.13.0` declares
a peer on `typescript@^5.x` and crashes (`Cannot read properties of undefined
(reading 'createKeywordTypeNode')`) against `app/package.json`'s already-pinned
`typescript@7.0.2`. That is a second, independent reason (beyond v8 line 265)
this box adds no npm dependency.

this walks `api_payload.ALL_MEMBERS` and each model's own
`model_json_schema()` — the same schema `api_payload.export_openapi` already
assembles for `lockwire api-schema` — rather than re-enumerating fields or
commands by hand. One walk, reused.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lockwire import plugins
from lockwire.api_payload import ALL_MEMBERS, MEMBERS
from lockwire.report import VERDICT_EXIT_CODES, verdict_word

#: Every consumer must see this before touching the file by hand.
SENTINEL = (
    "// GENERATED — do not edit by hand.\n"
    "// `uv run lockwire api-types --out app/src/shared/api.gen.ts` regenerates this\n"
    "// file from `src/lockwire/api_payload.py`. CI regenerates and diffs on every run;\n"
    "// a stale committed file fails the build (packaging/generate-types.md).\n"
)


PLUGIN_SENTINEL = (
    "// GENERATED — do not edit by hand.\n"
    "// Written next to `api.gen.ts` by the same `lockwire api-types` run, from the payload\n"
    "// members a plugin registers. A stale committed file fails the build.\n"
)


def _schemas(models: tuple[type, ...] = ALL_MEMBERS) -> dict[str, Any]:
    """The same `{name: schema}` map `api_payload.export_openapi` builds — one
    call per union member, `$defs` folded in beside it."""
    schemas: dict[str, Any] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    return schemas


def _ts_type(node: dict[str, Any]) -> str:
    """One JSON-Schema node -> one TypeScript type expression."""
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        return " | ".join(json.dumps(v) for v in node["enum"])
    if "anyOf" in node:
        return " | ".join(_ts_type(n) for n in node["anyOf"])
    kind = node.get("type")
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        return f"({_ts_type(node['items'])})[]"
    if kind == "object":
        values = node.get("additionalProperties")
        return f"Record<string, {_ts_type(values)}>" if isinstance(values, dict) else "Record<string, unknown>"
    return "unknown"  # pragma: no cover — no union member emits an untyped leaf today


def _interface(name: str, schema: dict[str, Any]) -> str:
    required = set(schema.get("required", []))
    lines = [f"export interface {name} {{"]
    for field, node in schema.get("properties", {}).items():
        optional = "" if field in required else "?"
        lines.append(f"  {field}{optional}: {_ts_type(node)};")
    lines.append("}")
    return "\n".join(lines)


def _declaration(name: str, schema: dict[str, Any]) -> str:
    """Emit objects as interfaces and root schemas as named type aliases."""
    if schema.get("type") == "object" or "properties" in schema:
        return _interface(name, schema)
    return f"export type {name} = {_ts_type(schema)};"


def emit() -> str:
    """The whole file, as text — every union member's interface plus the
    discriminated `MachineDocument` alias TypeScript narrows on `artifact_type`."""
    schemas = _schemas()
    parts = [SENTINEL]
    for name in sorted(schemas):
        parts.append(_declaration(name, schemas[name]))
    verdict_exit_codes = {
        verdict_word(code): exit_code for code, exit_code in VERDICT_EXIT_CODES.items()
    }
    parts.append(
        "export const VERDICT_EXIT_CODES = "
        + json.dumps(verdict_exit_codes, indent=2)
        + ' as const satisfies Record<CheckPayload["verdict"], number>;'
    )
    members = " | ".join(model.__name__ for model in MEMBERS.values())
    parts.append(f"export type MachineDocument = {members};")
    return "\n\n".join(parts) + "\n"


def emit_plugin() -> str | None:
    """The file for members a plugin registered (`plugins.PAYLOAD_*`), or None when
    there are none. Types the free file already defines are imported from it."""
    plugins.ensure()
    models = (*plugins.PAYLOAD_MEMBERS.values(), *plugins.PAYLOAD_EXTRA)
    if not models:
        return None
    free = _schemas()
    own = {name: s for name, s in _schemas(models).items() if name not in free}
    refs = set(re.findall(r'"\$ref": "#/components/schemas/(\w+)"', json.dumps(own)))
    parts = [PLUGIN_SENTINEL]
    if used := sorted(refs & set(free)):
        parts.append("import type { " + ", ".join(used) + ' } from "../api.gen";')
    parts += [_declaration(name, own[name]) for name in sorted(own)]
    members = " | ".join(model.__name__ for model in plugins.PAYLOAD_MEMBERS.values())
    parts.append(f"export type MachineDocumentPlugin = {members};")
    return "\n\n".join(parts) + "\n"


def write(path: Path | str) -> None:
    """The free file at `path`; a plugin's members, if any, at `<dir>/pro/<name>`."""
    Path(path).write_text(emit(), encoding="utf-8")
    if (extra := emit_plugin()) is not None:
        out = Path(path).parent / "pro" / Path(path).name
        out.parent.mkdir(exist_ok=True)
        out.write_text(extra, encoding="utf-8")
