"""The drift guard for the app's TypeScript types (e1-d3).

`emit_ts.emit()` is the single source of truth: `app/src/shared/api.gen.ts` is
committed, and this file is what proves a regenerate matches it — the same
proof CI runs on every commit (`packaging/generate-types.md`). No npm
generator sits in the middle (v8 line 265, measured broken against this
repo's pinned `typescript@7.0.2` besides).
"""

from __future__ import annotations

import json
from pathlib import Path

from lockwire import emit_ts
from lockwire.api_payload import ALL_MEMBERS, MEMBERS
from lockwire.report import VERDICT_EXIT_CODES, verdict_word

GEN_TS = Path(__file__).parent.parent / "app" / "src" / "shared" / "api.gen.ts"
APP_PACKAGE_JSON = Path(__file__).parent.parent / "app" / "package.json"


def test_committed_api_gen_ts_is_not_stale():
    """Regenerating from the union byte-matches the committed file. A member
    added to the union without regenerating breaks this (mutation bite 3)."""
    assert GEN_TS.read_text(encoding="utf-8") == emit_ts.emit()


def test_union_emits_a_discriminated_type_typescript_can_narrow():
    """Every enveloped member's own literal `artifact_type` field is what
    TypeScript narrows `MachineDocument` on — not a synthetic `command` field
    invented for the union. Mutation bite 2: drop the `const` handling in
    `emit_ts._ts_type` and every member's `artifact_type` degrades to `string`,
    which no longer narrows anything."""
    text = emit_ts.emit()
    assert "export type MachineDocument =" in text
    for artifact_type, model in MEMBERS.items():
        assert f"{model.__name__} {{" in text
        assert f'artifact_type: "{artifact_type}";' in text, (
            f"{model.__name__}.artifact_type did not emit as a narrowable literal"
        )


def test_every_union_member_appears_in_the_emitted_file():
    """Mutation bite 1: skip a member in the walk and its declaration vanishes."""
    text = emit_ts.emit()
    for model in ALL_MEMBERS:
        assert f"export interface {model.__name__} {{" in text or (
            f"export type {model.__name__} =" in text
        ), f"{model.__name__} is in api_payload.ALL_MEMBERS but missing from the emitted file"


def test_root_list_member_emits_its_item_type_instead_of_an_empty_interface():
    """A top-level list keeps its array shape through the generated TypeScript."""
    text = emit_ts.emit()
    assert "export type LogPayload = (LogEntry)[];" in text
    assert "export interface LogPayload {}" not in text


def test_python_verdict_exit_codes_are_emitted_for_the_app_boundary():
    text = emit_ts.emit()
    assert "export const VERDICT_EXIT_CODES =" in text
    for code, exit_code in VERDICT_EXIT_CODES.items():
        assert f"{json.dumps(verdict_word(code))}: {exit_code}" in text


def test_no_typescript_generator_dependency_declared():
    """Mutation bite 4: adding a TypeScript-generator package to
    `app/package.json` (or importing one from `emit_ts.py`) must be caught.
    `openapi-typescript` specifically is named because it is the one the
    committed design doc considered and measured broken here."""
    package = json.loads(APP_PACKAGE_JSON.read_text(encoding="utf-8"))
    declared = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    generator_names = {
        name for name in declared if "openapi-typescript" in name or "quicktype" in name
    }
    assert not generator_names, f"a TypeScript-generator dependency was added: {generator_names}"

    source = Path(emit_ts.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source and "npx" not in source, (
        "emit_ts.py should not shell out to a TypeScript generator"
    )
