# Service logs — `service.logs`, `service.logs.follow`, event `service.log`

Logs are a property of the **running** service, not of its architecture, so they live in
the `service.*` namespace and are keyed by the **graph service id** the cron dataflow graph
uses: `arch:<id>` (an architecture manifest), `launchd:<label>`, `docker:<12>`,
`nomad:<job>`, `proc_<id>`. Enforcement lives with the architecture manifest: a local
service must declare log sinks or it is non-conforming — see
[architecture.md → Log capture](architecture.md#log-capture) for the manifest shape.

## Sink resolution by provider

| Service id | Sinks |
|---|---|
| `arch:<id>` | The manifest's declared sinks. When the manifest binds a launchd runtime, the plist's `StandardOutPath` / `StandardErrorPath` are added as `launchd_stdout` / `launchd_stderr` unless a declared sink already has that id or that (kind, path) |
| `launchd:<label>` | The plist's paths as `stdout` / `stderr` (kinds `launchd_stdout` / `launchd_stderr`); when a manifest binds this label, its declared sinks are unioned in. A label with no plist and no manifest has no sinks (4040) |
| `docker:*`, `nomad:*`, `proc_*` | **4042** `log capture is not implemented for this provider yet` — an honest error, never a fake empty list |

Anything else is **4001** (not a graph service id); an unknown `arch:` id is **4030**.
Plists are looked up in `~/Library/LaunchAgents`, then `/Library/LaunchAgents`,
`/Library/LaunchDaemons`.

## Methods

| Method | Params | Returns |
|---|---|---|
| `service.logs` | `service`, `sink?` (default: the first sink), `lines?` (default 200, max 2000), `cursor?` | `{service, sink: {id, kind, label, path, exists, size_bytes, modified_at}, sinks: [...all resolved...], lines: [str], cursor, truncated, rotated?, encoding: "utf-8-replace"}` |
| `service.logs.follow` | `service`, `sink?`, `enabled` (default true), `cursor?` | started/refreshed: `{service, following: true, sink, cursor}`; stopped: `{service, following: false, sink, stopped: bool}` |

Errors: **4029** missing `service`; **4001** bad params or not a graph id; **4030** unknown
`arch:` service; **4040** unknown sink id, or the service has no sinks; **4041** the sink has
no file yet; **4042** provider without log capture; **5044/5045** internal. Both run on the
RPC pool (`_LONG_HANDLERS`).

### Reads are bounded and never leave the resolved sinks

Without a `cursor`, `service.logs` returns the last `lines` **complete** lines, scanning at
most 4 MiB from the end of the active file, and `cursor` — the byte offset just after the
last complete line — to continue from. With a cursor it returns every complete line appended
since (bounded by `lines`; `truncated: true` when more remain, and the returned cursor resumes
exactly where the page ended). A file shorter than the cursor was rotated or truncated: the
read restarts from the tail with `rotated: true`. Bytes decode as UTF-8 with replacement;
a partial trailing line is never returned until its newline arrives. For a `directory` sink,
`sink.path` is the active file (newest by mtime). A caller names a sink by **id**; a path
where an id is expected is simply an unknown sink (4040).

### Follow

`service.logs.follow {enabled: true}` starts a daemon thread that polls the sink every
second and broadcasts the session-less event `service.log`:

```jsonc
{"service": "arch:demo", "sink": "app", "lines": ["…", "…"], "cursor": "18342"}
{"service": "launchd:com.example.cam", "sink": "stdout", "lines": ["fresh"], "cursor": "6", "rotated": true}
{"service": "arch:demo", "sink": "app", "lines": [], "cursor": "18342", "stopped": "idle-timeout"}
```

Events are coalesced (at most 500 lines each) and sent only when complete new lines exist.
One follower per `(service, sink)`: asking again refreshes its idle clock and returns the
same cursor. It stops on `{enabled: false}` or after ten minutes without a client asking
again; the final event carries `stopped` (`"stopped"` or `"idle-timeout"`). Followers are
daemon threads; they are not persisted across restarts and there is no explicit shutdown
hook — process exit ends them (the same posture as the wiki watcher).

## Capabilities

`gateway.capabilities` advertises `service: {methods: ["service.logs", "service.logs.follow"],
events: ["service.log"]}` and lists both methods in `capability_names`.

## Limitations

- Docker, Nomad and bare-process services return 4042; their log capture is not implemented.
- The cron graph node annotation does not yet carry sink ids for launchd nodes; a client
  resolves them by calling `service.logs` (the `sinks` array) for the node's id.
- A launchd plist that names no `StandardOutPath`/`StandardErrorPath` yields no derived sinks.
