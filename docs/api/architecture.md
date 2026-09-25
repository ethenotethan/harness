# `architecture.*` — per-service architecture models

A service that conforms to the architecture standard ships a compiler that emits an
**architecture model** — components, the system map with its invariants, data stores,
external systems and the CI plane (`architecture/model/model.json` in Portal is the
reference shape) — and a `--check` that fails when the committed model drifts from the
tree. Harness learns about such a service from a **manifest**, reads and snapshots its
model per revision, runs its check on demand, and hands all of it to Portal, which opens
the model from the service node on the dataflow graph. A checkout that is never pushed
and a GitHub repository are served by the same methods and are indistinguishable to the
client.

## Manifest

One JSON file per service under `~/.hermes/services/architecture/<id>.json`. The file
stem is the id (`^[a-z0-9][a-z0-9._-]{0,63}$`); the service appears on the graph as
`arch:<id>`.

```jsonc
{
  "name": "Portal",                                   // required — display name
  "description": "Native macOS/iOS client for …",     // required — markdown detail card
  "root": "/Users/me/Desktop/portal",                 // local checkout (never has to be pushed)
  "repository": "owner/name", "ref": "main",          // …or a GitHub repository at a ref
  "model": "architecture/model/model.json",           // default; relative, inside the service
  "check": ["python3", "scripts/build_architecture.py", "--check"],  // local only
  "source_files": ["scripts/build_architecture.py"],  // extra code shown on the node
  "inputs": [], "outputs": [], "side_effects": [], "relationships": []  // dataflow, as launchd sidecars
}
```

`root` or `repository` is required. A local service's revision is its git head (or a
content digest of the model when there is no repository); a GitHub service's revision is
the model's own `source_revision`, falling back to the ref. Malformed manifests are
logged and skipped, never fatal.

### Standalone or bound to a runtime service

A manifest describes a codebase. When nothing else on the gateway runs that codebase
(Portal, a library, a tool), the manifest is **standalone** and the graph draws it as its
own `arch:<id>` service node. When a process, Docker, Nomad or launchd provider already
reports the running service, the manifest **binds** to it and annotates that node instead
of adding a second one:

```jsonc
{
  "name": "Hermes Gateway",
  "description": "Hermes gateway architecture",
  "root": "/Users/me/.hermes/hermes-agent",
  "check": ["python3", "scripts/build_architecture.py", "--check"],
  "runtime": {"provider": "launchd", "id": "ai.hermes.gateway"}
}
```

The binding is typed and exact — never a display-name match. `runtime.id` is the
provider's own identity and resolves to the canonical graph id the provider emits:

| `runtime.provider` | `runtime.id` | Graph node |
|---|---|---|
| `launchd` | the launchd label | `launchd:<label>` |
| `docker` | the container id | `docker:<first 12 chars>` |
| `nomad` | the job id | `nomad:<job>` |
| `process` | the tracked process id | `proc_<id>` |

A binding with an unknown provider, a blank or non-string id, a prefixed id, or an
`arch:` id makes the manifest invalid (logged, skipped). Omit `runtime` when a standalone
codebase node is intended.

**Merge authority** for a bound manifest, applied in `tools/service_graph.py` after every
provider has reported and before the graph is built:

| Fact | Owner |
|---|---|
| graph node id, health/liveness, description, label | the runtime provider |
| inputs, outputs, side effects, relationships (topology) | the runtime provider — the manifest's dataflow is not drawn |
| `architecture` annotation | the manifest (`ref` is `arch:<manifest-id>`, plus `runtime`: the graph id) |
| `source_files` | deterministic union of both, deduplicated by normalized path |
| browse root | `arch-<manifest-id>` → the manifest's `root`, as for a standalone manifest |

If the bound runtime service is **absent** (not running, not reported), nothing is drawn
for the manifest — no `arch:` node is fabricated — and a warning names the target. The
model stays reachable through `architecture.describe`. If **two manifests bind one
runtime service**, neither is applied (a warning names both); there is no last-writer-wins.

