# Read-only file browsing (`files.list` / `files.read`)

Lets a connected desktop click through Hermes-specific files — `scripts/`,
`indexing/`, the `~/.hermes` data home — and read source and markdown in-app.
Before this, the only file readers were scoped to a single skill
(`skills.get`) or a single wiki page (`wiki.page`); the HTTP `/v1/files` path
only serves files the agent explicitly staged. Neither can list a directory
or reach the repo tree.

## Roots

Exactly two roots are browsable, and nothing outside them is reachable:

| Root | Path | What's in it |
|------|------|--------------|
| `repo` | the running gateway's checkout (`Path(__file__).parents[1]`) | `indexing/`, `scripts/`, `agent/`, `tui_gateway/`, … |
| `hermes` | `HERMES_HOME` (default `~/.hermes`) | `memory/`, `skills/`, wiki data, … |

`hermes` is omitted if the data home does not resolve (fresh install); `repo`
is always present.

## Security

The whole contract is **containment**. Every path a client names is resolved
and verified to live under its declared root — `(root / rel).resolve()` then
`.relative_to(root)` — before a single byte is read, the same idiom
`skills.get` and `file_serve` use. This rejects both `../` traversal and
symlinks that point outside the root. There is **no write path**. Reads are
UTF-8 text only: binary files (NUL-byte sniff) and files over 1 MiB are
refused, so the endpoint can't be used to stream out arbitrary blobs.

## `files.list`

One directory level at a time — the client lazy-loads a folder's contents when
it expands, so any path is always reachable regardless of repo size.

**Params:** `root` (optional), `path` (optional, relative, default the root).
With no `root`, returns the available root names.

```jsonc
// files.list {} → the roots
{ "roots": ["hermes", "repo"] }

// files.list {"root": "repo", "path": "indexing"}
{
  "root": "repo",
  "root_path": "/Users/you/.hermes/hermes-agent",
  "path": "indexing",
  "entries": [
    { "name": "cache", "path": "indexing/cache", "type": "dir", "has_children": true },
    { "name": "x402_snapshot.py", "path": "indexing/x402_snapshot.py", "type": "file", "size": 8123 }
  ]
}
```

Entries sort directories first, then case-insensitively by name. Noise
directories (`.git`, `__pycache__`, `node_modules`, virtualenvs, tool caches,
build output) are pruned.

## `files.read`

**Params:** `root` (required), `path` (required, relative).

```jsonc
// files.read {"root": "repo", "path": "indexing/x402_snapshot.py"}
{
  "root": "repo",
  "path": "indexing/x402_snapshot.py",
  "content": "…",
  "size": 8123,
  "read_only": true,       // os.access(W_OK)
  "language": "py"         // extension hint for the client's highlighter
}
```

## Error codes

| Code | Meaning |
|------|---------|
| 4001 | `root` / `path` required |
| 4013 | file too large (> 1 MiB) |
| 4015 | binary / non-UTF-8 file, not viewable |
| 4020 | path escapes the root (traversal or symlink) |
| 4404 | unknown root, or file/directory not found |

Implementation: pure logic in `tui_gateway/files_browse.py` (tested directly
against a tmp tree in `tests/gateway/test_files_browse.py`); thin
`@method("files.list")` / `@method("files.read")` handlers in
`tui_gateway/methods_harness.py`.
