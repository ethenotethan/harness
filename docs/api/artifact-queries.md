# `artifact.query.*` — structured reads for living artifacts

The read-side twin of `artifact.action.*`. Where an intent lets a rendered
artifact *command* the backend, a query lets it *read* — with the discipline
of a persisted query behind an API: the caller names a declared slot and
supplies typed variables; the server resolves the handler, validates, runs,
and returns data. See [plugins/queries.md](../plugins/queries.md) for the
authoring side.

## Manifest

`queries` on the artifact record (set through `artifact.set` / the
`artifact` tool, shape-checked at write, carried forward when omitted):

```jsonc
[{ "id": "open-orders",             // what the page refers to
   "query": "postgres.orders.open", // registered handler; never query text
   "bind": {"state": "open"},       // fixed by the author, not overridable
   "params": {"limit": {"type": "int", "min": 1, "max": 200}},  // narrows the handler's schema
   "live": {"mode": "poll", "interval_s": 30},                  // or {"mode": "subscribe"}; omit for one-shot
   "invalidated_by": ["archive-order"] }]                       // intents whose success re-runs this
```

## Methods

| Method | Params | Returns |
|--------|--------|---------|
| `artifact.query.invoke` | `artifact_id`, `artifact_rev`, `query_id`, `params?` (object), `cursor?` | `{status, data, etag, params, duration_ms, next_cursor?}` — see statuses |
| `artifact.query.subscribe` | same as invoke (no `cursor`) | the invoke result plus `subscription` (handle) and `interval_s` (`null` for push-only) |
| `artifact.query.unsubscribe` | `subscription` | `{status: "ok", removed}` |
| `artifact.query.handlers` | — | `{handlers: [{name, params}]}` — registered handler names and schemas |

Statuses: **`ok`** · **`failed`** (`reason`: parameter rejected, handler
error, result over 256 KB, rate limited — 30 calls / 10 s per slot) ·
**`conflict`** (artifact changed since the page rendered; refresh and resend
with the new `artifact_rev`; `artifact_rev: null` skips the check) ·
**`unsupported`** (`reason`: no such declaration, or its handler isn't
registered). Malformed requests are JSON-RPC **4001**; internal failures
**5220–5223**.

Parameter validation runs the artifact's `params` schema and then the
handler's own; unknown keys, out-of-range values and attempts to override a
`bind` are all `failed` with a reason naming the parameter.

## Event

`artifact.query.changed` — broadcast when a subscribed slot's result changed:

```jsonc
{"artifact_id": "dash", "query_id": "open-orders", "params_hash": "…", "etag": "…", "status": "ok"}
{"artifact_id": "dash", "query_id": "open-orders", "params_hash": "…", "status": "unsupported", "reason": "…"}
```

Clients re-issue `artifact.query.invoke` for the matching slot on `ok`;
`unsupported` means the slot has been dropped server-side. Emission is
etag-diffed: a poll that returns identical data emits nothing. A new artifact
revision (`artifact.set`) marks that artifact's slots due immediately, as does
a plugin calling `mark_query_changed`.

## Capabilities

`gateway.capabilities` advertises `artifact.query`, `artifact.query.invoke`,
`artifact.query.subscribe`, `artifact.query.handlers`. Clients gate the
bridge on `artifact.query`.

## Built-in handler

`artifact.rows` — the entries of a dataset / map / checklist / kanban /
calendar / model artifact, tombstones omitted, offset-paginated. Params:
`source` (artifact id, required), `set` (model entity set), `limit`
(1–1000, default 100). Lets an HTML dashboard read the artifacts the agent
already maintains with no database at all.
