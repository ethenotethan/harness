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

## Methods

| Method | Params | Returns |
|---|---|---|
| `architecture.list` | — | `{services: [{id, label, description, source, root, repository, ref, model, check_configured, runtime, status}], contract: {name, version}}` — `status` is the node annotation above (with `contract` and `conforming`); `runtime` is the binding (`{provider, id, graph_id}`) or null |
| `architecture.describe` | `service` (graph id or bare id), `revision?` | `{service: {…}, revision, source, stored_at, summary, check, contract, model}` — without `revision` the current model is read now, validated against the contract and snapshotted; with it a stored snapshot is returned. **4032** model missing/invalid/unfetchable, **4033** does not conform to the contract (message names the problems), **4404** no such stored revision |
| `architecture.check` | `service` | `{service, check: {status: passed\|failed\|unavailable, exit_code?, output?, reason?, revision, checked_at, duration_s, command}}` — runs the manifest's `check` in `root`, bounded at 300 s. A GitHub service or a manifest without `check` is `unavailable` with a `reason` |
| `architecture.history` | `service` | `{service, source, latest, revisions: [{revision, source, stored_at, summary}], checks: [runs, newest first]}` |

Common errors: **4029** missing `service`, **4030** unknown service; **5040–5043**
internal, per method. All four run on the RPC pool (`_LONG_HANDLERS`).

`summary` is derived from the model without the client loading it: schema version,
component/file/line counts, map nodes/edges/flows, invariants with the violated ids,
store and external counts, and the CI plane's gate/ratchet/workflow counts.

## Contract

Every model the gateway reads is validated against **`hermes.architecture` v1.0**
(`tui_gateway/architecture_contract.py`; JSON Schema export in
`docs/api/architecture-document-v1.schema.json`, digest `b89978fcf4c8fed4f4f90a516c971d9c8950522040342ee853f64db3e2686541`).
The module is the contract between a service's compiler, this gateway and every
renderer; it is vendored **byte-for-byte** into each consumer and pinned by digest there
(Portal: `architecture/contract/pins.json`), so a contract change lands in every
repository in lock-step rather than as a drift one side discovers at decode time.

**Required sections** — a service either proves where its map came from and what defends
it, or it is not a conforming service:

| Section | What it must carry |
|---|---|
| `components` | ≥1 component: `id`, `label`, `description`, `files`, `declarations`, counts |
| `interplay` | the system map: `nodes` (≥1), `edges` (endpoints are nodes), `flows` (steps end on nodes by `history_key`/`id` or `page:<id>`), `invariants` (≥1; status `holds`/`violated`/`unchecked`) |
| `extraction` | where the map came from: `files` (a file is `touched` iff `citations > 0`), `passes` (≥1 `mechanical`), `entities` **one per node** with ≥1 origin `(path, line, rule, family)` — origins name analysed files and declared passes |
| `ci` | the gates: `workflows` (≥1), `jobs` (≥1; role `gate`/`post-merge`/`manual`/`disabled`/`local`), `edges` between jobs, triggers and the merge, `merge` with ≥1 job as input, `ratchets`, `static_checks`, `summary` |
| `inventory` | `files`, `lines`, `declarations` |
| `evidence_metadata` | `class`, `rules`, `limitations` |

`stores`, `externals`, `layers`, `edges` and `behavior` are optional. Top-level `title`,
`description`, `source_tree_sha256` (64 hex) and semver `schema_version` are required.

**Versioning.** `schema_version`'s major is the contract major: this gateway speaks
`1.x` and refuses any other major. A minor adds optional fields or
sections; unknown fields are ignored everywhere. Unknown enum values are rejected only
where renderers branch on them (edge `class`, invariant `status`, pass `class`, job
`role`, extraction `authority`). Identifiers must be unique and every reference must
resolve (edges → nodes, entities ↔ nodes, origins → files and passes, `needs`/merge
inputs/ratchets/static checks → jobs).

**Enforcement.** Validation runs on every read, local or GitHub, before a snapshot is
stored, so a stored revision is always a conforming one. A non-conforming document is
refused with **4033** whose message names the contract, the first five problems and how
many more (`model at … does not conform to hermes.architecture v1.0: …; … (+N more)`);
Portal shows it verbatim. `architecture.describe` and `architecture.history` entries carry
`contract: {name, version, major, minor, required_sections, optional_sections, schema_digest}`;
the node annotation (`architecture.list` → `status`) carries `contract: {name, version}` and
`conforming: bool` (local: the checkout's model is read and validated; GitHub: conforming once
a snapshot exists, otherwise `problem` says the model has not been read yet).
`gateway.capabilities` advertises `architecture: {methods, contract}` beside the flat
`capability_names` list.

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
are advertised by `gateway.capabilities` in `capability_names`, and
`architecture: {methods, contract}` names the contract (see above) so a client can decode a
served document knowing its major before the first call.

## Security

Manifests are operator-owned state under `HERMES_HOME`. Model paths are resolved inside
the declared root and rejected when they escape it; models are capped at 16 MiB and must
be JSON objects with a `schema_version` that conform to the contract. Check commands run only for local services, in
the declared root, with output truncated to 4 000 characters. GitHub fetches go to
`raw.githubusercontent.com` over HTTPS and send `GITHUB_TOKEN`/`GH_TOKEN` when set.

Implementation: `tui_gateway/architecture_contract.py` (the contract), `tui_gateway/architecture_store.py`
(manifests, bindings, validation, snapshots, checks), `tools/architecture_services.py` (definitions),
`tools/service_graph.py` (runtime providers + composition), `tui_gateway/methods_architecture.py`
(handlers); tests in `tests/gateway/test_architecture_contract.py`, `tests/gateway/test_architecture_store.py`,
`tests/gateway/test_methods_architecture.py`, `tests/cron/test_architecture_services.py`,
`tests/cron/test_architecture_binding.py` (fixture: `tests/gateway/architecture_fixtures.py`).
