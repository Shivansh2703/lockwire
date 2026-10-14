"""The sidecar: GET /v1/hello, POST /v1/run for the read-only commands,
and ``lockwire serve``'s lifecycle. Uses FastAPI's TestClient for the app itself
(no socket involved) and one real subprocess test for the actual bind/port/token/
exit lifecycle a real Electron parent would drive.

The sidecar's HTTP surface is private to the app — these tests hold that
shape, not a published contract.
"""


import hashlib
import inspect
import json
import logging
import os
import re
import signal
import shutil
import socket
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from conftest import FIXTURES
from lockwire import __version__
from lockwire import api, plugins
from lockwire import serve
from lockwire.api_payload import EditPayload
from lockwire.edit import Change, contract_revision
from lockwire.api import contract_sha
from lockwire.schema import load_contract
from lockwire.serve import create_app


REPO_ROOT = Path(__file__).resolve().parent.parent


X500 = FIXTURES / "x500" / "contract.yaml"


CONTRACT_EMPTY = FIXTURES / "contract_empty.yaml"


SERVE_ARGV = ["uv", "run", "lockwire", "serve"]
TOKEN = "test-token"


def _client(app) -> TestClient:
    """Loopback base_url, so the Host header the tests send is the one a real
    caller sends. TestClient's default is ``http://testserver``, and accepting
    that name would mean a test-only bypass living in the production guard."""
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client() -> TestClient:
    return _client(create_app(TOKEN))


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"X-Lockwire-Token": token}


# --------------------------------------------------------------------- hello


def test_hello_needs_token(client: TestClient):
    response = client.get("/v1/hello")
    assert response.status_code == 401


def test_hello_wrong_token(client: TestClient):
    response = client.get("/v1/hello", headers=_auth("wrong"))
    assert response.status_code == 401


def test_hello_ok(client: TestClient):
    response = client.get("/v1/hello", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == __version__
    names = {command["name"] for command in body["commands"]}
    # `scan` and `preflight --live` are off this API entirely (O9);
    # `mcp` is a door, not a command to serve through another door.
    assert set(serve._FREE_COMMANDS) <= names


FREE_COMMANDS = {
    "start", "check", "page", "declare-form", "declare", "add", "review", "commit", "undo",
    "place", "move", "rotate", "reparent", "mount-shape", "describe", "what-if", "plan", "diff",
    "log", "report",
}  # fmt: skip


def _property_names(node) -> set[str]:
    if isinstance(node, dict):
        names = set(node.get("properties", {}))
        return names.union(*(_property_names(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_property_names(v) for v in node))
    return set()


def test_free_sidecar_serves_exactly_the_free_commands(monkeypatch: pytest.MonkeyPatch):
    """With no plugin loaded the dispatch table is the free list and nothing else, no request
    model carries a `recording` or `preflight` field, and any other command is a 422."""
    from lockwire import plugins

    monkeypatch.setattr(plugins, "entry_points", lambda group: [])
    monkeypatch.setattr(plugins, "_ready", False)
    monkeypatch.setattr(plugins, "SERVE_COMMANDS", {})
    monkeypatch.setattr(plugins, "SERVE_SETUP", [])
    monkeypatch.setattr(plugins, "SERVE_POLICY", None)
    assert set(serve.commands()) == FREE_COMMANDS
    assert set(serve._FREE_COMMANDS) == FREE_COMMANDS
    for entry in serve.commands().values():
        names = _property_names(entry.command.model_json_schema())
        assert not names & {"recording", "preflight"}, (entry.command.__name__, names)

    client = _client(create_app(TOKEN))
    hello = client.get("/v1/hello", headers=_auth()).json()
    assert {c["name"] for c in hello["commands"]} == FREE_COMMANDS
    reply = client.post("/v1/run", headers=_auth(), json={"command": "no-such-command", "args": {}})
    assert reply.status_code == 422


# ----------------------------------------------------------------------- run


def test_run_needs_token(client: TestClient):
    response = client.post("/v1/run", json={"command": "check", "args": {"contract": str(X500)}})
    assert response.status_code == 401


def test_what_if_is_api_routed_stale_checked_and_never_writes(tmp_path: Path):
    contract = tmp_path / "contract.yaml"
    contract.write_bytes((FIXTURES / "contract_clean.yaml").read_bytes())
    (tmp_path / "testbot.urdf").write_bytes((FIXTURES / "testbot.urdf").read_bytes())
    ledger = contract.with_name("contract.health.jsonl")
    ledger.write_bytes(b"existing health receipt\n")
    before_contract, before_ledger = contract.read_bytes(), ledger.read_bytes()
    args = {
        "contract": str(contract),
        "expected_revision": contract_revision(contract),
        "change": {
            "path": "topology/rails/rail.5v/capacity",
            "value": "1 W",
            "source": "docs/datasheet.pdf",
            "witness": "Maya Chen",
        },
    }
    response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "what-if", "args": args}
    )
    reply = response.json()
    assert response.status_code == 200
    assert reply["refusal"] is None
    assert reply["payload"] == plugins.pick("what_if", api.what_if)(
        contract,
        [
            Change(
                path=args["change"]["path"],
                value=args["change"]["value"],
                source=args["change"]["source"],
                witness=args["change"]["witness"],
            )
        ],
    )
    assert contract.read_bytes() == before_contract
    assert ledger.read_bytes() == before_ledger

    stale = {**args, "expected_revision": "0" * 64}
    stale_reply = (
        _client(create_app(TOKEN))
        .post("/v1/run", headers=_auth(), json={"command": "what-if", "args": stale})
        .json()
    )
    assert stale_reply["payload"] is None
    assert stale_reply["refusal"] == "contract changed since it was opened; reload before comparing"
    assert contract.read_bytes() == before_contract
    assert ledger.read_bytes() == before_ledger

    malformed = {**args, "change": {**args["change"], "apply": True}}
    malformed_response = _client(create_app(TOKEN)).post(
        "/v1/run", headers=_auth(), json={"command": "what-if", "args": malformed}
    )
    assert malformed_response.status_code == 422
    assert contract.read_bytes() == before_contract
    assert ledger.read_bytes() == before_ledger


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("place", {"target": "dev.gripper", "on": "elbow", "offset": [10, 0, 25], "unit": "mm"}),
        ("move", {"target": "dev.gripper", "offset": [10, 0, 25], "unit": "mm"}),
        ("rotate", {"target": "wrist", "rpy": [0, 0, 90], "unit": "deg"}),
        ("reparent", {"target": "elbow", "to": "base"}),
        (
            "mount-shape",
            {"target": "dev.gripper", "shape": {"kind": "plate", "dimensions_mm": [100, 50, 3]}},
        ),
    ],
)
def test_served_cad_edits_use_the_existing_api_document_and_reopen_contract(
    tmp_path: Path, command: str, args: dict
):
    contract = tmp_path / "armtree.yaml"
    shutil.copy(FIXTURES / "armtree.yaml", contract)
    response = _client(create_app(TOKEN)).post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": command,
            "args": {
                "contract": str(contract),
                **args,
                "source": "notes/cad.md",
                "witness": "Maya Chen",
                "expected_revision": contract_revision(contract),
            },
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["refusal"] is None
    payload = EditPayload.model_validate(body["payload"])
    assert payload.written is True
    assert load_contract(contract).robot.name == "armbot"
    assert payload.contract_sha == contract_sha(contract)


