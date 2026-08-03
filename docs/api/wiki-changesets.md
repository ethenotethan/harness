# wiki.changesets — API Spec for HermesNative Timeline

## Endpoint

```
JSON-RPC method: wiki.changesets
Gateway: tui_gateway/server.py (already wired, no server-side changes needed)
```

## Request

```jsonc
{
  "method": "wiki.changesets",
  "params": {
    // ALL optional — omit for full timeline
    "wiki":    "main",                          // wiki name from wikis.yaml (omit for default)
    "page":    "entities/llama-cpp.md",         // filter to one page
    "action":  "update",                        // "create" | "update" | "archive" | "delete"
    "trigger": "ingest",                        // "ingest" | "query" | "lint" | "process-inbox" | "manual"
    "limit":   50,                              // default 50, max 200
    "offset":  0,                               // pagination offset
    "since":   "2026-06-01T00:00:00Z",         // ISO 8601, only after this
    "until":   "2026-06-28T00:00:00Z"          // ISO 8601, only before this
  }
}
```

All params are optional. Omit everything for the full timeline (newest first, 50 per page).

## Response

```jsonc
{
  "changesets": [
    {
      "id":             "2026-06-28T140819-001",           // unique, sortable
      "timestamp":      "2026-06-28T14:08:19Z",           // ISO 8601 UTC
      "action":         "update",                          // "create" | "update" | "archive" | "delete"
      "page":           "entities/hermes-agent.md",        // relative path in wiki
      "title":          "Hermes Agent",                    // page title from frontmatter
      "type":           "entity",                          // page type from frontmatter
      "summary":        "Added speculative decoding benchmarks and updated to b4820",
      "diff_stats": {
        "lines_added":   45,
        "lines_removed": 12
      },
      "trigger":        "ingest",                         // what caused the change
      "source":         "raw/articles/llama-cpp-release.md", // LEGACY single source; prefer source_event_keys
      "source_event_keys": [                              // provenance: the events that caused this
        "raw/articles/llama-cpp-release.md",
        "raw/papers/spec-decoding.md"
      ],
      "git_commit":     "218f565a",                       // short git hash (empty if no git)
      "after_sha256":   "a7185eefbeca4d2f..."             // page content hash after change
    }
    // ... more changesets
  ],
  "total":  7,            // total matching (for pagination)
  "limit":  50,
  "offset": 0
}
```

## Page type values

The `type` field is the `type:` frontmatter value from each page:

| type | directory | description |
|------|-----------|-------------|
| `entity` | entities/ | person, org, model, product |
| `concept` | concepts/ | idea, technique, topic |
| `comparison` | comparisons/ | side-by-side analysis |
| `query` | queries/ | filed question/answer |
| `project` | projects/ | personal project |
| `goal` | goals/ | life/project goal |
| `life` | life/ | life tracking entry |
| `issue` | issues/ | problem/blocker/task |

## Action values

| action | meaning |
|--------|---------|
| `create` | new page written |
| `update` | existing page modified |
| `archive` | page moved to _archive/ |
| `delete` | page removed |

## Trigger values

These five are conventional, not exhaustive. A trigger is a free string, and
what each one *is* is declared by a `type: event-type` wiki page — so adding an
ingestion source is a page commit, not a gateway or client release. Clients
resolve unrecognized values against those pages and fall back to a stable
derived presentation, so a new trigger is never a broken one.

| trigger | meaning |
|---------|---------|
| `ingest` | from a source ingest (article, paper, URL) |
| `query` | filed from a query answer |
| `lint` | auto-fix during linting |
| `process-inbox` | from human inbox thoughts |
| `manual` | direct agent action |

## Provenance — `source_event_keys`

`source_event_keys` is the edge from a change back to the events that caused
it: an ordered list of wiki-relative raw source paths. Raw sources are
immutable files carrying their own `source_url` / `ingested` / `sha256`, so the
path is a stable identity and provenance needs no new storage.

It is **always present**, so a reader never has to distinguish "field missing
because this changeset is old" from "field missing because nobody recorded it":

| value | meaning |
|-------|---------|
| `["raw/a.md", "raw/b.md"]` | recorded — these events caused the change |
| `[]` | **unknown**. Not a claim that nothing caused it |

Empty means *unrecorded*, deliberately not split into "no cause" vs "cause not
written down". Those two are indistinguishable on disk — the legacy `source`
defaults to `""` for both — and inferring which from the `trigger` would be
guesswork presented as fact. So the log refuses to guess.

That makes `unknown` trustworthy only if it stays rare going forward, which is
why every write path now accepts provenance and the count of unknowns only
shrinks: `unknown` comes to mean precisely "predates provenance".

Changesets written before this field existed are migrated **on read** — the
legacy `source` becomes the first key. An existing KB needs a newer gateway,
not a migration script.

## Error response

```jsonc
{
  "error": {
    "code":    5055,
    "message": "error description"
  }
}
```

## Swift model (suggested)