The **RPC identity and the graph identity differ** for a bound manifest: `architecture.*`
methods take `arch:<manifest-id>` (or the bare id), while the node on `cron.graph` is the
runtime's id and carries `architecture.ref` pointing back at the manifest.

## On the dataflow graph

`cron.graph` and `code.graph` compose the four runtime providers with the manifests
(`tools/service_graph.collect_graph_services`). A standalone manifest is a service node
of its own; a bound manifest annotates the runtime node. Either way the node has
`kind: "service"`, its `source_files` (the model file plus any declared files, resolved
onto browse roots like a cron's scripts), and an `architecture` annotation:

```jsonc
"architecture": {
  "ref": "arch:portal", "source": "local", "revision": "62911e4…",
  "model": "architecture/model/model.json", "snapshots": 3,
  "runtime": "launchd:ai.hermes.gateway",           // bound manifests only
  "check": {"status": "passed", "checked_at": "2026-09-24T09:00:00+00:00"},
  "summary": {"components": 31, "nodes": 240, "invariants": {"total": 12, "holds": 12, "violated": []}, "gates": 14, …}
}
```

Like `source_files`, the annotation is node metadata and stays outside the changeset
commitment digest. A local service's checkout is also exposed as a read-only browse root
named `arch-<id>` for `files.list` / `files.read`.

## Compiling a Python service

A Portal-style compiler is Swift-specific. Any Python service gets a conforming model from
`tools/architecture_compile_python.py`: it reads the tree plus `architecture/config.json`,
extracts with `ast` (declarations, entrypoints, routes, subprocess spawns, file writes,
HTTP clients, sockets, threads and tasks, environment reads, external imports, enum-typed
state machines, persistent stores), draws the map (modules, classes, entrypoints,
endpoints, stores, resources, external packages) with provenance for every construction,
wires the declared gates into one merge gate, validates the result against the
`hermes.architecture` contract (`tui_gateway/architecture_contract.py`) and writes
`architecture/model/model.json`. `--check` fails when the committed model drifts.

```jsonc
// <service>/architecture/config.json
{
  "title": "Home Awareness", "description": "Camera capture, perception and room state.",
  "source_root": ".", "exclude": ["tests/**"],
  "components": [
    {"id": "capture", "label": "Capture", "description": "Frame capture and orchestration.", "patterns": ["capture/**", "main.py"]},
    {"id": "perception", "label": "Perception", "description": "Detection and tracking.", "patterns": ["perception/**"]}
  ],
  "declared": {                                    // optional: a person's assertions, every reference must resolve
    "nodes": [{"id": "declared:camera", "kind": "device", "label": "PTZ camera", "path": "capture/ptz.py", "line": 12}],
    "edges": [{"source": "class:capture:Capture", "target": "declared:camera", "relation": "reads"}],
    "flows": [{"id": "boot", "title": "Boot", "steps": [{"from": "class:capture:Capture", "to": "declared:camera", "relation": "reads"}]}]
  },
  "gates": {
    "commands": [{"id": "check", "name": "Model is current", "command": "python3 tools/architecture_compile_python.py . --check"},
                 {"id": "tests", "name": "Unit tests", "command": "python3 -m pytest -q", "role": "gate"}],
    "workflows": ".github/workflows"                // optional: GitHub jobs join the gates
  }
}
```

```sh
python3 tools/architecture_compile_python.py /path/to/service          # writes architecture/model/model.json
python3 tools/architecture_compile_python.py /path/to/service --check  # exit 1 when stale or non-conforming
```

Point the service's manifest at the model (`"model": "architecture/model/model.json"`) and at
the check (`"check": ["python3", "…/tools/architecture_compile_python.py", ".", "--check"]`).
Every file must belong to one component and at least one gate must be declared; the
compiler refuses to write a model that does not conform.

## Compiling any service: language packs

`tools/architecture_compiler` is the multi-language compiler: one language-independent core and one
**language pack** per language on a [tree-sitter](https://tree-sitter.github.io) floor. Supported today:
Swift (`.swift`), TypeScript/JavaScript (`.ts .tsx .js .jsx .mjs .cjs`), Go (`.go`), Rust (`.rs`) and
Python (`.py`). A service may mix languages; every file is parsed by the pack for its extension.

```
python3 -m pip install -r tools/architecture_compiler/requirements.txt   # tree-sitter + grammar bundle
python3 -m tools.architecture_compiler <service-root>                   # writes architecture/model/model.json
python3 -m tools.architecture_compiler <service-root> --check           # exit 1 when the committed model is stale
```

The config is the Python compiler's `architecture/config.json` (see above) plus four keys:

```json
{
  "external_systems": [
    {"id": "camera", "label": "Tapo camera", "category": "cameras", "description": "RTSP feed.",
     "signatures": [{"pattern": "rtsp://", "scope": "strings"}, "\\bcv2\\b"]}
  ],
  "external_groups": [{"id": "sensing", "label": "Sensing", "categories": ["cameras"]}],
  "local_modules": ["example.com/demo"],
  "rules": [
    {"id": "publish", "description": "A room-state publish", "family": "boundary", "language": "python",
     "regex": "\\bpublish_state\\(", "scope": "code",
     "produces": {"kind": "resource", "sub_kind": "publisher", "relation": "notifies", "edge_class": "interplay"}},
    {"id": "handlers", "description": "Every handler class", "family": "wiring", "language": "python",
     "query": "(class_definition name: (identifier) @label) @site"}
  ]
}
```

- **`external_systems`** are the extrinsic dependencies a person names, observed through **signatures**:
  regexes matched against comment-masked code (`scope: code`, the default) or against string-literal
  contents only (`scope: strings`, for hostnames and URL schemes). A declared system with **zero hits fails
  the compile**. Each system becomes an `external:<id>` node wired by `uses` (class `boundary`) from every
  type or module that cites it, and a row in `externals.systems` with hit and file counts. A signature that
  matches an imported package's root **absorbs** that package: no separate `external:<package>` node is
  drawn for it. Packages no system claims still appear, uncategorised, so nothing is hidden.
- **`external_groups`** put categories into boundary hulls (`interplay.boundary_groups`), the way Portal draws
  *Platform storage* and *On-device inference*. A group with no members, or a category claimed by two groups,
  fails the compile.
- **`local_modules`** lists import prefixes that belong to the service itself (a Go module path, a Rust
  crate, an internal npm scope) so they never count as external.
- **`rules`** add project-specific passes on top of the pack's baseline. A rule is a regex (`scope`
  `code` | `raw` | `strings`) or a tree-sitter `query` whose `@site` capture is the citation and optional
  `@label` the label. `produces` puts a construction on the map (`external`, `store`, `resource`,
  `endpoint`) with the relation and edge class the owner is wired by; without it the rule only cites,
  which still maps the enclosing declaration. Rule ids are namespaced by language (`python.publish`).
- **`languages`** (optional) restricts which packs run.

Every pack provides the same baseline passes where the concept exists — `<lang>.type`, `<lang>.entrypoint`,
`<lang>.import_external`, `<lang>.route`, `<lang>.http_client`, `<lang>.socket_or_server`,
`<lang>.file_write`, `<lang>.process_spawn`, `<lang>.env_read`, `<lang>.persistent_store`,
`<lang>.concurrency`, `<lang>.state_machine` — and Swift adds Portal's own conventions:
`swift.store_declaration` (`*Store/*Cache/*Inventory/*Ledger` types become store nodes), `swift.ui_trigger`
(a SwiftUI action or lifecycle closure calling a same-type property's method draws `drives`) and
`swift.rpc_call` (`call("namespace.method")` draws an endpoint the caller `invokes`). Intrinsic versus
extrinsic is expressed exactly as Portal expresses it: edge `class` (`structure`/`lifecycle`/`interplay`
inside, `boundary` across) plus the boundary hulls.

Adding a pack means one module under `tools/architecture_compiler/packs/`: the grammar name, the comment and
string node types (so signatures can be scoped), a declaration census, an import reader, an entrypoint
finder and the rule table; register it in `packs/__init__.py`. The compiler validates its output against
the contract and refuses to write a non-conforming model; two compiles of one tree are identical bytes.

What the passes cannot see, stated in every model's `evidence_metadata.limitations`: dynamic dispatch,
reflection and plugin registries, routes assembled at runtime, decorators built at runtime, macro and
derive expansions, and any mechanism reached through a wrapper in another file. Portal keeps its own
Swift compiler until the Swift pack plus a project rule table reproduces its map.

## Methods

| Method | Params | Returns |
|---|---|---|
| `architecture.list` | — | `{services: [{id, label, description, source, root, repository, ref, model, check_configured, runtime, status}]}` — `status` is the node annotation above; `runtime` is the binding (`{provider, id, graph_id}`) or null |
| `architecture.describe` | `service` (graph id or bare id), `revision?` | `{service: {…}, revision, source, stored_at, summary, check, model}` — without `revision` the current model is read now and snapshotted; with it a stored snapshot is returned. **4032** model missing/invalid/unfetchable, **4404** no such stored revision |
| `architecture.check` | `service` | `{service, check: {status: passed\|failed\|unavailable, exit_code?, output?, reason?, revision, checked_at, duration_s, command}}` — runs the manifest's `check` in `root`, bounded at 300 s. A GitHub service or a manifest without `check` is `unavailable` with a `reason` |
| `architecture.history` | `service` | `{service, source, latest, revisions: [{revision, source, stored_at, summary}], checks: [runs, newest first]}` |

Common errors: **4029** missing `service`, **4030** unknown service; **5040–5043**
internal, per method. All four run on the RPC pool (`_LONG_HANDLERS`).

`summary` is derived from the model without the client loading it: schema version,
component/file/line counts, map nodes/edges/flows, invariants with the violated ids,
store and external counts, and the CI plane's gate/ratchet/workflow counts.

## Snapshots and checks

Every revision read is stored under `~/.hermes/architecture/<id>/<revision>.json` with
`index.json` (latest, revisions with summaries). At most 30 snapshots are kept per
service; pruning removes the oldest **after the first**, so genesis is never lost. Check
runs land in `checks.json` (20 newest).

## Event

`architecture.changed` — session-less, broadcast to every client:

```jsonc
{"service": "arch:portal", "revision": "62911e4…", "source": "local", "reason": "snapshot"}
{"service": "arch:portal", "revision": "62911e4…", "source": "local", "reason": "check", "status": "failed"}
```

Emitted when `architecture.describe` stores a revision it had not seen and when
`architecture.check` finishes. Clients refetch via `architecture.describe`.

## Capabilities

`architecture.list`, `architecture.describe`, `architecture.check`, `architecture.history`
are advertised by `gateway.capabilities`.

## Security

Manifests are operator-owned state under `HERMES_HOME`. Model paths are resolved inside
the declared root and rejected when they escape it; models are capped at 16 MiB and must
be JSON objects with a `schema_version`. Check commands run only for local services, in
the declared root, with output truncated to 4 000 characters. GitHub fetches go to
`raw.githubusercontent.com` over HTTPS and send `GITHUB_TOKEN`/`GH_TOKEN` when set.

Implementation: `tui_gateway/architecture_store.py` (manifests, bindings, snapshots,
checks), `tools/architecture_services.py` (definitions), `tools/service_graph.py`
(runtime providers + composition), `tui_gateway/methods_architecture.py` (handlers); tests
in `tests/gateway/test_architecture_store.py`, `tests/gateway/test_methods_architecture.py`,
`tests/cron/test_architecture_services.py`, `tests/cron/test_architecture_binding.py`.
