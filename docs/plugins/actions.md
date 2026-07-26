# Artifact action plugins

Tier-1 handlers for artifact intent buttons: deterministic code the gateway
runs when a user clicks a declared button in a living artifact. If the code
can be written in advance, it belongs here — not in an agent loop.

## Where plugins live

```
~/.hermes/plugins/actions/*.py
```

Each `.py` file executes at gateway startup and on every reload. Inside the
file, `register_handler(name, fn)` is pre-bound in the execution namespace —
no import needed:

```python
def _my_handler(artifact_id, binding_id, entity_ref):
    return {"status": "succeeded", "message": "done"}

register_handler("my.custom.action", _my_handler)
```

A handler receives keyword arguments `artifact_id`, `binding_id`,
`entity_ref` and returns a dict with `status` in
`succeeded | failed` (plus optional `message` / `reason`).

## Reloading — no gateway restart needed

Reload is **explicit, never file-watched**. Three equivalent triggers:

| Surface | How |
|---------|-----|
| RPC | `actions.reload` (native app, scripts) |
| Chat | ask the agent to reload actions (it wraps the RPC) |
| Restart | plugins also load at gateway startup |

The reload is a **staged swap**: every plugin file executes against a staging
registry first. If any file fails to parse or execute, the whole swap aborts,
the previous handlers stay live, and the traceback comes back to the caller.
You cannot brick the running registry with a syntax error.

Every reload logs a registry diff (added / changed / removed handler names),
which pairs with the invocation ledger (`~/.hermes/artifacts/invocations.jsonl`)
to answer "what code ran when I clicked that button."

## The security model — why you author files but agents trigger reloads

- The plugins directory MUST NOT be writable by agent tools. The loader
  resolves the real path and **hard-fails** if it sits inside any agent
  workspace root.
- Given that, the reload *trigger* is safe to expose publicly. Triggering
  activation is harmless when only a human can author what activates.
- No file-watching: silent auto-reload would turn the agent's ordinary
  file-write tools into a code-injection path if the directory check were
  ever misconfigured. The convenience delta is seconds; the risk delta is
  total.

Sessions author *declarations* (data — which buttons exist, what they bind
to). Only filesystem-authored plugins and core code register *handlers*
(executable behavior). An artifact can never smuggle code.

## The entity-ref rule (MANDATORY for every handler)

`entity_ref` arrives from the client and is **untrusted**. Treat it as a
lookup key into the pinned artifact content; extract external identifiers
(Linear issue IDs, URLs, primary keys) from the **stored entity fields**,
never from the raw string. If the lookup fails, return `failed` — never
proceed with the raw ref.

```python
# WRONG — client controls the target:
linear_client.delete(entity_ref)

# RIGHT — target comes from artifact content the agent already wrote:
row = lookup_row(artifact_content, entity_ref)
if row is None:
    return {"status": "failed", "reason": f"unknown entity {entity_ref!r}"}
linear_client.delete(row["linear_id"])
```

This bounds the blast radius to what the artifact already declares: a forged
`entity_ref` that isn't in the artifact simply fails.

## Destructive handlers

Declare the *binding* with `"presentation": {"role": "destructive"}` in the
artifact's `actions` array. The gateway then requires the V1 challenge flow —
the user confirms a native dialog that leads with the server-resolved intent
name before the handler ever runs. The handler code itself needs nothing
special; confirmation is enforced by the invocation engine, and an artifact
cannot opt out of it.

## Reference plugin: linear.issue.delete

Deletes a Linear issue via the GraphQL API. Demonstrates the entity-ref rule,
credential handling (env var on the gateway host — never in the artifact),
and error surfacing.

```python
# ~/.hermes/plugins/actions/linear_delete.py
import json
import os
import urllib.request

LINEAR_GRAPHQL = "https://api.linear.app/graphql"


def _lookup_row(content_json: str, entity_ref: str):
    """Resolve entity_ref against the pinned dataset content (rule above)."""
    try:
        content = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return None
    key_field = content.get("key", "id")
    target = entity_ref.strip().lower()
    for row in content.get("rows", []):
        if str(row.get(key_field, "")).strip().lower() == target:
            return row
    return None


def _delete_linear_issue(artifact_id, binding_id, entity_ref):
    from tui_gateway import artifact_store

    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        return {"status": "failed", "reason": "artifact not found"}

    row = _lookup_row(artifact.get("content", ""), entity_ref)
    if row is None:
        return {"status": "failed",
                "reason": f"entity {entity_ref!r} not found in artifact"}

    # External ID from the STORED row — never from the client string.
    linear_id = str(row.get("linear_id", "")).strip()
    if not linear_id:
        return {"status": "failed",
                "reason": f"row {entity_ref!r} has no linear_id field"}

    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        return {"status": "failed",
                "reason": "LINEAR_API_KEY not set on the gateway host"}

    body = json.dumps({
        "query": "mutation($id: String!) { issueDelete(id: $id) { success } }",
        "variables": {"id": linear_id},
    }).encode()
    request = urllib.request.Request(
        LINEAR_GRAPHQL, data=body,
        headers={"Authorization": api_key,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the engine
        return {"status": "failed", "reason": f"Linear API error: {exc}"}

    errors = payload.get("errors")
    if errors:
        return {"status": "failed", "reason": str(errors[0].get("message", errors[0]))}
    ok = payload.get("data", {}).get("issueDelete", {}).get("success", False)
    if not ok:
        return {"status": "failed", "reason": "Linear rejected the delete"}
    return {"status": "succeeded", "message": f"Deleted Linear issue {entity_ref}"}


register_handler("linear.issue.delete", _delete_linear_issue)
```

Wire it to a button by declaring the binding in the artifact:

```json
{
  "id": "linear-issues",
  "actions": [
    {"type": "intent", "id": "delete-ticket", "label": "Delete",
     "intent": "linear.issue.delete",
     "presentation": {"role": "destructive"}}
  ]
}
```

Each dataset row needs a `linear_id` field holding the real Linear issue ID
(the UUID or `ENG-101`-style key the API accepts). For inline HTML artifacts,
the page marks the click target with inert attributes:

```html
<button data-hermes-binding="delete-ticket"
        data-hermes-entity="eng-101">Delete</button>
```

`data-hermes-entity` must match the row's key-field value; the gateway
resolves everything else.

## Iterating on a handler

1. Edit the file in `~/.hermes/plugins/actions/`.
2. Trigger `actions.reload` (chat: "reload my actions").
3. Click the button again. In-flight invocations finish on the old code;
   the swap affects the next invoke.

If the reload response reports an error, the traceback names the failing
file and the previous handlers are still live.

## Overriding built-ins

Registering the same intent name as a built-in (`artifact.refresh`,
`artifact.entity.tombstone`) deliberately replaces it. Files load
alphabetically; on a name conflict between plugin files, the last file wins.
