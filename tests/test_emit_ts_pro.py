"""The plugin half of the generated types: `shared/pro/api.gen.ts` and `renderer/pro/schema.json`."""

import json
from pathlib import Path

from lockwire import emit_ts, plugins
from lockwire.api_payload import export_openapi

APP = Path(__file__).parent.parent / "app" / "src"


def test_committed_pro_types_are_not_stale():
    assert (APP / "shared" / "pro" / "api.gen.ts").read_text(
        encoding="utf-8"
    ) == emit_ts.emit_plugin()


def test_pro_schema_matches_its_exporter(tmp_path):
    export_openapi(tmp_path / "schema.json")
    committed = APP / "renderer" / "pro" / "schema.json"
    assert committed.read_bytes() == (tmp_path / "pro" / "schema.json").read_bytes()


def test_pro_file_carries_only_plugin_members_and_imports_the_rest():
    text = emit_ts.emit_plugin()
    assert text is not None
    for model in (*plugins.PAYLOAD_MEMBERS.values(), *plugins.PAYLOAD_EXTRA):
        assert f"export interface {model.__name__} {{" in text
    assert "export interface CheckPayload {" not in text
    assert 'from "../api.gen"' in text
    assert "export type MachineDocumentPlugin = " in text
    assert "lockwire_pro" not in json.dumps(text)
