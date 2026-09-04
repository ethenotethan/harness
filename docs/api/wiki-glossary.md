# Per-wiki glossary API

Each wiki configured in `$HERMES_HOME/wikis.yaml` may define a proper-noun
glossary at `<wiki-root>/glossary.yaml`. The glossary is optional. It is owned
by the wiki rather than by an installed skill, so gateway clients and other
Harness code use the same validated data.

## `glossary.yaml` schema

```yaml
version: 1
mode: canonicalize # canonicalize | strict
proper_nouns:
  - canonical: Nous Research
    aliases:
      - Nous
    description: AI research organization
  - canonical: Hermes Agent
```

The root fields are required:

- `version` must be the integer `1`.
- `mode` must be `canonicalize` or `strict`.
- `proper_nouns` must be an array. Each entry requires a non-empty `canonical`
  string and may contain an `aliases` array of non-empty strings and a
  non-empty `description` string.

Canonical spellings and aliases share one case-insensitive namespace. A
spelling cannot duplicate another canonical spelling or alias, including an
alias in its own entry. Unknown fields are rejected. If the file is absent,
the glossary is disabled; if it is present but unreadable, malformed, or
invalid, loading fails closed.

Resource bounds are enforced at validation time: at most 2,000 entries, 50
aliases per entry, 256 characters per spelling, and 2,000 characters per
description.

Canonicalization maps a canonical spelling or alias to its `canonical` value.
An unknown spelling passes through in `canonicalize` mode and is rejected in
`strict` mode.

Harness consumers may call `canonicalize_text` to normalize configured forms
inside generated text and `canonicalize_inventory` to normalize and deduplicate
a model-declared proper-noun inventory. In strict mode, the inventory helper
raises on any unlisted name. Producers must invoke these helpers at their
post-generation boundary; publishing a policy does not implicitly rewrite
unrelated `wiki.update` calls.

## Wiki selection and authority

Both RPCs accept `wiki`, which must be a name in
`$HERMES_HOME/wikis.yaml`. If `wiki` is omitted, that registry must contain an
explicit `default` whose name exists in `wikis`. Raw paths, unknown names,
empty names, invalid configured paths, and environment/home-directory
fallbacks are rejected.

## `wiki.glossary`

Read one glossary:

```jsonc
{
  "method": "wiki.glossary",
  "params": { "wiki": "main" }
}
```

Result when absent:

```json
{
  "enabled": false,
  "version": 1,
  "mode": "canonicalize",
  "proper_nouns": [],
  "revision": ""
}
```

A present valid file returns the same shape with `enabled: true`, normalized
entries, and `revision` set to the SHA-256 hex digest of the stored bytes.
Invalid selection or an invalid present glossary returns JSON-RPC error `4001`.
Unexpected I/O/runtime failures return `5062`.

## `wiki.glossary.update`

Atomically replace one glossary:

```jsonc
{
  "method": "wiki.glossary.update",
  "params": {
    "wiki": "main",
    "version": 1,
    "mode": "strict",
    "proper_nouns": [
      { "canonical": "OpenAI", "aliases": ["Open AI"] }
    ],
    "if_match": "sha256-from-the-last-read"
  }
}
```

`version`, `mode`, and `proper_nouns` are required. `if_match` is optional; when
present it must equal the current revision. Use the empty string to require
that no glossary currently exists. A stale revision returns error `409` and
does not write. Invalid params or schema return `4001`; unexpected failures
return `5063`. A successful update returns the normalized read shape, including
the new revision.

Writes use a same-directory temporary file, `fsync`, and atomic replacement.
Clients should retain the returned revision and provide it on their next edit.

Both method names are advertised by `gateway.capabilities`.
