#!/usr/bin/env python3
"""
Artifact Tool — living models the agent reads and maintains across sessions.

A living artifact is a named model in the HermesNative render dialects
(map/chart/graph/stats/table/markdown): a client list, an apartment-hunt
map, a monthly-spend chart. The store is shared with the gateway RPCs
(tui_gateway.artifact_store), so chat turns, cron jobs, workflows, and the
HermesNative app all see the same state, and every mutation is revisioned.

Critical behavior this tool enables that fence-emission alone cannot:
READ-BEFORE-WRITE. A fresh session updating "clients" first `get`s the
current content, modifies it, and writes back — instead of hallucinating
the prior state and overwriting history.

Actions:
  list                       -> id/kind/title/updated summaries
  get    {id}                -> full artifact incl. content
  set    {id, kind, content, title?, replace?, category?} -> upsert (merge per kind)
  categorize {id, category}  -> file under a folder ("" = unfile); no revision
  delete {id}
  revisions {id}             -> audit trail (who/when/rev)
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

VALID_KINDS = {"map", "chart", "graph", "stats", "table", "markdown", "dataset", "sankey", "timeline", "model", "html"}


def artifact_tool(
    action: str,
    id: str = "",
    kind: str = "",
    content: str = "",
    title: str = "",
    replace: bool = False,
    actions: str = "",
    queries: str = "",
    session_id: str = "",
    category: Optional[str] = None,
    comment_id: str = "",
) -> str:
    """Execute an artifact action against the shared store.

    Returns a JSON STRING — the tool registry's result contract
    (_normalize_handler_result) accepts only str or the multimodal
    envelope; raw dicts are rejected as tool_result_contract errors,
    which broke every artifact call once the contract landed.
    """
    return json.dumps(
        _artifact_tool_impl(
            action, id=id, kind=kind, content=content,
            title=title, replace=replace, actions=actions,
            queries=queries, session_id=session_id, category=category,
            comment_id=comment_id,
        ),
        ensure_ascii=False,
        default=str,
    )


def _artifact_tool_impl(
    action: str,
    id: str = "",
    kind: str = "",
    content: str = "",
    title: str = "",
    replace: bool = False,
    actions: str = "",
    queries: str = "",
    session_id: str = "",
    category: Optional[str] = None,
    comment_id: str = "",
) -> dict:
    from tui_gateway import artifact_comments, artifact_store

    action = (action or "").strip().lower()
    try:
        if action == "list":
            return {"success": True, "artifacts": artifact_store.list_artifacts()}

        if action == "get":
            artifact = artifact_store.get_artifact(id)
            if artifact is None:
                return {"success": False, "error": f"artifact not found: {id!r}"}
            result = {"success": True, "artifact": artifact}
            # Surfaced on the read every write starts with, so the user's
            # notes are seen before the content they are about is replaced.
            open_comments = artifact_comments.open_comment_count(id)
            if open_comments:
                result["open_comments"] = open_comments
            return result

        if action == "comments":
            if artifact_store.get_artifact(id) is None:
                return {"success": False, "error": f"artifact not found: {id!r}"}
            return {
                "success": True,
                "comments": artifact_comments.list_comments(id, include_resolved=False),
            }

        if action == "resolve_comment":
            comment = artifact_comments.resolve_comment(
                id, comment_id,
                resolved_by=f"agent:{session_id}" if session_id else "agent",
            )
            if comment is None:
                return {"success": False, "error": f"comment not found: {comment_id!r}"}
            _emit_comment_changed(id, comment_id, "resolved")
            return {"success": True, "comment": comment}

        if action == "set":
            normalized_kind = (kind or "").strip().lower()
            if normalized_kind not in VALID_KINDS:
                return {
                    "success": False,
                    "error": f"kind must be one of {sorted(VALID_KINDS)}",
                }
            # Action declarations arrive as a JSON string (tool params are
            # scalars). None (omitted) carries the stored declarations
            # forward; a present-but-invalid string is an error, not a
            # silent drop — a dropped declaration would dead-button the
            # artifact with no signal to the model.
            parsed_actions = None
            if actions.strip():
                try:
                    parsed_actions = json.loads(actions)
                except ValueError:
                    return {
                        "success": False,
                        "error": "actions must be a JSON array of action declarations",
                    }
                if not isinstance(parsed_actions, list):
                    return {
                        "success": False,
                        "error": "actions must be a JSON array of action declarations",
                    }
            # Same contract for the read side: omitted carries forward, a
            # present-but-invalid string is an error.
            parsed_queries = None
            if queries.strip():
                try:
                    parsed_queries = json.loads(queries)
                except ValueError:
                    return {
                        "success": False,
                        "error": "queries must be a JSON array of query declarations",
                    }
                if not isinstance(parsed_queries, list):
                    return {
                        "success": False,
                        "error": "queries must be a JSON array of query declarations",
                    }
            stored = artifact_store.set_artifact(
                artifact_id=id,
                kind=normalized_kind,
                content=content,
                title=title or None,
                updated_by=f"agent:{session_id}" if session_id else "agent",
                replace=bool(replace),
                actions=parsed_actions,
                queries=parsed_queries,
                category=category,
            )
            _emit_changed(stored)
            summary = {k: v for k, v in stored.items() if k != "content"}
            return {"success": True, "artifact": summary}

        if action == "categorize":
            stored = artifact_store.set_category(id, category or "")
            if stored is None:
                return {"success": False, "error": f"artifact not found: {id!r}"}
            _emit_changed({**stored, "category": stored.get("category", "")})
            summary = {k: v for k, v in stored.items() if k != "content"}
            return {"success": True, "artifact": summary}

        if action == "delete":
            if not artifact_store.delete_artifact(id):
                return {"success": False, "error": f"artifact not found: {id!r}"}
            _emit_changed({"id": id, "deleted": True})
            return {"success": True, "deleted": id}

        if action == "revisions":
            if artifact_store.get_artifact(id) is None:
                return {"success": False, "error": f"artifact not found: {id!r}"}
            return {"success": True, "revisions": artifact_store.list_revisions(id)}

        return {"success": False, "error": f"unknown action {action!r}"}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tool results must not raise
        logger.exception("artifact tool failed")
        return {"success": False, "error": str(exc)}


def _emit_changed(payload: dict) -> None:
    """Best-effort artifact.changed emission — tool calls should update
    connected clients live, but a headless context (no gateway loop) must
    not fail the write."""
    try:
        from tui_gateway.server import _emit

        event = {
            key: payload[key]
            for key in ("id", "kind", "title", "rev", "updated_at", "updated_by", "deleted", "category")
            if key in payload
        }
        _emit("artifact.changed", "", event)
    except Exception:  # noqa: BLE001
        pass


def _emit_comment_changed(artifact_id: str, comment_id: str, change: str) -> None:
    """Best-effort, for the same reason as _emit_changed."""
    try:
        from tui_gateway.artifact_comments import open_comment_count
        from tui_gateway.server import _emit

        _emit("artifact.comment.changed", "", {
            "artifact_id": artifact_id, "comment_id": comment_id,
            "change": change, "open": open_comment_count(artifact_id),
        })
    except Exception:
        pass


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

ARTIFACT_SCHEMA = {
    "name": "artifact",
    "description": (
        "Read and maintain living artifacts the user views in their client. "
        "They are named, persistent models (kinds: map, chart, graph, stats, table, markdown, "
        "dataset, html — content is the same JSON/markdown/HTML you would put in "
        "a fenced block of that kind). Artifacts survive across sessions and are shared "
        "with scheduled jobs and workflows; every change is revisioned.\n\n"
        "ALWAYS `get` an artifact before updating it — modify the CURRENT "
        "content, never reconstruct it from memory (a wholesale rewrite from "
        "memory destroys data other writers added). `map` kind merges markers "
        "by label and `dataset` kind merges rows by the declared key field, so "
        "for those you may set only new/changed entries; every other kind "
        "replaces content wholesale, so write back the complete updated body. "
        "Use `list` to discover what exists. Artifacts may be filed under a "
        "`category` — the folder the user browses them by in their client. "
        "`list` shows each artifact's category; when creating an artifact that "
        "belongs with existing ones, reuse that exact category rather than "
        "coining a near-duplicate, and use `categorize` to file or move one "
        "without rewriting it (it adds no revision). `model` kind is the ensemble "
        "artifact: named entity sets ({\"entities\": {name: {\"key\", \"items\"}}}), "
        "relations ([{\"from\": \"set/key\", \"to\": \"set/key\", \"type\"}]), and "
        "stacked views (map/table/graph/chart/stats/markdown/kanban) the client "
        "renders over one store with linked selection. Views may interleave "
        "narrative and an interactive board: [{\"type\": \"markdown\", "
        "\"text\": \"## Current sprint\"}, {\"type\": \"kanban\", "
        "\"entities\": [\"workstreams\"], \"column\": \"status\", "
        "\"columns\": [\"Todo\", \"Doing\", \"Done\"]}]. Kanban moves "
        "write the lane into the configured entity field without an actions "
        "declaration. Entity sets merge by key and relations by (from,to,type), "
        "so set only new/changed items.\n\n"
        "`html` kind is a self-contained HTML document (content is the raw "
        "HTML, not JSON) the client renders in a web view — use it for layouts "
        "the structured kinds can't express (custom dashboards, styled "
        "reports). It has no per-kind merge, so always write the COMPLETE "
        "document; `get` first and edit the current content.\n\n"
        "USER TRIAGE: dataset/map artifacts may declare an `actions` array "
        "(choice/toggle/delete controls the user taps in their client); the "
        "user's marks land in entry fields — read them, they are signal "
        "(e.g. rows with \"status\": \"going\", markers with \"reached_out\": "
        "true). Entries with `_deleted: true` are tombstones the user removed: "
        "the merge preserves them even if you re-emit the entry — NEVER strip "
        "or set `_deleted` yourself unless the user explicitly asks.\n\n"
        "USER COMMENTS: the user can pin comments anywhere on a rendered "
        "artifact. `get` reports `open_comments` when there are any; "
        "`comments` returns them, each with its `body`, the `anchor` it was "
        "pinned to (a CSS `selector` and the `text` that element showed) and "
        "the `rev` it was written against. Read them before editing, treat "
        "each as the user's guidance about that spot, and call "
        "`resolve_comment` with its `comment_id` once the artifact reflects it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "set", "categorize", "delete", "revisions", "comments", "resolve_comment"],
            },
            "id": {
                "type": "string",
                "description": "Artifact id (1-128 chars [a-zA-Z0-9._-]), e.g. 'bkk-apartments', 'clients'",
            },
            "kind": {
                "type": "string",
                "enum": ["map", "chart", "graph", "stats", "table", "markdown", "dataset", "sankey", "timeline", "model", "html"],
                "description": "Render dialect of the content (required for set)",
            },
            "content": {
                "type": "string",
                "description": "The artifact body — same format as the fenced block of that kind",
            },
            "title": {"type": "string", "description": "Human display name"},
            "comment_id": {
                "type": "string",
                "description": "Id of the user comment to mark addressed (resolve_comment)",
            },
            "category": {
                "type": "string",
                "description": (
                    "Folder the artifact is filed under in the user's client, "
                    "e.g. 'Research', 'Travel' (max 64 chars). On `set`, omit "
                    "to keep the current category; on `categorize`, an empty "
                    "string unfiles it."
                ),
            },
            "replace": {
                "type": "boolean",
                "description": "Skip per-kind merge and overwrite outright (default false)",
            },
            "actions": {
                "type": "string",
                "description": (
                    "JSON array of action declarations for the artifact's "
                    "native controls, stored alongside content (NOT inside "
                    "it). Intent buttons: [{\"type\": \"intent\", \"id\": "
                    "\"delete-ticket\", \"label\": \"Delete\", \"intent\": "
                    "\"linear.issue.delete\", \"presentation\": {\"role\": "
                    "\"destructive\"}}]. In html-kind content, wire elements "
                    "to a declaration via data-hermes-binding=\"<id>\" and "
                    "data-hermes-entity=\"<row-key>\". Omit to carry the "
                    "stored declarations forward unchanged."
                ),
            },
            "queries": {
                "type": "string",
                "description": (
                    "JSON array of query declarations — the READ side of "
                    "actions, for html-kind dashboards that show live data "
                    "from a backend the gateway can reach. Each names a "
                    "registered query handler (see the artifact.query.handlers "
                    "RPC; built-in: artifact.rows) and may narrow or bind its "
                    "parameters: [{\"id\": \"open-orders\", \"query\": "
                    "\"postgres.orders.open\", \"bind\": {\"state\": "
                    "\"open\"}, \"params\": {\"limit\": {\"type\": "
                    "\"int\", \"max\": 200}}, \"live\": {\"mode\": "
                    "\"poll\", \"interval_s\": 30}, \"invalidated_by\": "
                    "[\"archive-order\"]}]. In the page, an element with "
                    "data-hermes-query=\"<id>\" (and optional "
                    "data-hermes-params='{...}') receives the JSON result in a "
                    "child <script type=\"application/json\" data-hermes-sink> "
                    "and a 'hermes-data' event; render it with the page's own "
                    "JS. Never put SQL or credentials in an artifact — the "
                    "handler owns those. Omit to carry stored declarations "
                    "forward."
                ),
            },
        },
        "required": ["action"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="artifact",
    toolset="artifact",
    schema=ARTIFACT_SCHEMA,
    handler=lambda args, **kw: artifact_tool(
        action=args.get("action", ""),
        id=args.get("id", ""),
        kind=args.get("kind", ""),
        content=args.get("content", ""),
        title=args.get("title", ""),
        replace=bool(args.get("replace", False)),
        # Both manifests used to be dropped here: the tool accepted `actions`
        # but the registry never forwarded it, so an agent's intent buttons
        # silently never landed.
        actions=str(args.get("actions", "") or ""),
        queries=str(args.get("queries", "") or ""),
        session_id=str(kw.get("session_id", "") or ""),
        # None (omitted) keeps the stored category; "" is a deliberate unfile.
        category=args.get("category") if isinstance(args.get("category"), str) else None,
        comment_id=str(args.get("comment_id", "") or ""),
    ),
    emoji="🗂️",
)
