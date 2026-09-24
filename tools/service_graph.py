"""The services the dataflow graph draws: runtime providers, then architecture.

Four liveness providers put running services on the graph — tracked background
processes, Docker containers, Nomad jobs, launchd services. Architecture
manifests (``tui_gateway/architecture_store.py``) add the SDLC side: a model, a
check, a history. The two meet here, after every collector has run and before
``build_cron_graph`` turns declarations into nodes:

* a manifest **bound** to a runtime service (``"runtime": {"provider", "id"}``)
  annotates that provider's node — one node per logical service, with the
  provider's liveness and topology and the manifest's ``architecture`` and
  source files — and adds no node of its own;
* a manifest with no binding is a **standalone** codebase and keeps its own
  ``arch:<id>`` node, as before.

Composition is exact (canonical graph ids, never display names), deterministic,
and fail-open: a binding whose runtime is absent, or two manifests binding one
runtime, is logged and skipped rather than fabricating or overwriting a node.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def _runtime_collectors() -> Dict[str, Callable[[], List[Dict[str, Any]]]]:
    from tools.docker_services import collect_docker_services
    from tools.launchd_services import collect_launchd_services
    from tools.nomad_services import collect_nomad_services
    from tools.process_registry import process_registry

    return {
        "process": process_registry.collect_service_declarations,
        "docker": collect_docker_services,
        "nomad": collect_nomad_services,
        "launchd": collect_launchd_services,
    }


def collect_runtime_services(
    collectors: Optional[Dict[str, Callable[[], List[Dict[str, Any]]]]] = None,
) -> List[Dict[str, Any]]:
    """Every live service the providers report. Providers run concurrently and
    fail open: one provider's hiccup must never sink the graph. Results are
    ordered by provider name so the graph is stable across calls."""
    collectors = collectors if collectors is not None else _runtime_collectors()
    results: Dict[str, List[Dict[str, Any]]] = {}
    if not collectors:
        return []
    with ThreadPoolExecutor(max_workers=len(collectors)) as executor:
        pending = {executor.submit(fn): name for name, fn in collectors.items()}
        for future in as_completed(pending):
            provider = pending[future]
            try:
                results[provider] = list(future.result() or [])
            except Exception:
                logger.exception("service overlay unavailable: %s", provider)
                results[provider] = []
    services: List[Dict[str, Any]] = []
    for provider in sorted(results):
        services.extend(results[provider])
    return services


def _merged_source_files(*lists: Iterable[str]) -> List[str]:
    """Deterministic union of source-file paths, deduplicated by normalized path."""
    import os

    seen: Dict[str, str] = {}
    for values in lists:
        for value in values or []:
            if not isinstance(value, str) or not value.strip():
                continue
            key = os.path.normpath(value.strip())
            seen.setdefault(key, value.strip())
    return [seen[key] for key in sorted(seen)]


def attach_architecture(
    runtime_services: List[Dict[str, Any]],
    definitions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Compose runtime services with architecture definitions.

    ``definitions`` come from :func:`tools.architecture_services.collect_architecture_definitions`:
    each has ``service`` (the standalone declaration), ``source_files``,
    ``architecture`` (the node annotation) and, when bound, ``runtime`` with the
    canonical ``graph_id``. Runtime declarations are returned first, in their
    given order, with bound annotations merged in; standalone definitions
    follow, sorted by id. Runtime-owned facts (health, inputs, outputs,
    side_effects, relationships, description) stay authoritative; the manifest
    contributes only ``architecture`` and its source files.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []
    for service in runtime_services:
        sid = service.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        copy = dict(service)
        by_id[sid] = copy
        ordered.append(copy)

    bound: Dict[str, List[Dict[str, Any]]] = {}
    standalone: List[Dict[str, Any]] = []
    for definition in definitions:
        runtime = definition.get("runtime")
        if isinstance(runtime, dict) and runtime.get("graph_id"):
            bound.setdefault(str(runtime["graph_id"]), []).append(definition)
        else:
            standalone.append(definition)

    for target in sorted(bound):
        candidates = bound[target]
        if len(candidates) > 1:
            names = ", ".join(sorted(str(d.get("manifest_id")) for d in candidates))
            logger.warning(
                "architecture: %d manifests bind runtime service %s (%s); none applied — one manifest per runtime service",
                len(candidates), target, names,
            )
            continue
        definition = candidates[0]
        service = by_id.get(target)
        if service is None:
            logger.warning(
                "architecture manifest %s binds runtime service %s, which no provider reports right now; "
                "the model stays reachable via architecture.describe but nothing is drawn for it",
                definition.get("manifest_id"), target,
            )
            continue
        service["architecture"] = dict(definition.get("architecture") or {})
        service["source_files"] = _merged_source_files(service.get("source_files") or [], definition.get("source_files") or [])

    for definition in sorted(standalone, key=lambda d: str(d.get("service", {}).get("id", ""))):
        service = definition.get("service")
        if isinstance(service, dict) and service.get("id"):
            ordered.append(dict(service))
    return ordered


def collect_graph_services() -> List[Dict[str, Any]]:
    """What ``cron.graph`` / ``code.graph`` overlay on the dataflow graph."""
    from tools.architecture_services import collect_architecture_definitions

    runtime = collect_runtime_services()
    try:
        definitions = collect_architecture_definitions()
    except Exception:
        logger.exception("service overlay unavailable: architecture")
        definitions = []
    return attach_architecture(runtime, definitions)
