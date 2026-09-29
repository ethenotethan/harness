# `/v1/ws` — JSON-RPC over WebSocket

`GET /v1/ws` upgrades to a WebSocket that speaks the same JSON-RPC 2.0 protocol
as the stdio TUI gateway (`tui_gateway.server.dispatch`). Native clients
(Portal on macOS/iOS, HermesNative) open **one** connection and run every RPC
and event stream over it. Authentication is the same `Authorization: Bearer
<API_SERVER_KEY>` header as the HTTP routes and is checked before the upgrade.

On connect the server sends one `gateway.ready` event. After that each text
frame is one request; responses and events are text frames in the other
direction. Malformed JSON is answered with `-32700 parse error` (`id: null`).

## Connection concurrency

Requests on a connection are **dispatched concurrently**, not in order. The
read loop keeps reading frames while handlers run; each request is dispatched
on a worker thread in its own task, and the response is written when that
handler returns. Clients must therefore correlate by `id`, never by arrival
order — a `session.list` that takes 20 s no longer delays the `prompt.submit`
or `cron.manage` sent after it, and client pings keep flowing while long
handlers run.

What is still guaranteed:

- All frames on a connection go through a single writer, so frames are never
  interleaved or corrupted.
- Events a handler emits (through its bound transport) are written before that
  handler's own response.
- Methods in `tui_gateway.server._LONG_HANDLERS` behave exactly as before:
  `dispatch` returns `None` and the handler's pool worker writes the response.

Per-connection concurrency is bounded by a semaphore; requests beyond the bound
queue in arrival order. When the socket closes, the server stops waiting on
in-flight requests and drops their responses (the thread work itself runs to
completion), then unregisters the transport and detaches it from any sessions.

## Environment

| Variable | Default | Effect |
| --- | --- | --- |
| `HERMES_WS_MAX_INFLIGHT` | `8` | Maximum requests one `/v1/ws` connection runs at once. Values below 1 are clamped to 1; non-numeric falls back to the default. Read per connection. |
| `HERMES_SERVICE_GRAPH_TTL` | `10` | Seconds `cron.graph` / `code.graph` reuse the runtime liveness probes (Docker, Nomad, launchd, process registry) before re-running them. `0` disables the cache. Architecture manifests are re-read on every call. Read per call. |
