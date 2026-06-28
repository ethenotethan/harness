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
      "source":         "raw/articles/llama-cpp-release.md", // raw source (empty string if none)
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

| trigger | meaning |
|---------|---------|
| `ingest` | from a source ingest (article, paper, URL) |
| `query` | filed from a query answer |
| `lint` | auto-fix during linting |
| `process-inbox` | from human inbox thoughts |
| `manual` | direct agent action |

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
    let gitCommit: String
    let afterSha256: String

    struct DiffStats: Codable {
        let linesAdded: Int
        let linesRemoved: Int
    }

    enum CodingKeys: String, CodingKey {
        case id, timestamp, action, page, title, type, summary, trigger, source
        case diffStats = "diff_stats"
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