def test_authenticated_cad_write_is_not_refused_when_its_sha_reread_fails(
    tmp_path: Path, monkeypatch
):
    contract = tmp_path / "armtree.yaml"
    shutil.copy(FIXTURES / "armtree.yaml", contract)
    before = contract.read_bytes()

    def _failed_reread(_path):
        raise OSError("post-write reread failed")

    monkeypatch.setattr("lockwire.api.contract_sha", _failed_reread)
    response = _client(create_app(TOKEN)).post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "move",
            "args": {
                "contract": str(contract),
                "target": "dev.gripper",
                "offset": [10, 0, 25],
                "unit": "mm",
                "source": "notes/cad.md",
                "witness": "Maya Chen",
                "expected_revision": contract_revision(contract),
            },
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["refusal"] is None
    payload = EditPayload.model_validate(body["payload"])
    assert contract.read_bytes() != before
    assert payload.written is True
    assert payload.contract_sha is None
    assert payload.contract_sha_error == "post-write reread failed"


@pytest.mark.parametrize("parent, word", [("wrist", "CYCLE"), ("detached_cart", "ORPHAN")])
def test_served_cad_reparent_refusals_preserve_contract_bytes(
    tmp_path: Path, parent: str, word: str
):
    contract = tmp_path / "armtree.yaml"
    shutil.copy(FIXTURES / "armtree.yaml", contract)
    before = contract.read_bytes()
    body = (
        _client(create_app(TOKEN))
        .post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "reparent",
                "args": {
                    "contract": str(contract),
                    "target": "shoulder",
                    "to": parent,
                    "source": "notes/cad.md",
                    "witness": "Maya Chen",
                    "expected_revision": contract_revision(contract),
                },
            },
        )
        .json()
    )
    assert body["refusal"] is not None and word in body["refusal"]
    assert contract.read_bytes() == before


@pytest.mark.parametrize(
    "body",
    [
        {
            "command": "move",
            "args": {
                "contract": "x",
                "target": "d",
                "offset": [1, 2],
                "unit": "mm",
                "source": "s",
                "witness": "w",
            },
        },
        {
            "command": "rotate",
            "args": {
                "contract": "x",
                "target": "l",
                "rpy": [0, 0, 1],
                "unit": "rad",
                "source": "s",
                "witness": "w",
                "extra": True,
            },
        },
        {
            "command": "mount-shape",
            "args": {
                "contract": "x",
                "target": "d",
                "shape": {"kind": "sphere", "dimensions_mm": [1, 2, 3]},
                "source": "s",
                "witness": "w",
            },
        },
    ],
)
def test_served_cad_requests_refuse_malformed_shapes_before_dispatch(
    client: TestClient, body: dict
):
    assert client.post("/v1/run", headers=_auth(), json=body).status_code == 422
    assert client.post("/v1/run", json=body).status_code == 401


def test_run_check_no_go(client: TestClient):
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": str(X500)}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 1
    assert body["verdict"] == "NO-GO"  # JSON verdict spelling is hyphenated
    assert body["refusal"] is None
    assert body["cli_line"] == f"lockwire check {X500}"

    # The payload IS the library's document, not a second one assembled here.
    # No `scene` key here: T2 discovery only matches a scene sharing the
    # contract's own stem (`cli.py:454`, `_find_scene`) — X500's checked-in
    # fixture is `contract.yaml` beside `x500.glb`, a deliberate stem
    # mismatch, so nothing is discovered for THIS path (measured; see
    # `_x500_with_matching_scene_stem` below for the discoverable case).
    from lockwire import api

    assert body["payload"] == api.check(X500)


def test_run_declare_form_returns_library_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from lockwire import api

    expected = api.declare_form(X500)
    calls: list[Path | str] = []

    def producer(contract: Path | str):
        calls.append(contract)
        return expected

    monkeypatch.setattr(serve.api, "declare_form", producer)
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "declare-form", "args": {"contract": str(X500)}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cli_line"] == f"lockwire declare-form {X500}"
    assert body["exit_code"] == 0
    assert body["verdict"] is None
    assert body["refusal"] is None
    assert body["payload"] == jsonable_encoder(expected)
    assert calls == [str(X500)]
    robot_body = (
        _client(create_app(TOKEN, robot=True))
        .post(
            "/v1/run",
            headers=_auth(),
            json={"command": "declare-form", "args": {"contract": str(X500)}},
        )
        .json()
    )
    assert robot_body["refusal"] is None


def test_run_declare_form_malformed_args_is_422_only_after_auth(client: TestClient):
    payload = {"command": "declare-form", "args": {"contract": 7, "unexpected": True}}
    assert client.post("/v1/run", headers=_auth(), json=payload).status_code == 422
    assert client.post("/v1/run", json=payload).status_code == 401


