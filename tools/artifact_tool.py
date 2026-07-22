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
  set    {id, kind, content, title?, replace?} -> upsert (merge per kind)
  delete {id}
  revisions {id}             -> audit trail (who/when/rev)
"""

import json
import logging

logger = logging.getLogger(__name__)

VALID_KINDS = {"map", "chart", "graph", "stats", "table", "markdown", "dataset", "sankey", "timeline"}


def artifact_tool(
    action: str,
    id: str = "",
    kind: str = "",
    content: str = "",
    title: str = "",
    replace: bool = False,
    session_id: str = "",
) -> dict:
    """Execute an artifact action against the shared store."""
    from tui_gateway import artifact_store

    action = (action or "").strip().lower()
    try:
        if action == "list":
            return {"success": True, "artifacts": artifact_store.list_artifacts()}

        if action == "get":
            artifact = artifact_store.get_artifact(id)
            if artifact is None:
                return {"success": False, "error": f"artifact not found: {id!r}"}
            return {"success": True, "artifact": artifact}

        if action == "set":
            normalized_kind = (kind or "").strip().lower()
            if normalized_kind not in VALID_KINDS:
                return {
                    "success": False,
                    "error": f"kind must be one of {sorted(VALID_KINDS)}",
                }
            stored = artifact_store.set_artifact(
                artifact_id=id,
                kind=normalized_kind,
                content=content,
                title=title or None,
                updated_by=f"agent:{session_id}" if session_id else "agent",
                replace=bool(replace),
            )
            _emit_changed(stored)
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
            for key in ("id", "kind", "title", "rev", "updated_at", "updated_by", "deleted")
            if key in payload
        }
        _emit("artifact.changed", "", event)
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

ARTIFACT_SCHEMA = {
    "name": "artifact",
    "description": (
        "Read and maintain LIVING ARTIFACTS: named, persistent models the user "
        "views in their client (kinds: map, chart, graph, stats, table, markdown, "
        "dataset — content is the same JSON/markdown you would put in a fenced "
        "block of that kind). Artifacts survive across sessions and are shared "
        "with scheduled jobs and workflows; every change is revisioned.\n\n"
        "ALWAYS `get` an artifact before updating it — modify the CURRENT "
        "content, never reconstruct it from memory (a wholesale rewrite from "
        "memory destroys data other writers added). `map` kind merges markers "
        "by label and `dataset` kind merges rows by the declared key field, so "
        "for those you may set only new/changed entries; every other kind "
        "replaces content wholesale, so write back the complete updated body. "
        "Use `list` to discover what exists.\n\n"
        "USER TRIAGE: dataset/map artifacts may declare an `actions` array "
        "(choice/toggle/delete controls the user taps in their client); the "
        "user's marks land in entry fields — read them, they are signal "
        "(e.g. rows with \"status\": \"going\", markers with \"reached_out\": "
        "true). Entries with `_deleted: true` are tombstones the user removed: "
        "the merge preserves them even if you re-emit the entry — NEVER strip "
        "or set `_deleted` yourself unless the user explicitly asks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "set", "delete", "revisions"],
            },
            "id": {
                "type": "string",
                "description": "Artifact id (1-128 chars [a-zA-Z0-9._-]), e.g. 'bkk-apartments', 'clients'",
            },
            "kind": {
                "type": "string",
                "enum": ["map", "chart", "graph", "stats", "table", "markdown", "dataset", "sankey", "timeline"],
                "description": "Render dialect of the content (required for set)",
            },
            "content": {
                "type": "string",
                "description": "The artifact body — same format as the fenced block of that kind",
            },
            "title": {"type": "string", "description": "Human display name"},
            "replace": {
                "type": "boolean",
                "description": "Skip per-kind merge and overwrite outright (default false)",
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
        session_id=str(kw.get("session_id", "") or ""),
    ),
    emoji="🗂️",
)
