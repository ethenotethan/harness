"""Living artifacts as the dataflow graph sees them.

``cron.graph`` draws what a job *declares* it reads and writes. A living
artifact (``tui_gateway/artifact_store.py``) is declared the other way round:
its JSON content names the crons that tend it in a top-level ``maintainers``
list (``["cron:<jobId>", …]`` — the convention ``artifact_actions`` consumes)
and every record is stamped with who last wrote it (``updated_by`` =
``cron:<jobId>`` / ``session:<id>`` / ``agent``). Neither fact reaches the
graph unless someone hands it to ``build_cron_graph`` — so a cron that tends a
map showed no edge to the map. This module is that hand-off.

One declaration per artifact, in the shape ``build_cron_graph(artifacts=…)``
reads::

    {"id": "artifact:<artifact_id>", "label": <title or id>,
     "artifact_id", "artifact_kind", "rev", "updated_at", "updated_by",
     "maintainers": ["cron:<jobId>", …], "queries": [<query id>, …]}

Content is parsed once per artifact from a single index read; revisions are
never loaded (the graph is re-read every few seconds for liveness and a
revision walk per artifact per poll would be the wrong price). Fail-open: a
store that cannot be read yields ``[]`` with a warning — the graph is still
drawn, only without artifacts. Sorted by id so two collections over the same
store are byte-identical.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _maintainers(content: Any) -> List[str]:
    """``maintainers`` from a JSON document's top level; ``[]`` for anything
    else (markdown/html kinds, a JSON list, malformed text)."""
    if not isinstance(content, str):
        return []
    text = content.lstrip()
    if not text.startswith("{"):
        return []
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("maintainers")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str):
            item = item.strip()
            if item and item not in out:
                out.append(item)
    return out


def _query_ids(record: Dict[str, Any]) -> List[str]:
    """The ids of the query declarations a record exposes (see
    ``artifact_queries.validate_declarations``), in declaration order."""
    queries = record.get("queries")
    if not isinstance(queries, list):
        return []
    ids: List[str] = []
    for declaration in queries:
        if not isinstance(declaration, dict):
            continue
        qid = str(declaration.get("id") or "").strip()
        if qid and qid not in ids:
            ids.append(qid)
    return ids


def artifact_declaration(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One stored artifact (WITH content) → its graph declaration, or ``None``
    for a record without an id."""
    artifact_id = str(record.get("id") or "").strip()
    if not artifact_id:
        return None
    title = record.get("title")
    declaration: Dict[str, Any] = {
        "id": f"artifact:{artifact_id}",
        "label": (title.strip() if isinstance(title, str) else "") or artifact_id,
        "artifact_id": artifact_id,
        "artifact_kind": str(record.get("kind") or ""),
        "rev": record.get("rev"),
        "updated_at": record.get("updated_at"),
        "updated_by": record.get("updated_by") or "",
        "maintainers": _maintainers(record.get("content")),
    }
    queries = _query_ids(record)
    if queries:
        declaration["queries"] = queries
    return declaration


def collect_graph_artifacts() -> List[Dict[str, Any]]:
    """What ``cron.graph`` overlays for living artifacts. Never raises."""
    try:
        from tui_gateway import artifact_store

        records = artifact_store.list_artifacts(include_content=True)
    except Exception:
        logger.warning("artifact overlay unavailable: store unreadable", exc_info=True)
        return []

    declarations: List[Dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        try:
            declaration = artifact_declaration(record)
        except Exception:
            logger.warning(
                "artifact overlay: skipping unreadable record %r",
                record.get("id"), exc_info=True,
            )
            continue
        if declaration is not None:
            declarations.append(declaration)
    declarations.sort(key=lambda d: d["id"])
    return declarations