def test_run_diff_reuses_the_real_history_producer(tmp_path: Path, client: TestClient):
    """The served seam keeps its refs and contract path; it invents no diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for key, value in (("user.email", "test@example.com"), ("user.name", "test")):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    contract = repo / "lockwire.yaml"
    source = (FIXTURES / "contract_clean.yaml").read_text(encoding="utf-8")
    old = "capacity: {value: 5, unit: W}"
    for capacity in ("1", "8"):
        contract.write_text(
            source.replace(old, f"capacity: {{value: {capacity}, unit: W}}"), encoding="utf-8"
        )
        subprocess.run(["git", "add", "lockwire.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", f"capacity {capacity}"], cwd=repo, check=True)

    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "diff", "args": {"contract": str(contract), "a": "HEAD~1", "b": "HEAD"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cli_line"] == f"lockwire diff HEAD~1 HEAD --file {contract}"
    assert body["payload"] == api.diff(contract, "HEAD~1", "HEAD")
    assert body["payload"]["changes"][0]["old"] == 1.0
    assert body["payload"]["changes"][0]["new"] == 8.0


def test_run_diff_refuses_missing_ref_and_extra_args(client: TestClient):
    missing = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "diff", "args": {"contract": str(X500), "a": "no-such-ref"}},
    )
    assert missing.status_code == 200
    assert missing.json()["exit_code"] == 2
    assert "no such ref 'no-such-ref'" in missing.json()["refusal"]

    malformed = {"command": "diff", "args": {"contract": str(X500), "scope": "all"}}
    assert client.post("/v1/run", headers=_auth(), json=malformed).status_code == 422
    assert client.post("/v1/run", json=malformed).status_code == 401


def test_run_page_returns_the_page_document(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """e1-pg: `PagePayload` had no producer — this crosses the sidecar's real
    HTTP boundary (`client.post`, not `_handle_page` called in-process), which
    is the thing that catches a document mangled on the way out."""
    import lockwire.report_html as report_html_module

    monkeypatch.setattr(report_html_module, "_now_utc", lambda: "2026-01-01T00:00:00Z")
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "page", "args": {"contract": str(X500)}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0  # `gui`'s own convention: never a decision gate
    assert body["verdict"] is None
    assert body["refusal"] is None
    assert body["cli_line"] == f"lockwire check {X500}"

    from lockwire import api
    from lockwire.api_payload import PagePayload

    expected = api.page(X500, cli_line=body["cli_line"])
    assert body["payload"] == expected
    # Anchored on the union's own model (PARITY-ANCHOR LAW), not just on
    # agreement between the two doors: a real HTTP round trip (JSON in, JSON
    # out) must still validate as the union member six other boxes build on.
    PagePayload.model_validate(
        {k: v for k, v in body["payload"].items() if k in PagePayload.model_fields}
    )


def _x500_with_matching_scene_stem(tmp_path: Path) -> Path:
    """Copy X500's contract + glb into `tmp_path` under one shared stem
    (`contract.yaml` / `contract.glb`), the same trick
    `test_gui_discovers_a_sidecar_scene_beside_the_contract` (`tests/test_cli.py`)
    uses — T2 discovery (`_find_scene`) only matches `contract.with_suffix(ext)`,
    and the checked-in X500 fixture's `x500.glb` deliberately does not share
    `contract.yaml`'s stem."""
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500.read_bytes())
    (tmp_path / "contract.glb").write_bytes((X500.parent / "x500.glb").read_bytes())
    return contract_path


def test_check_reply_carries_a_discovered_scenes_hash_undecimated(
    client: TestClient, tmp_path: Path
):
    contract_path = _x500_with_matching_scene_stem(tmp_path)
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": str(contract_path)}},
    )
    scene = response.json()["payload"]["scene"]
    assert scene["source"] == "contract.glb (found beside the contract)"
    assert isinstance(scene["hash"], str) and len(scene["hash"]) == 64
    import hashlib

    asset = tmp_path / "contract.glb"
    assert scene["asset_path"] == str(asset.resolve())
    assert scene["asset_sha256"] == hashlib.sha256(asset.read_bytes()).hexdigest()
    assert scene["expected_asset_sha256"] is None
    # Un-decimated (v8 line 317): the display cap is `gui --out`'s, not this door's.
    assert scene["display_triangles"] == scene["triangles"]


def test_matching_scene_hash_ships_no_geometry(client: TestClient, tmp_path: Path):
    contract_path = _x500_with_matching_scene_stem(tmp_path)
    first = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": str(contract_path)}},
    )
    current_hash = first.json()["payload"]["scene"]["hash"]

    second = client.post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "check",
            "args": {"contract": str(contract_path)},
            "scene": {"have": current_hash},
        },
    )
    short = second.json()["payload"]["scene"]
    assert short["unchanged"] is True
    assert short["asset_sha256"] == first.json()["payload"]["scene"]["asset_sha256"]
    assert short["expected_asset_sha256"] is None
    assert "parts" not in short
    # The accept line's own bar: a reply with the geometry skipped is small.
    assert len(second.content) < 50_000


def test_changed_scene_hash_ships_geometry_with_its_new_hash(client: TestClient, tmp_path: Path):
    contract_path = _x500_with_matching_scene_stem(tmp_path)
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "check",
            "args": {"contract": str(contract_path)},
            "scene": {"have": "not-the-real-hash"},
        },
    )
    scene = response.json()["payload"]["scene"]
    assert "unchanged" not in scene
    assert isinstance(scene["hash"], str) and len(scene["hash"]) == 64
    assert scene["parts"]


def test_anymal_check_payload_contains_dae_meshes(client: TestClient):
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": str(FIXTURES / "anymal" / "contract.yaml")}},
    )
    scene = response.json()["payload"]["scene"]
    assert len(scene["parts"]) == 40
    assert scene["triangles"] > 0
    assert {part["id"] for part in scene["parts"]} >= {"base", "top_shell", "bottom_shell"}
    assert all(part["positions"]["data"] and part["indices"]["data"] for part in scene["parts"])


def test_parsed_scene_cached_by_path_mtime_size(client: TestClient, tmp_path: Path):
    from lockwire import serve

    contract_path = _x500_with_matching_scene_stem(tmp_path)
    serve._SCENE_CACHE.clear()
    client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": str(contract_path)}},
    )
    assert len(serve._SCENE_CACHE) == 1
    client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": str(contract_path)}},
    )
    # Same (path, mtime, size) -> still exactly one entry, not re-parsed into a
    # second cache slot.
    assert len(serve._SCENE_CACHE) == 1


