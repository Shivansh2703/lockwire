"""MCP list_parts also returns the catalog stock shapes the agent can add."""

import asyncio

from lockwire.edit import add_entry
from lockwire.mcp_server import build_server


def _rows(query=None):
    async def run():
        from mcp.client import Client

        async with Client(build_server()) as client:
            return await client.call_tool("list_parts", {"query": query} if query else {})

    return asyncio.run(run()).structured_content["parts"]


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_stock_rows_state_named_dimensions_and_mass_basis():
    rows = _by_id(_rows())
    assert rows["stock.plate"]["named_dimensions"] is True
    assert rows["stock.round_tube"]["named_dimensions"] is False
    assert rows["stock.plate"]["kind"] == "stock"
    tslot = rows["tslot.20x20"]
    assert tslot["mass_basis"] == "maker's mass per metre"
    assert tslot["material_required"] is False


def test_query_filters_stock_and_parts_keep_keys_with_kind_part():
    assert [r["id"] for r in _rows("plate") if r["kind"] == "stock"] == ["stock.plate"]
    part = next(r for r in _rows() if r["kind"] == "part")
    assert {"id", "name", "label", "numbers"} <= set(part)


def test_every_returned_stock_id_is_addable(tmp_path):
    path = tmp_path / "lockwire.yaml"
    path.write_text('schema_version: "0.7"\nrobot:\n  name: r\n')
    add_entry(path, "link", {"name": "base", "mass": "1 kg"}, source="s", witness="w1")
    row = _by_id(_rows())["stock.plate"]
    fields = {
        "stock": row["id"], "dimensions": dict.fromkeys(row["dimensions"], 0.1),
        "material": "Aluminium 6061-T6", "parent": "base",
        "frame": {"xyz": [0, 0, 0.1]}, "joint": {"type": "fixed"}, "name": "p",
    }  # fmt: skip
    add_entry(path, "stock", fields, source="s", witness="w1")
