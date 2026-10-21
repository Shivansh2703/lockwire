# `lockwire.api` — the library door

lockwire answers the same questions through four doors: the command line, the MCP
server, the HTTP sidecar (`lockwire serve`, Lockwire Pro), and this module. There is exactly **one implementation behind all four**, so
`lockwire.api` *wraps* the existing checks and never recomputes them.

Two properties this module is responsible for:

- **Each function returns exactly the document the CLI's `--json` writes.** A
  library caller and a terminal user are never told different things about one
  run. `tests/test_api_library.py` compares them byte for byte.
- **`import lockwire.api` reaches no command line and no web server** — not
  `typer`, `rich`, `fastapi`, `uvicorn` or `mcp`. Importing lockwire into a
  script must not drag a terminal renderer or an HTTP stack in behind it. This
  is why `report.py` imports `rich` lazily; breaking it fails
  `test_library_import_pulls_no_server_or_cli_deps`.

```python
import lockwire.api as lw
```

## `load(contract) -> Contract`

The validated contract object, exactly what `schema.load_contract` returns. Every
other function accepts either a path or an already-loaded `Contract`, so a caller
making several calls loads once.

```python
contract = lw.load("lockwire.yaml")
print(contract.robot.name, len(contract.devices))
```

## `check(contract) -> dict`

Record-level checks — do the records agree? The document `lockwire check --json`
writes.

```python
document = lw.check("lockwire.yaml")
print(document["verdict"], document["exit_code"])   # e.g. NO_GO 1
for finding in document["findings"]:
    print(finding["severity"], finding["quantity"], finding["observed"])
```

## `what_if(contract, changes) -> dict`

Compares current and proposed record checks without writing the contract or its
health ledger. Pass existing `lockwire.edit.Change` records. The enveloped
`lockwire-what-if-report` separates current/proposed verdicts, findings, passed
comparisons and per-fact provenance states; absent evidence stays explicit.
It does not claim robot conformance or simulation validity. The CLI uses repeated
`--change PATH=VALUE` with `--source` and `--witness`; exit 0 means the comparison
was produced, while each side carries its own verdict. Exit 2 refuses the input.


## `plan(contract, plan) -> dict`

Scores an agent's intended plan against the contract before anything executes.
`plan` is a `plan.yaml` path (what the CLI has) or an already-parsed dict (what
MCP and the sidecar have, inline in a request body) — one function, both callers.

```python
document = lw.plan("lockwire.yaml", {"steps": [
    {"kind": "rail_load", "rail": "rail.servo-motor", "draw": {"value": 500, "unit": "W"}},
]})
print(document["verdict"], document["outcomes"])
```

## `describe(contract) -> dict`

Every generated reference file, keyed by filename — the same files
`lockwire describe --out <dir>` writes.

```python
files = lw.describe("lockwire.yaml")["files"]
print(sorted(files))                 # ['SYSTEM.md', 'dev.imu.md', ...]
print(files["SYSTEM.md"][:200])
```

## Not in the free library

`preflight` and `health` are part of Lockwire Pro and are **not** on `lockwire.api`
in the free package: `lw.preflight(...)` and `lw.health(...)` raise
`AttributeError` there. The free library's full list is `lw.FUNCTIONS`.

## `declare(contract, path, value, *, source, witness, ...) -> dict`

The write door. It goes through `edit.apply_changes`, which owns the
source-and-witness gate: a missing or empty `source` or `witness` raises
`EditRefusal` and nothing is written. This wrapper substitutes no placeholder —
a fact with no one behind it does not enter the contract.

```python
result = lw.declare(
    "lockwire.yaml",
    "devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
    "0.12 0 0.05 m",
    source="hand measurement",
    witness="shivansh",
    dry_run=True,          # print the diff, write nothing
)
print(result["written"], result["diff"])
```

## The other write functions