def test_selected_mesh_reparses_after_an_unselected_cache_entry(tmp_path: Path):
    """A root-only cache entry cannot bypass a later selected-model binding."""
    import hashlib

    from lockwire.scene import SceneRefusal

    mesh = tmp_path / "mesh.stl"
    mesh.write_text(
        "solid triangle\nfacet normal 0 0 1\nouter loop\n"
        "vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
        "endloop\nendfacet\nendsolid triangle\n"
    )
    scene = tmp_path / "lockwire.urdf"
    scene.write_text(
        '<robot name="mesh"><link name="base"><visual><geometry>'
        '<mesh filename="mesh.stl"/></geometry></visual></link></robot>'
    )
    contract = load_contract(X500)
    assert contract.model is not None
    contract.model.source = "elsewhere/lockwire.urdf"
    contract.model.source_sha256 = "0" * 64
    contract.model.external_assets = []
    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text(json.dumps(contract.model_dump(mode="json")))
    serve._SCENE_CACHE.clear()
    serve._cached_scene(contract_path)
    assert len(serve._SCENE_CACHE) == 1

    contract.model.source = scene.name
    contract.model.source_sha256 = hashlib.sha256(scene.read_bytes()).hexdigest()
    contract_path.write_text(json.dumps(contract.model_dump(mode="json")))
    with pytest.raises(SceneRefusal, match="no declared sha256"):
        serve._cached_scene(contract_path)


def test_run_check_not_scored(client: TestClient):
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": str(CONTRACT_EMPTY)}},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["exit_code"] == 3
    assert body["verdict"] == "NOT_SCORED"


def test_run_bad_contract_path_is_a_refusal(client: TestClient):
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "check", "args": {"contract": "no/such/contract.yaml"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] is None
    assert body["payload"] is None
    assert "no contract at" in body["refusal"]


def test_run_unknown_command_is_422(client: TestClient):
    response = client.post(
        "/v1/run", headers=_auth(), json={"command": "delete-everything", "args": {}}
    )
    assert response.status_code == 422


def test_report_and_gui_call_library_load_scene(client: TestClient, tmp_path: Path):
    """``report`` and ``gui`` both call ``api.load_scene`` instead of each
    carrying their own "which scene, from where" lookup — the
    same block that used to sit duplicated as ``serve._load_scene`` beside
    ``cli._load_scene_for_cli``.

    Bite: monkeypatch the library function to refuse and watch both handlers'
    refusals change from whatever they were to the injected message."""
    import lockwire.serve as serve_module
    from lockwire.scene import SceneRefusal

    def _boom(contract_path, scene, package_root):
        raise SceneRefusal("planted by test_report_and_gui_call_library_load_scene")

    original = serve_module.api.load_scene
    serve_module.api.load_scene = _boom
    try:
        report_response = client.post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "report",
                "args": {"contract": str(X500), "out": str(tmp_path / "r.html")},
            },
        )
    finally:
        serve_module.api.load_scene = original

    for response in (report_response,):
        assert response.status_code == 200
        body = response.json()
        assert body["exit_code"] == 2
        assert "planted by test_report_and_gui_call_library_load_scene" in body["refusal"]


# ---------------------------------------------------------- mutation bite target


def test_token_check_is_load_bearing(client: TestClient):
    """The 401 tests above fail if the token comparison in ``serve.py``'s
    middleware is deleted or short-circuited — the bite named in the box brief."""
    assert client.get("/v1/hello").status_code == 401
    assert client.post("/v1/run", json={"command": "check", "args": {}}).status_code == 401


# ------------------------------------------------------------- lockwire serve
#
# ``uv run lockwire serve ...`` forks a real, independent ``lockwire serve``
# process rather than exec'ing into it (measured 2026-09-09: the ``Popen``
# object's pid is ``uv``'s own wrapper pid; the actual server is a distinct
# child pid, reparented to init once ``uv`` exits). A plain ``proc.kill()``
# on that ``Popen`` only kills the wrapper — the real server, and its bound
# port, keep running forever. That is the leak six worktrees' worth of
# orphaned ``lockwire serve`` processes came from tonight (E0-sv). Every test
# below launches through ``start_serve`` and never manages the kill itself
# (dev-team fleet law: "give the lifetime to a fixture that owns it") —
# ``start_new_session=True`` makes the server its own process-group leader, so
# ``_stop_process_group`` can reap the WHOLE group, wrapper and server both.


def _direct_children(pid: int) -> set[int]:
    """Return direct children on the Unix hosts that run this subprocess test."""
    try:
        rows = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, check=True, text=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return set()

    children = set()
    for row in rows:
        fields = row.split()
        if len(fields) == 2 and int(fields[1]) == pid:
            children.add(int(fields[0]))
    return children


def _stop_process_group(proc: subprocess.Popen, pgid: int) -> None:
    """Stop ``uv``'s child first, so ``uv`` reaps it before the group dies.

    Killing the whole group at once leaves the server as an unreapable zombie
    under a non-reaping container PID 1 on Linux.  ``uv`` is still its parent
    here, so terminating the direct child lets ``proc.wait`` reap both.
    """
    deadline = time.monotonic() + 5
    while proc.poll() is None:
        child_pids = _direct_children(proc.pid)
        if child_pids:
            for child_pid in child_pids:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(timeout=5)


