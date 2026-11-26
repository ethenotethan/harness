# `artifact.comment.*` — comments pinned to an artifact

A reader pins a comment to a spot on a rendered artifact; the agent reads the
open ones before it edits and resolves the ones it has addressed. A comment is
stored beside the artifact, not inside its content, so replacing an `html`
document does not erase it, and adding one never appends a revision or moves
`rev`.

## Comment

```jsonc
{
  "id": "c-4f1c0a9be2d7",
  "artifact_id": "q3-report",
  "rev": 7,                       // the revision the reader was looking at
  "anchor": {                     // {} = a comment on the artifact as a whole
    "selector": "main > section:nth-of-type(2) > table",
    "x": 0.42, "y": 0.1,          // fractions of that element's box
    "text": "Revenue by region"   // what the element showed
  },
  "author": "app:1A2B3C4D",
  "body": "These totals look a quarter out of date.",
  "created_at": "2026-11-26T09:14:03+00:00",
  "resolved": false               // resolved_at / resolved_by are set once true
}
```

`rev` lets a client tell a reader that the page has changed since the pin was
placed. `anchor.text` is what a client uses to find the element again, or to
show the comment as detached, when the selector no longer matches.

## Methods

| Method | Params | Returns |
|---|---|---|
| `artifact.comment.add` | `artifact_id`, `body`, `anchor?`, `author?`, `artifact_rev?` | `{comment}` |
| `artifact.comment.list` | `artifact_id`, `include_resolved?` (default `true`) | `{comments}`, oldest first |
| `artifact.comment.resolve` | `artifact_id`, `comment_id`, `resolved?` (default `true`), `resolved_by?` | `{comment}` |
| `artifact.comment.delete` | `artifact_id`, `comment_id` | `{deleted}` |

Limits: 200 comments per artifact, a 4,000-byte body, a 512-byte selector and
280 characters of anchor text. Bodies may contain newlines and tabs; no other
control characters are accepted anywhere.

Errors: `4001` invalid input, `4004` unknown artifact or comment, `5224`–`5227`
an unexpected failure in `add`, `list`, `resolve`, `delete`.

## Event

```jsonc
// artifact.comment.changed
{ "artifact_id": "q3-report", "comment_id": "c-4f1c0a9be2d7",
  "change": "added",            // added | resolved | reopened | deleted
  "open": 3 }                   // open comments left on the artifact
```

Metadata only; clients refetch with `artifact.comment.list`. Deleting an
artifact deletes its comments and emits only `artifact.changed`.

## Capabilities

`gateway.capabilities` lists `artifact.comment` and one name per method.

## The agent's side

The `artifact` tool reads the same store: `get` adds `open_comments` when
there are any, `comments` returns the open ones, and `resolve_comment` marks
one addressed, attributed to `agent:<session_id>`. The agent is not notified
of a new comment; it sees them the next time it reads the artifact.