Every one takes the contract path first, needs a `source` and a `witness`, and
accepts `dry_run=True` (show the diff, write nothing) and
`expected_revision=` (refuse if the file changed since you read it). Each
returns the same document as the matching CLI command's `--json`.

- `add(contract, kind, *, id=None, source, witness, **fields)` — a device, rail, bus, family, model, budget or battery block.
- `review(contract, path, state, *, witness, note=None)` — approve or question one declared value.
- `place(contract, target, on, offset, *, unit, source, witness)` — put a device on a link with its offset.
- `move(contract, target, offset, *, unit, source, witness)` — move a device in its mount frame, or a link in its parent's.
- `rotate(contract, target, rpy, *, unit, source, witness)` — set a link's orientation; the unit is never assumed.
- `reparent(contract, target, to, *, source, witness)` — change a device's mount link or a link's joint parent.
- `mount_shape(contract, target, shape, *, source, witness)` — set a device's mount geometry.

```python
result = lw.move(
    "lockwire.yaml",
    "dev.flight-controller.pixhawk-6c",
    (0.01, 0.0, 0.0),
    unit="m",
    source="hand measurement",
    witness="shivansh",
    dry_run=True,
)
print(result["written"])   # False
```

## Starting, importing, upgrading

- `start(urdf, *, overwrite=False, expected_revision=None)` — write a first lockfile beside a robot model. It never replaces an existing lockfile unless `overwrite=True`.
- `from_parts(text, *, out=None, catalog=None)` — match a pasted parts list to the catalog; with `out`, also write a draft lockfile.
- `upgrade(contract, *, out, expected_revision)` — write a new lockfile from an older-format one without changing the original.
- `api_schema(out)` and `api_types(out)` — write the JSON schema and the TypeScript types for the documents above.
- `what_if(contract, changes)` is described above; `is_stale(document, contract)` says whether a document you hold is older than the file on disk.
- `verdict_document(...)` and `check_document(...)` build the findings documents that every door shares; most callers want `check` instead.

```python
rows = lw.from_parts("Pixhawk 6C\nOAK-D Lite")
print(sorted(rows))
```

`contract_sha`, `declare_form`, `load_scene` and `page` exist in the module but are
deliberately outside the public list (`lw.__all__`) and may change.

## Git history

`api.diff`, `api.log` and `api.blame` are the library twins of the corresponding
CLI commands. `blame` returns `(rows, in_repository)`; each row preserves the
declared value's source and witness and includes `last_changing_commit` only when
Git can attribute it honestly.

## Document shape

Every document lockwire hands a machine — read or write, library or CLI or MCP —
leads with the same three-field header, built in one place, `report_json.envelope`
(E0-25): `artifact_type`, `format_version` (`api.DOCUMENT_VERSION`, the document
shape) and `schema_version` (the contract's, **separate** and reported alongside).
The rest of the document is merged flat behind it, so a body key that collides
with a header key would silently displace one; `envelope` refuses that with a
`ValueError` rather than emitting a document whose version is a body field.

Documents built from a loaded contract then add `robot`. Findings documents add
`verdict`, `exit_code`, `counts`, `scored` and `findings`; plan documents add
`outcomes`. The MCP server returns these documents with one extra field,
`cli_line`, the paste-able command that reproduces the call.


Scene replies retain the geometry `hash` and separately expose `asset_path`,
`asset_sha256` and `expected_asset_sha256`. The observed digest binds only the
selected root file's parsed bytes, not external buffers, textures or meshes.
An expectation applies only when its resolved path exactly matches the declared
model source; mismatches refuse. Unpinned selections report a null expectation.
Geometry-unchanged replies still carry the root file identity.

The existing `blame --json` row list has a named `BlamePayload` carrier in the
shared schema and generated types. `api.blame` still returns `(rows, in_repository)`.
Rows preserve declared values and provenance verbatim; an unknown commit remains
absent. This carrier adds no app action or provenance inferred from git authors.