@pytest.fixture
def start_serve():
    """Factory fixture: launches ``lockwire serve`` subprocesses and guarantees
    every one of them (whole process group, not just the ``uv`` wrapper) is
    gone by the time the test ends, regardless of how the test itself exits."""
    procs: list[tuple[subprocess.Popen, int]] = []

    def _start(*args: str, env: dict[str, str] | None = None) -> subprocess.Popen:
        proc = subprocess.Popen(
            [*SERVE_ARGV, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        procs.append((proc, proc.pid))
        return proc

    yield _start

    for proc, pgid in procs:
        _stop_process_group(proc, pgid)


def _spawn_served_group() -> tuple[subprocess.Popen, int]:
    """Launch ``lockwire serve`` in its own process group and wait for it to
    be ready. Returns the ``Popen`` (``uv``'s wrapper) and the group's pgid."""
    proc = subprocess.Popen(
        [*SERVE_ARGV, "--port", "0"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid
    json.loads(proc.stdout.readline())
    proc.stdin.close()
    return proc, pgid


def test_serve_subprocess_group_has_no_survivor_after_stop():
    """The fixture's cleanup leaves no running process or zombie in its group.

    This checks only the group created by this test.  The mutation bite is to
    replace the child-first stop with ``proc.kill()``: that kills only ``uv``
    and leaves the live server in this group, so the assertion fails.
    """
    proc, pgid = _spawn_served_group()
    _stop_process_group(proc, pgid)
    with pytest.raises((ProcessLookupError, PermissionError)):
        os.killpg(pgid, 0)


def test_served_native_start_real_http_reuse_stale_and_readback(start_serve, tmp_path):
    import httpx
    import selectors

    source = tmp_path / "robot.urdf"
    source.write_text('<robot name="robot"><link name="base"/></robot>')
    proc = start_serve("--port", "0", "--exit-on-stdin-close")
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), "sidecar did not become ready"
    ready = json.loads(proc.stdout.readline())
    with httpx.Client(
        base_url=f"http://127.0.0.1:{ready['port']}",
        headers={"X-Lockwire-Token": ready["token"]},
        timeout=5,
    ) as client:

        def run(command, args):
            return client.post("/v1/run", json={"command": command, "args": args}).json()

        result = run("start", {"urdf": str(source)})
        assert result["refusal"] is None and result["payload"]["written"]
        target = Path(result["payload"]["path"])
        assert target == tmp_path / "lockwire.yaml"
        assert (
            run("page", {"contract": str(target)})["payload"]["revision"]
            == result["payload"]["revision"]
        )
        assert run("start", {"urdf": str(source)})["payload"]["written"] is False
        target.write_bytes(target.read_bytes() + b"\n# external edit\n")
        changed = target.read_bytes()
        refused = run(
            "start",
            {
                "urdf": str(source),
                "overwrite": True,
                "expected_revision": result["payload"]["revision"],
            },
        )
        assert refused["exit_code"] == 2 and "changed since" in refused["refusal"]
        assert target.read_bytes() == changed
        accepted = run(
            "start",
            {
                "urdf": str(source),
                "overwrite": True,
                "expected_revision": hashlib.sha256(changed).hexdigest(),
            },
        )
        assert accepted["payload"]["written"] is True
        assert not (tmp_path / "lockwire-gui.html").exists()
    proc.stdin.close()
    assert proc.wait(timeout=5) == 0


def test_serve_lifecycle_ready_line_auth_and_clean_exit(start_serve):
    proc = start_serve("--port", "0", "--exit-on-stdin-close")
    ready_line = proc.stdout.readline()
    data = json.loads(ready_line)
    assert data["lockwire"] == "ready"
    assert data["version"] == __version__
    port = data["port"]
    token = data["token"]
    assert isinstance(port, int) and port > 0
    assert isinstance(token, str) and len(token) > 10

    import httpx

    response = httpx.get(
        f"http://127.0.0.1:{port}/v1/hello", headers={"X-Lockwire-Token": token}, timeout=5
    )
    assert response.status_code == 200

    response = httpx.get(f"http://127.0.0.1:{port}/v1/hello", timeout=5)
    assert response.status_code == 401

    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail("lockwire serve did not exit within 5s of stdin closing")
    assert proc.returncode == 0


def test_serve_binds_loopback_only_lan_connection_refused(start_serve):
    lan_ip = None
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        candidate = probe.getsockname()[0]
        if not candidate.startswith("127."):
            lan_ip = candidate
    except OSError:
        pass
    finally:
        probe.close()
    if lan_ip is None:
        pytest.skip("no non-loopback IP on this machine")

    proc = start_serve("--port", "0", "--exit-on-stdin-close")
    data = json.loads(proc.stdout.readline())
    port = data["port"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    with pytest.raises(OSError):
        sock.connect((lan_ip, port))
    sock.close()
    proc.stdin.close()


def test_serve_refuses_non_loopback_host():
    result = subprocess.run(
        [*SERVE_ARGV, "--host", "0.0.0.0", "--port", "0"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "loopback only" in result.stderr


# ------------------------------------------------------- the amendment's tests


def test_serve_module_is_the_import_path():
    """The sidecar lives at ``lockwire.serve``; ``lockwire.api`` is the public
    library and must not be the sidecar. Both halves, or the rename could
    be undone by re-exporting one from the other."""
    from lockwire import api

    assert serve.create_app.__module__ == "lockwire.serve"
    assert not hasattr(api, "create_app")
    assert not hasattr(api, "run_server")
    assert "load" in api.FUNCTIONS  # api.py is still the library, not a stub


def test_handlers_call_the_library_not_the_checks():
    """Every command with a ``lockwire.api`` twin is served by calling that twin.

    Walks the commands that return a document and asserts each handler's body names
    ``api.<name>``. It also asserts no handler reaches a check module directly, which is
    the duplication the project forbids. ``report`` returns no document, so it is checked
    by the one shared computation it calls (``api.load_scene``, E1-at).
    """
    twins = {"check", "plan", "describe", "log", "diff"}
    assert twins <= set(serve._FREE_COMMANDS), "a twinned command stopped being served"
    for name in twins:
        handler = serve._FREE_COMMANDS[name].handler
        body = inspect.getsource(handler)
        assert f"api.{name.replace('-', '_')}(" in body, f"{name} does not call the library"

    forbidden = ("run_record_checks", "validate_plan(", "describe_all(")
    for name in twins:
        body = inspect.getsource(serve._FREE_COMMANDS[name].handler)
        for symbol in forbidden:
            assert symbol not in body, f"{name} recomputes with {symbol}"

    body = inspect.getsource(serve._FREE_COMMANDS["report"].handler)
    assert "api.load_scene(" in body, "report does not call api.load_scene"


def test_token_checked_before_body_is_parsed(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """A body pydantic would reject must still read 401, not 422, without a token.

    This is why the check is middleware: FastAPI parses and validates a request
    body *before* it solves a route's dependencies, so a ``Depends`` guard would
    let an unauthenticated caller learn the body schema from a 422.
    """
    response = client.post("/v1/run", json={"command": "delete-everything", "args": {}})
    assert response.status_code == 401
    response = client.post("/v1/run", content=b"not json at all")
    assert response.status_code == 401

    # ...and the route was never entered at all. A guard that runs *after* the
    # route still returns 401 while having parsed and dispatched the body, which
    # is the same hole wearing the right status code.
    monkeypatch.setattr(
        serve, "dispatch", lambda body: pytest.fail("an unauthenticated body was dispatched")
    )
    assert (
        client.post(
            "/v1/run", json={"command": "check", "args": {"contract": str(X500)}}
        ).status_code
        == 401
    )
    # ...and with the token the same body is the 422 it deserves.
    response = client.post(
        "/v1/run", headers=_auth(), json={"command": "delete-everything", "args": {}}
    )
    assert response.status_code == 422


def test_token_compared_in_constant_time():
    """``_token_ok`` compares with ``hmac.compare_digest``, never ``==``, so a
    wrong token leaks neither its length nor its matching prefix."""
    assert "compare_digest" in inspect.getsource(serve._token_ok)
    assert not serve._token_ok(None, "secret")
    assert not serve._token_ok("", "secret")
    assert not serve._token_ok("secre", "secret")
    assert not serve._token_ok("secrets", "secret")
    assert serve._token_ok("secret", "secret")
    # A non-ASCII offering must be a plain False, never a TypeError out of
    # compare_digest — that would be a 500 and a stack trace over the wire.
    assert not serve._token_ok("secr\u00e9t", "secret")


def test_wrong_host_header_is_refused(client: TestClient):
    """DNS rebinding: a browser page pointing a name it owns at 127.0.0.1 must
    not reach the sidecar even once it has guessed the token."""
    response = client.get("/v1/hello", headers={**_auth(), "Host": "evil.example.com"})
    assert response.status_code == 400
    # TestClient's own default name is refused too: no test-only bypass in the guard.
    assert client.get("/v1/hello", headers={**_auth(), "Host": "testserver"}).status_code == 400
    assert client.get("/v1/hello", headers={**_auth(), "Host": "127.0.0.1:8000"}).status_code == 200


def test_no_cors_middleware_is_mounted():
    """A browser must not be able to read this sidecar's replies at all."""
    app = create_app(TOKEN)
    assert not any(
        "CORS" in type(middleware.cls).__name__ or "CORS" in str(middleware.cls)
        for middleware in app.user_middleware
    )


def test_robot_mode_lists_write_commands_in_hello():
    robot = _client(create_app(TOKEN, robot=True))
    names = {c["name"] for c in robot.get("/v1/hello", headers=_auth()).json()["commands"]}
    assert serve._WRITE_COMMANDS <= names


@pytest.mark.parametrize("command", ["diff", "log"])
def test_robot_mode_lists_but_refuses_host_only_history(
    command: str, monkeypatch: pytest.MonkeyPatch
):
    robot = _client(create_app(TOKEN, robot=True))
    names = {c["name"] for c in robot.get("/v1/hello", headers=_auth()).json()["commands"]}
    assert serve._HOST_ONLY_COMMANDS <= names
    monkeypatch.setattr(
        serve.api,
        command,
        lambda *_args, **_kwargs: pytest.fail("robot refusal reached the history producer"),
    )
    response = robot.post(
        "/v1/run",
        headers=_auth(),
        json={"command": command, "args": {"contract": "lockwire.yaml"}},
    )
    assert response.status_code == 200
    assert response.json() == {
        "version": __version__,
        "cli_line": f"lockwire {command}",
        "exit_code": 2,
        "verdict": None,
        "payload": None,
        "refusal": f"host-only commands are not served on the robot: {command}",
    }


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("declare", {"contract": "x", "path": "p", "value": 1, "source": "s", "witness": "w"}),
        ("add", {"contract": "x", "kind": "bus", "source": "s", "witness": "w"}),
        ("review", {"contract": "x", "path": "p", "state": "approved", "witness": "w"}),
        (
            "commit",
            {
                "contract": "x",
                "message": "witness: w",
                "paths": ["x"],
                "expected_revision": "revision",
            },
        ),
        ("undo", {"contract": "x"}),
        (
            "place",
            {
                "contract": "x",
                "target": "d",
                "on": "link",
                "offset": [0, 0, 0],
                "unit": "mm",
                "source": "s",
                "witness": "w",
            },
        ),
        (
            "move",
            {
                "contract": "x",
                "target": "d",
                "offset": [0, 0, 0],
                "unit": "mm",
                "source": "s",
                "witness": "w",
            },
        ),
        (
            "rotate",
            {
                "contract": "x",
                "target": "link",
                "rpy": [0, 0, 0],
                "unit": "rad",
                "source": "s",
                "witness": "w",
            },
        ),
        ("reparent", {"contract": "x", "target": "d", "to": "link", "source": "s", "witness": "w"}),
        (
            "mount-shape",
            {
                "contract": "x",
                "target": "d",
                "shape": {"kind": "plate", "dimensions_mm": [1, 1, 1]},
                "source": "s",
                "witness": "w",
            },
        ),
    ],
)
def test_robot_mode_refuses_each_write_command_by_name(command: str, args: dict):
    robot = _client(create_app(TOKEN, robot=True))
    response = robot.post("/v1/run", headers=_auth(), json={"command": command, "args": args})
    assert response.status_code == 200
    assert response.json()["refusal"] == f"write commands are not served on the robot: {command}"


@pytest.mark.parametrize("command", ["report"])
def test_robot_renderer_cannot_replace_contract_through_out(tmp_path: Path, command: str):
    contract = tmp_path / "contract.yaml"
    shutil.copy(X500, contract)
    before = contract.read_bytes()
    robot = _client(create_app(TOKEN, robot=True))
    response = robot.post(
        "/v1/run",
        headers=_auth(),
        json={"command": command, "args": {"contract": str(contract), "out": str(contract)}},
    )
    assert response.status_code == 200
    assert response.json()["refusal"] == f"write commands are not served on the robot: {command}"
    assert response.json()["exit_code"] == 2
    assert contract.read_bytes() == before


def test_review_preserves_the_library_written_bytes(tmp_path: Path):
    contract = tmp_path / "contract.yaml"
    shutil.copy(X500, contract)
    response = _client(create_app(TOKEN)).post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "review",
            "args": {
                "contract": str(contract),
                "path": "devices/dev.companion.jetson-orin-nano/power/draw",
                "state": "questioned",
                "witness": "Maya Chen",
                "note": "datasheet p.2 says 20.0 W",
                "locked_at": "2026-09-08",
                "expected_revision": contract_revision(contract),
            },
        },
    )
    body = response.json()
    assert body["payload"]["written"] is True
    assert contract.read_bytes() == (FIXTURES / "review_note_expected.yaml").read_bytes()


@pytest.mark.parametrize("command", ["declare", "add", "review"])
def test_edit_write_handlers_return_the_library_document_verbatim(
    command: str, monkeypatch: pytest.MonkeyPatch
):
    document = {"artifact_type": "sentinel", "nested": {"bytes": [1, 2, 3]}}
    monkeypatch.setattr(serve.api, command, lambda *args, **kwargs: document)
    args = {
        "declare": {
            "contract": "c",
            "path": "p",
            "value": 1,
            "source": "s",
            "witness": "w",
            "expected_revision": "r",
        },
        "add": {
            "contract": "c",
            "kind": "bus",
            "source": "s",
            "witness": "w",
            "expected_revision": "r",
        },
        "review": {
            "contract": "c",
            "path": "p",
            "state": "approved",
            "witness": "w",
            "expected_revision": "r",
        },
    }[command]
    body = (
        _client(create_app(TOKEN))
        .post("/v1/run", headers=_auth(), json={"command": command, "args": args})
        .json()
    )
    assert body["payload"] == document


def test_commit_returns_the_vcs_document_verbatim(monkeypatch: pytest.MonkeyPatch):
    document = {"argv": ["git", "commit"], "committed": False, "sha": None}
    monkeypatch.setattr(serve.vcs, "commit_contract", lambda *args, **kwargs: document)
    body = (
        _client(create_app(TOKEN))
        .post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "commit",
                "args": {
                    "contract": "c",
                    "message": "witness: Maya",
                    "paths": ["c"],
                    "dry_run": True,
                    "expected_revision": "revision",
                },
            },
        )
        .json()
    )
    assert body["payload"] == document


def test_undo_plans_before_optional_apply_and_returns_that_plan(monkeypatch: pytest.MonkeyPatch):
    plan = {"kind": "restore", "argv": ["restore"], "scope": ["lockwire.yaml"]}
    planned: list[tuple[str, str]] = []

    def plan_undo(contract: str, *, source: str) -> dict:
        planned.append((contract, source))
        return plan

    monkeypatch.setattr(serve.vcs, "undo_plan", plan_undo)
    applied: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        serve.vcs, "apply_undo", lambda plan, **kwargs: applied.append((plan, kwargs))
    )
    desktop = _client(create_app(TOKEN))

    preview = desktop.post(
        "/v1/run", headers=_auth(), json={"command": "undo", "args": {"contract": "c"}}
    ).json()
    assert preview["payload"] == plan
    assert applied == []
    assert planned == [("c", "HEAD")]

    result = desktop.post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "undo",
            "args": {"contract": "c", "source": "HEAD~1", "apply": True, "expected_revision": "r"},
        },
    ).json()
    assert result["payload"] == plan
    assert applied == [(plan, {"contract": "c", "expected_revision": "r"})]
    assert planned == [("c", "HEAD"), ("c", "HEAD~1")]


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("declare", {"path": "p", "value": 1, "source": "s", "witness": "w"}),
        ("add", {"kind": "bus", "source": "s", "witness": "w"}),
        ("review", {"path": "p", "state": "approved", "witness": "w"}),
        ("commit", {"message": "witness: w", "paths": ["ignored"]}),
        ("undo", {"apply": True}),
        (
            "place",
            {
                "target": "d",
                "on": "link",
                "offset": [0, 0, 0],
                "unit": "mm",
                "source": "s",
                "witness": "w",
            },
        ),
        ("move", {"target": "d", "offset": [0, 0, 0], "unit": "mm", "source": "s", "witness": "w"}),
        (
            "rotate",
            {"target": "link", "rpy": [0, 0, 0], "unit": "rad", "source": "s", "witness": "w"},
        ),
        ("reparent", {"target": "d", "to": "link", "source": "s", "witness": "w"}),
        (
            "mount-shape",
            {
                "target": "d",
                "shape": {"kind": "plate", "dimensions_mm": [1, 1, 1]},
                "source": "s",
                "witness": "w",
            },
        ),
    ],
)
def test_sidecar_write_commands_refuse_missing_raw_revision_before_touching_bytes(
    tmp_path: Path, command: str, args: dict
):
    contract = tmp_path / "contract.yaml"
    shutil.copy(X500, contract)
    before = contract.read_bytes()

    body = (
        _client(create_app(TOKEN))
        .post(
            "/v1/run",
            headers=_auth(),
            json={"command": command, "args": {"contract": str(contract), **args}},
        )
        .json()
    )

    assert body["exit_code"] == 2
    assert (
        body["refusal"]
        == "expected_revision is required before applying a sidecar edit; reload first"
    )
    assert contract.read_bytes() == before