```swift
struct WikiChangeset: Codable, Identifiable {
    let id: String
    let timestamp: String
    let action: String          // "create" | "update" | "archive" | "delete"
    let page: String            // "entities/llama-cpp.md"
    let title: String
    let type: String            // "entity" | "concept" | ...
    let summary: String
    let diffStats: DiffStats
    let trigger: String
    let source: String
    /// Absent on pre-provenance payloads, so decode it optionally and treat
    /// nil and [] identically — both are "unknown".
    let sourceEventKeys: [String]?
    let gitCommit: String
    let afterSha256: String

    struct DiffStats: Codable {
        let linesAdded: Int
        let linesRemoved: Int
    }

    enum CodingKeys: String, CodingKey {
        case id, timestamp, action, page, title, type, summary, trigger, source
        case diffStats = "diff_stats"
        case sourceEventKeys = "source_event_keys"
        case gitCommit = "git_commit"
        case afterSha256 = "after_sha256"
    }
}

struct WikiChangesetsResponse: Codable {
    let changesets: [WikiChangeset]
    let total: Int
    let limit: Int
    let offset: Int
}
```

## Usage notes

1. **Pagination**: use `limit` + `offset`. `total` tells you how many more exist.
2. **Filter by page**: pass `"page": "entities/llama-cpp.md"` to see the edit history of one page.
3. **Date range**: `since`/`until` for week/month views. ISO 8601 with Z suffix.
4. **Empty git_commit**: means the wiki isn't git-initialized or git wasn't available when the changeset was captured. Don't crash, just hide the commit link.
5. **Timeline order**: newest first by default (index.json is prepended). Already correct, no client-side sorting needed.

## Example: "last 20 changes across the whole wiki"

```jsonc
{
  "method": "wiki.changesets",
  "params": { "limit": 20 }
}
```

## Example: "all changes to llama.cpp since June 1"

```jsonc
{
  "method": "wiki.changesets",
  "params": {
    "page": "entities/llama-cpp.md",
    "since": "2026-06-01T00:00:00Z",
    "limit": 50
  }
}
```

## Example: "creates only, this week"

```jsonc
{
  "method": "wiki.changesets",
  "params": {
    "action": "create",
    "since": "2026-06-22T00:00:00Z",
    "until": "2026-06-28T23:59:59Z"
  }
}
```
## wiki.changeset_diff — per-changeset unified diff

```jsonc
{
  "method": "wiki.changeset_diff",
  "params": {
    "id":   "2026-06-28T140819-001",   // required — from wiki.changesets
    "wiki": "main"                     // optional
  }
}
```

Response:

```jsonc
{
  "diff": "diff --git a/entities/x.md b/entities/x.md\n--- a/...\n+++ b/...\n@@ -4,3 +4,4 @@ ...\n+Line two added.\n",
  "changeset": { /* same shape as a wiki.changesets entry */ }
}
```

Errors: `4001` bad/missing id · `5057` diff unavailable (changeset unknown, or
the wiki wasn't git-initialized when it was captured — the message says which)
· `5056` unexpected failure. Diffs are truncated at 200KB.

## wiki.events — the ingestion event log

Every event that caused a wiki update, newest first. This is a **join over data
already on disk**, not new storage: files under `raw/` are the events, and the
changeset index records which events caused which page writes. So it is
accurate for history that predates it.

```jsonc
{
  "method": "wiki.events",
  "params": {
    // ALL optional
    "wiki":   "main",
    "kind":   "ingest",                  // filter by event kind (see below)
    "limit":  200,                       // default 200, max 1000
    "offset": 0,
    "since":  "2026-06-01T00:00:00Z",
    "until":  "2026-06-28T00:00:00Z"
  }
}
```

Response:

```jsonc
{
  "events": [
    {
      "key":        "raw/articles/llama-cpp-release.md",  // stable identity; the provenance join key
      "kind":       "ingest",                             // event_kind, else the page's type
      "title":      "llama.cpp b4820 release notes",
      "timestamp":  "2026-06-28T14:05:00Z",              // `ingested`; "" when never recorded
      "source_url": "https://github.com/ggml-org/llama.cpp/releases/tag/b4820",
      "sha256":     "a7185eefbeca4d2f...",
      "changesets": [                                     // what this event caused
        {
          "id":        "2026-06-28T140819-001",
          "page":      "entities/llama-cpp.md",
          "title":     "llama.cpp",
          "action":    "update",
          "timestamp": "2026-06-28T14:08:19Z"
        }
      ]
    }
  ],
  "total":  12,
  "limit":  200,
  "offset": 0
}
```

`kind` is **not** a fixed enum. It's the raw source's `event_kind` (falling back
to its `type`), matched against `type: event-type` wiki pages that declare what
each kind is and how to draw it. An undeclared kind is still a valid kind.

Notes:

- **An event that caused nothing still appears** with `"changesets": []`. An
  ingested source nobody synthesized from is exactly the gap worth seeing.
- **`timestamp` is never invented.** A source with no `ingested` reports `""`
  and sorts last, rather than being silently dated to now.
- A wiki with no `raw/` returns an empty log, not an error.

Errors: `5059` unexpected failure.

## wiki.update — provenance on write

`wiki.update` accepts three additional optional params so a write can declare
what caused it:

```jsonc
{
  "method": "wiki.update",
  "params": {
    "path": "entities/llama-cpp.md",
    "body": "...",
    "trigger": "ingest",                                   // default "manual"
    "source_events": ["raw/articles/llama-cpp-release.md"], // provenance
    "summary": "Added b4820 speculative-decoding benchmarks"
  }
}
```

`trigger` was previously hardcoded to `"manual"` on this path, which made every
write through it indistinguishable in the timeline regardless of what made it.
Omitting `source_events` records `[]` — *unknown* — which is the honest result
for a hand edit that genuinely had no ingestion event.
