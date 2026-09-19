# Artifact query plugins

The **read side** of artifact intents. An intent is a button that commands
the backend; a query is a slot that reads from it — an HTML dashboard asking
for "open orders, 100 rows" and getting JSON back, continuously, from a
database or service the gateway holds the credentials for.

The mental model is the one every full-stack app already uses across a
service boundary: **the browser never sends SQL.** It calls a named endpoint
with typed variables; the API layer validates, executes, and pushes results
back over a subscription. Here the *plugin* is the endpoint, the *artifact*
is the client's declared contract, the *page* supplies variables, and the
gateway sits in between validating everything.

| Party | Owns | May never |
|-------|------|-----------|
| Plugin (`~/.hermes/plugins/actions/*.py`) | the statement, the connection, the credentials, the parameter schema | — |
| Artifact (`queries` on the record) | which handlers its page may call; narrowing and binding of their parameters | carry SQL, credentials, or a handler that isn't registered |
| Page (`data-hermes-query`) | parameter *values* | name a handler, widen a schema, override a bound value, receive markup |

## Registering a handler

Same directory and same reload model as [action plugins](actions.md).
`register_query_handler(name, fn, params=schema)` is pre-bound in the plugin
namespace, alongside `mark_query_changed` and `QueryError`:

```python
def _open_issues(artifact_id, query_id, params, cursor):
    rows = linear.issues(state=params["state"], first=params["limit"], after=cursor)
    return {"data": {"rows": rows.nodes}, "next_cursor": rows.end_cursor}

register_query_handler(
    "linear.issues.list", _open_issues,
    params={
        "state": {"type": "enum", "values": ["open", "closed"], "default": "open"},
        "limit": {"type": "int", "min": 1, "max": 250, "default": 50},
    },
)
```

- `fn(artifact_id, query_id, params, cursor)` returns `{"data": <json>,
  "next_cursor": <str|None>}`. `params` arrives **already validated** against
  your schema (and the artifact's narrowing of it). Raise `QueryError("…")`
  for a user-facing refusal; any other exception is reported as `failed`.
- `params` is the handler's authoritative schema. Types: `string` (`max`),
  `int` / `number` (`min`, `max`), `bool`, `enum` (`values`), `cursor`. Each
  may carry `required` and `default`. A handler registered with no schema
  takes no parameters.
- Results are JSON only, capped at 256 KB. Paginate rather than dump.
- **Read only.** If it mutates, it is an intent — register it with
  `register_handler` so the confirmation flow applies.

## Declaring queries on an artifact

`queries` sits on the artifact record next to `actions`, pinned to the
revision, and travels through `artifact.set` (RPC) or the `artifact` tool:

```json
[
  {
    "id": "open-orders",
    "query": "postgres.orders.open",
    "bind": { "state": "open" },
    "params": { "limit": { "type": "int", "min": 1, "max": 200 } },
    "live": { "mode": "poll", "interval_s": 30 },
    "invalidated_by": ["archive-order"]
  }
]
```

- `query` names a registered handler. `artifact.query.handlers` lists them.
- `bind` fixes parameters the page cannot change. A page that sends a
  different value for a bound key gets `failed`, not a silent override.
- `params` may **narrow** the handler's schema (tighter bounds, fewer enum
  values). Both schemas are enforced; the artifact cannot widen.
- `live` makes the slot subscribable: `{"mode": "poll", "interval_s": N}`
  (clamped to 5 s – 1 h) or `{"mode": "subscribe"}` for push-only handlers
  that call `mark_query_changed`. Omit `live` for one-shot reads.
- `invalidated_by` lists intent binding ids whose success should re-run this
  query — the write side telling the read side it's stale.

## Wiring the page

```html
<section data-hermes-query="open-orders" data-hermes-params='{"limit": 50}'>
  <script type="application/json" data-hermes-sink></script>
  <table id="orders"></table>
</section>
<script>
  document.querySelector('[data-hermes-query="open-orders"]')
    .addEventListener('hermes-data', (e) => {
      const sink = e.currentTarget.querySelector('[data-hermes-sink]');
      const { rows } = JSON.parse(sink.textContent);
      render(rows);
    });
</script>
```

Native watches `data-hermes-query` / `data-hermes-params` in its isolated
content world, validates the parameters, calls `artifact.query.invoke`, and
writes the result into the sink as **text** (`textContent`, never
`innerHTML`) before dispatching `hermes-data`. Changing `data-hermes-params`
re-runs the query. `data-hermes-query-status` on the element reflects
`loading | ok | failed | unsupported`, and `data-hermes-query-error` carries
the reason for the last two. The page's own JavaScript renders; no gateway
object, credential, or RPC name ever reaches it.

## Continuous data

`artifact.query.subscribe` registers a (query, params) slot. The **gateway**
re-runs it — on the declared cadence, or immediately when a plugin calls
`mark_query_changed("postgres")` from a LISTEN/webhook thread — and emits
`artifact.query.changed` only when the result's etag differs. Polling happens
where the credentials live; the client never polls, it re-fetches on the
event. Slots are dropped when the last subscriber leaves, and a slot whose
handler disappears on reload reports `unsupported` once and stops.

## Reference plugin: Postgres

[`postgres_queries/postgres_queries.py`](postgres_queries/postgres_queries.py)
turns a directory of [`statements/*.sql`](postgres_queries/statements/) files
into `postgres.<name>` handlers. Each
statement leads with its parameter schema in a `-- params:` header and uses
psycopg's `%(name)s` placeholders. Connections are opened read-only with a
statement timeout; an optional `LISTEN` thread turns database `NOTIFY`s into
`mark_query_changed`. Adding a query to your database is adding a file to
that directory — and only a human can write there.