def test_sidecar_undo_requires_the_raw_revision_but_preview_stays_read_only(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(argv, cwd=repo, check=True)
    contract = repo / "lockwire.yaml"
    contract.write_text("name: base\n", encoding="utf-8")
    subprocess.run(["git", "add", contract.name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    contract.write_text("name: changed\n", encoding="utf-8")
    desktop = _client(create_app(TOKEN))

    preview = desktop.post(
        "/v1/run", headers=_auth(), json={"command": "undo", "args": {"contract": str(contract)}}
    ).json()
    assert preview["refusal"] is None
    assert contract.read_text(encoding="utf-8") == "name: changed\n"

    missing = desktop.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "undo", "args": {"contract": str(contract), "apply": True}},
    ).json()
    assert (
        missing["refusal"]
        == "expected_revision is required before applying a sidecar edit; reload first"
    )
    assert contract.read_text(encoding="utf-8") == "name: changed\n"

    stale = contract_revision(contract)
    semantic = contract_sha(contract)
    assert stale != semantic
    contract.write_text("# formatting only\nname: changed\n", encoding="utf-8")
    before = contract.read_bytes()
    for expected_revision in (stale, semantic):
        reply = desktop.post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "undo",
                "args": {
                    "contract": str(contract),
                    "apply": True,
                    "expected_revision": expected_revision,
                },
            },
        ).json()
        assert reply["refusal"] is not None and "reload" in reply["refusal"]
        assert contract.read_bytes() == before

    current = desktop.post(
        "/v1/run",
        headers=_auth(),
        json={
            "command": "undo",
            "args": {
                "contract": str(contract),
                "apply": True,
                "expected_revision": contract_revision(contract),
            },
        },
    ).json()
    assert current["refusal"] is None
    assert contract.read_text(encoding="utf-8") == "name: base\n"


