"""Architecture-manifest services for the cron interflow graph.

A service that conforms to the architecture standard (see
``tui_gateway/architecture_store.py``) is declared by a manifest under
``~/.hermes/services/architecture/<id>.json``. This module turns each manifest
into an **architecture definition**: the standalone service declaration the
graph would draw for a codebase nobody else reports (``arch:<id>``), its code
(``source_files``), the ``architecture`` annotation Portal opens the model from
and, when the manifest binds to a runtime service, the canonical graph id of
that service. ``tools/service_graph.py`` composes the definitions with the
process, Docker, Nomad and launchd providers: a bound definition annotates the
runtime node, a standalone one is a node of its own. There is nothing to probe
here — a manifest describes a codebase, not a running process.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _absolute_source_files(manifest: Dict[str, Any]) -> List[str]:
    """Manifest ``source_files`` are relative to the service root; the graph's
    resolver treats relative paths as ``~/.hermes/scripts`` entries, so make them
    absolute here. The model file itself is always listed so it is browsable."""
    root = manifest.get("root")
    if not root:
        return []
    base = Path(root)
    files = [str(base / manifest["model"])]
    for item in manifest.get("source_files") or []:
        path = Path(item).expanduser()
        files.append(str(path if path.is_absolute() else base / path))
    return files


def collect_architecture_definitions(home: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every valid manifest as a definition, sorted by manifest id. Fail-open per manifest."""
    from cron.jobs import normalize_service_declaration
    from tui_gateway import architecture_store as store

    definitions: List[Dict[str, Any]] = []
    for manifest in store.list_manifests(home):
        try:
            declaration = normalize_service_declaration(
                manifest["name"],
                manifest["description"],
                inputs=manifest.get("inputs"),
                outputs=manifest.get("outputs"),
                side_effects=manifest.get("side_effects"),
                relationships=manifest.get("relationships"),
                source_files=_absolute_source_files(manifest),
            )
        except ValueError as exc:
            logger.warning("architecture manifest %s rejected: %s", manifest["id"], exc)
            continue
        annotation = store.status_for(manifest, home)
        service: Dict[str, Any] = {
            "id": store.graph_id(manifest),
            "label": declaration["name"],
            "description": declaration["description"],
            "inputs": declaration["inputs"],
            "outputs": declaration["outputs"],
            "side_effects": declaration["side_effects"],
            "source_files": declaration["source_files"],
            "architecture": annotation,
        }
        if declaration.get("relationships"):
            service["relationships"] = declaration["relationships"]
        definitions.append({
            "manifest_id": manifest["id"],
            "graph_id": store.graph_id(manifest),
            "runtime": manifest.get("runtime"),
            "service": service,
            "source_files": declaration["source_files"],
            "architecture": annotation,
        })
    return definitions


def collect_architecture_services(home: Optional[str] = None) -> List[Dict[str, Any]]:
    """The standalone definitions as service declarations — codebases no runtime
    provider reports. Bound definitions are not services of their own; they
    annotate the runtime node through ``tools.service_graph.attach_architecture``."""
    return [
        definition["service"]
        for definition in collect_architecture_definitions(home)
        if not definition.get("runtime")
    ]
