# Generating the TypeScript types

**Corrected 2026-09-08 (e1-d3):** this used to describe a two-step pipeline —
`lockwire api-schema` piped through the third-party `openapi-typescript` npm package. That is
**not** what ships. Plan v8 line 265, verbatim: *"There is no TypeScript generator in the list,
deliberately: the mainstream generator's own documentation says a discriminated union like ours
comes out in a shape TypeScript cannot narrow. The Python step that builds the union emits the
TypeScript directly (about forty lines), and CI fails if the committed file is stale."* Measured
the same day: the npm pipeline this note used to describe does not even run here —
`openapi-typescript@7.13.0` peers on `typescript@^5.x` and crashes against `app/package.json`'s
already-pinned `typescript@7.0.2` (`Cannot read properties of undefined (reading
'createKeywordTypeNode')`). One more reason, not the only one, to keep zero new npm dependencies.

1. `uv run lockwire api-types --out app/src/shared/api.gen.ts` — walks the payload **union**
   (`src/lockwire/api_payload.py`, via `src/lockwire/emit_ts.py`) straight into TypeScript: one
   `export interface` per union member, and a `MachineDocument` alias TypeScript narrows on the
   literal `artifact_type` field each member leads with. No npm package sits between the Python
   union and the app's types.
   Two generated files per edition: free members go to `app/src/shared/api.gen.ts`, the paid
   plugin's members to `app/src/shared/pro/api.gen.ts`;
   `api-schema` likewise writes `schema.json` and `pro/schema.json`. The public snapshot drops the `pro/` ones.
2. Re-run whenever `api_payload.py` or `api_payload_pro.py` changes — the drift test (`tests/test_emit_ts.py`,
   `test_committed_api_gen_ts_is_not_stale`) is what tells you it changed, and CI regenerates and
   diffs on every run.

`uv run lockwire api-schema --out ... ` still exists, unrelated to this file's generation: it
writes the same union as an OpenAPI-shaped JSON Schema document, for a consumer that wants the
`x-lockwire-*` metadata below at runtime rather than TypeScript's compile-time types.

Three keys the emitter reads beyond `components.schemas`: `x-lockwire-members` maps each
`artifact_type` to its member, so the renderer narrows a document by its own header rather than by
guessing; `x-lockwire-number-kinds` is the table (`Model.field` → declared/measured/derived/
catalog/projected, plus the word to render for the last two), so a catalog or projected figure is
never drawn in the measured style; `x-lockwire-extension-points` names slots deliberately left
empty (O33's accessibility strings today).

`Figure` is the shape to render, not to read past: `{value, unit, kind, by, mark?}`, one number on
screen with its own number-kind (*the number kind is a claim about numbers ON SCREEN, so a
preformatted string figure defeats it*). **The renderer formats it** — `value.toFixed(2) + " " +
unit`, which is `report.format_magnitude`'s one rule and nothing else — and draws `mark` beside a
`catalog` or `projected` figure. A field typed `Figure` deliberately has no
`x-lockwire-number-kinds` row: the kind is on the value. Conversely a `string` field in the schema
is prose, an identifier or a date, never a number the app should parse
(`api_payload.STRING_FIGURE_EXEMPTIONS` names every string that still opens with a digit, with why).

No JavaScript is written or run by this repo's Python tooling; `lockwire api-types` writes
TypeScript source text, and nothing here invokes `npm`/`npx`.