def test_sidecar_stale_commit_refuses_before_touching_contract_or_git(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(argv, cwd=repo, check=True)
    contract = repo / "lockwire.yaml"
    contract.write_text("name: base\n", encoding="utf-8")
    subprocess.run(["git", "add", contract.name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    opened_revision = contract_revision(contract)
    contract.write_text("# formatting only\nname: base\n", encoding="utf-8")
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    before_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    before_bytes = contract.read_bytes()
    lock_path = contract.with_name(f".{contract.name}.lock")
    assert not lock_path.exists()

    reply = (
        _client(create_app(TOKEN))
        .post(
            "/v1/run",
            headers=_auth(),
            json={
                "command": "commit",
                "args": {
                    "contract": str(contract),
                    "message": "lockwire sidecar update; witness: Test Operator",
                    "paths": [str(contract)],
                    "expected_revision": opened_revision,
                },
            },
        )
        .json()
    )

    assert reply["exit_code"] == 2
    assert reply["payload"] is None
    assert reply["refusal"] == "contract changed since it was opened; reload before committing"
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
        == before_head
    )
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
        == before_status
    )
    assert contract.read_bytes() == before_bytes
    assert not lock_path.exists()


def test_exit_on_stdin_close_is_opt_in(start_serve):
    """Without the flag, a closed stdin must NOT kill the server — otherwise
    ``lockwire serve`` is unusable by hand or in CI, where stdin is /dev/null.

    This is the test that leaked every time before this box: it deliberately
    keeps the server alive past stdin closing, so it never reaches a self-exit
    to reap cleanly, and the old raw ``proc.kill()`` teardown only killed the
    ``uv`` wrapper (see the module comment above) — ``start_serve`` is what
    now guarantees the real server dies too."""
    assert inspect.signature(serve.run_server).parameters["exit_on_stdin_close"].default is False

    proc = start_serve("--port", "0")
    json.loads(proc.stdout.readline())
    proc.stdin.close()
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=3)


