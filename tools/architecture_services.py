"""Architecture-manifest services for the cron interflow graph.

A service that conforms to the architecture standard (see
``tui_gateway/architecture_store.py``) is declared by a manifest under
``~/.hermes/services/architecture/<id>.json``. This collector turns each manifest
into the same service declaration the process, Docker, Nomad and launchd
providers produce, so the service appears on the dataflow graph with its code
(``source_files``) and an ``architecture`` annotation Portal uses to open the
model. Unlike the liveness providers there is nothing to probe: a manifest
describes a codebase, not a running process, so its presence is its liveness.
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


def collect_architecture_services(home: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every valid manifest as a service declaration. Fail-open per manifest."""
    from cron.jobs import normalize_service_declaration
    from tui_gateway import architecture_store as store

    services: List[Dict[str, Any]] = []
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
        service: Dict[str, Any] = {
            "id": store.graph_id(manifest),
            "label": declaration["name"],
            "description": declaration["description"],
            "inputs": declaration["inputs"],
            "outputs": declaration["outputs"],
            "side_effects": declaration["side_effects"],
            "source_files": declaration["source_files"],
            "architecture": store.status_for(manifest, home),
        }
        if declaration.get("relationships"):
            service["relationships"] = declaration["relationships"]
        services.append(service)
    return services