def test_ready_line_has_no_ansi(start_serve):
    """The parent parses this line; one escape byte from the rich console makes
    it unparseable. Written with ``sys.stdout.write``, never through a Console."""
    source = inspect.getsource(serve.run_server)
    assert "sys.stdout.write(" in source
    assert "Console(" not in source

    proc = start_serve(
        "--port",
        "0",
        "--exit-on-stdin-close",
        env={**os.environ, "FORCE_COLOR": "1", "CLICOLOR_FORCE": "1"},
    )
    line = proc.stdout.readline()
    assert re.search(r"\x1b\[", line) is None, repr(line)
    assert json.loads(line)["lockwire"] == "ready"
    proc.stdin.close()


def test_pyproject_serve_extra_and_dev_group_both_carry_fastapi_uvicorn():
    """CI runs a plain ``uv sync --dev``: without these in the dev group the whole
    of this file silently never runs, and without the extra a packaged app has no
    sidecar."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    extra = " ".join(data["project"]["optional-dependencies"]["serve"])
    dev = " ".join(data["dependency-groups"]["dev"])
    for package in ("fastapi", "uvicorn"):
        assert package in extra, f"{package} missing from the serve extra"
        assert package in dev, f"{package} missing from the dev group"


# ------------------------------------------------------- rotating sidecar log


@pytest.fixture
def sidecar_logger():
    """Isolate lockwire.serve's module logger: configure it fresh, then strip
    whatever handler this test attached so later tests never inherit it."""
    logger = logging.getLogger("lockwire.serve")
    yield logger
    logger.handlers = []


def test_rotating_log_writes_past_bound_and_caps_kept_files(tmp_path, sidecar_logger):
    """The whole box's claim: a size bound (not unbounded growth) and a small,
    capped number of kept files, oldest discarded — real files, not a config read.

    Volume is well past six rotations (~250 bytes/line x 12000 =~ 3MB against a
    200KB bound), not just past one — the retention count matters here, not only
    that rotation happened at all: weakening ``backupCount`` from 5 changes the
    file count this test asserts, which the box's r1 attempt did not (bounce,
    e1-log r1: 4000 lines produced 5 files under backupCount 5 *or* 500)."""
    log_path = serve._configure_sidecar_logging(tmp_path)
    line = "x" * 200
    for _ in range(12000):
        sidecar_logger.info(line)

    files = sorted(tmp_path.glob("sidecar.log*"))
    assert log_path in files
    assert len(files) > 1, "never rotated past the bound"
    assert len(files) == 6, f"expected base + backupCount(5) kept files, got: {files}"
    for f in files:
        assert f.stat().st_size <= 200_000 + 512, f"{f} grew past the size bound"


def test_rotating_log_never_contains_the_token(tmp_path, sidecar_logger):
    """The per-launch token authenticates every request; the access log
    line logs only method/path/status (serve.py's ``guard`` middleware) so a
    real request carrying the token in its header never reaches this file."""
    token = "super-secret-per-launch-token-do-not-log-me"
    app = create_app(token)
    serve._configure_sidecar_logging(tmp_path)
    with _client(app) as client:
        client.get("/v1/hello", headers={"X-Lockwire-Token": token})
        client.get("/v1/hello", headers={"X-Lockwire-Token": "wrong"})
        client.post(
            "/v1/run",
            headers={"X-Lockwire-Token": token},
            json={"command": "check", "args": {"contract": "missing.yaml"}},
        )

    log_text = (tmp_path / "sidecar.log").read_text()
    assert "GET /v1/hello -> 200" in log_text
    assert "POST /v1/run -> 200" in log_text
    assert token not in log_text


def test_refusal_responses_are_logged_too(tmp_path, sidecar_logger):
    """The guard middleware returns 400/401 *before* ``call_next`` runs — a user
    reporting "it stopped working" is overwhelmingly likely to be looking at a
    token or Host rejection (its whole reason to exist), and the r1 attempt's
    log line ran only on the success path, leaving those two cases silent."""
    token = "another-super-secret-token-do-not-log-me"
    app = create_app(token)
    serve._configure_sidecar_logging(tmp_path)
    with _client(app) as client:
        client.get("/v1/hello", headers={"X-Lockwire-Token": "wrong"})
        client.get("/v1/hello", headers={"Host": "evil.example.com", "X-Lockwire-Token": token})

    log_text = (tmp_path / "sidecar.log").read_text()
    assert "GET /v1/hello -> 401" in log_text
    assert "GET /v1/hello -> 400" in log_text
    assert token not in log_text


def test_plan_reply_steps_carry_targets_on_the_wire(client: TestClient, tmp_path: Path):
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "steps:\n  - kind: rail_load\n    rail: rail.servo-motor\n    draw: {value: 5, unit: W}\n"
    )
    response = client.post(
        "/v1/run",
        headers=_auth(),
        json={"command": "plan", "args": {"contract": str(X500), "plan": str(plan)}},
    )
    steps = response.json()["payload"]["steps"]
    assert steps[0]["targets"][0]["id"] == "rail.servo-motor"
