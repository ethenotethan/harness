"""The smallest architecture document that conforms to hermes.architecture v1.

Shared by every architecture test: the store validates each model it reads
against the contract, so a fixture that is "just JSON with a schema_version"
no longer gets through. ``minimal_document(**overrides)`` returns a fresh,
conforming document (two components, two constructions with provenance, one
mechanical pass, one flow, one holding and one violated invariant, one local
gate feeding the merge, one store, two externals); pass top-level keys to
override sections, or mutate the result.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

DIGEST = "0" * 64


def _component(component_id: str, label: str) -> Dict[str, Any]:
    return {
        "id": component_id, "label": label, "description": f"The {label} component.", "layer": "core",
        "files": [f"src/{component_id}.py"], "declarations": [label], "file_count": 1, "line_count": 10, "declaration_count": 1,
    }


_MINIMAL: Dict[str, Any] = {
    "schema_version": "1.0.0",
    "title": "Demo",
    "description": "A demo architecture model.",
    "repository": None,
    "source_tree_sha256": DIGEST,
    "components": [_component("a", "Alpha"), _component("b", "Beta")],
    "interplay": {
        "nodes": [
            {"id": "n1", "kind": "store", "label": "Alpha", "component": "a", "path": "src/a.py", "line": 1, "history_key": "store:a:Alpha"},
            {"id": "n2", "kind": "endpoint", "label": "Beta", "component": "b", "path": "src/b.py", "line": 1, "history_key": "endpoint:b:Beta"},
        ],
        "edges": [{"source": "n1", "target": "n2", "relation": "calls", "class": "interplay"}],
        "flows": [{"id": "f1", "title": "Alpha calls Beta", "steps": [{"from": "store:a:Alpha", "to": "n2", "relation": "calls"}]}],
        "invariants": [
            {"id": "one", "kind": "single_transport", "status": "holds", "why": "One transport.", "checked": 1},
            {"id": "two", "kind": "stores_mapped", "status": "violated", "why": "Every store is on the map.", "checked": 1},
        ],
        "pages": [{"id": "home", "label": "Home"}],
    },
    "extraction": {
        "authority": "observed",
        "derivation": "Every (pass, file, line) the model cites, joined to declarations by line range.",
        "files": [
            {"path": "src/a.py", "component": "a", "line_count": 10, "citations": 1, "semantic_citations": 0, "touched": True,
             "declaration_count": 1, "mapped_declarations": 1, "passes": ["py.class"],
             "declarations": [{"kind": "class", "name": "Alpha", "line": 1, "passes": ["py.class"]}]},
            {"path": "src/b.py", "component": "b", "line_count": 10, "citations": 1, "semantic_citations": 0, "touched": True,
             "declaration_count": 1, "mapped_declarations": 1, "passes": ["py.class"],
             "declarations": [{"kind": "class", "name": "Beta", "line": 1, "passes": ["py.class"]}]},
            {"path": "src/untouched.py", "component": "b", "line_count": 4, "citations": 0, "semantic_citations": 1, "touched": False,
             "declaration_count": 1, "mapped_declarations": 0, "passes": [],
             "declarations": [{"kind": "func", "name": "helper", "line": 1, "passes": []}]},
        ],
        "passes": [
            {"id": "py.class", "class": "mechanical", "description": "A class declaration.", "files": 2, "citations": 2},
            {"id": "semantic.construct", "class": "semantic", "description": "A construct record written by a person.", "files": 1, "citations": 1},
        ],
        "entities": [
            {"id": "n1", "kind": "store", "label": "Alpha", "component": "a", "origins": [{"path": "src/a.py", "line": 1, "rule": "py.class", "family": "store"}]},
            {"id": "n2", "kind": "endpoint", "label": "Beta", "component": "b", "origins": [{"path": "src/b.py", "line": 1, "rule": "py.class", "family": "wiring"}]},
        ],
        "summary": {
            "files": 3, "touched_files": 2, "untouched_files": 1, "declarations": 3, "mapped_declarations": 2,
            "entities": 2, "entities_with_origin": 2, "passes": 1, "citations": 2, "semantic_citations": 1,
        },
    },
    "ci": {
        "workflows": [{"id": "local", "label": "Local checks", "name": "Local checks", "family": "posture", "jobs": ["local/check"], "events": ["manual"]}],
        "jobs": [{"id": "local/check", "key": "check", "name": "Compiler check", "workflow": "local", "family": "posture", "role": "local", "needs": [],
                  "steps": [{"command": "python3 build.py --check"}], "step_count": 1, "scripts": ["build.py"]}],
        "edges": [{"source": "local/check", "target": "merge:accept", "kind": "gates"}],
        "triggers": [],
        "merge": {"id": "merge:accept", "label": "Accepted", "inputs": ["local/check"]},
        "ratchets": [],
        "static_checks": [{"name": "Compiler check", "command": "python3 build.py --check", "job": "local/check", "scripts": ["build.py"]}],
        "limitations": [],
        "summary": {"workflows": 7, "jobs": 1, "gates": 14, "ratchets": 7, "static_checks": 1},
    },
    "inventory": {"files": 3, "lines": 120, "declarations": 3},
    "evidence_metadata": {"class": "static_source", "rules": {"py.class": "A class declaration."}, "limitations": []},
    "stores": {"count": 1, "items": [{"id": "s", "label": "S", "kind": "class", "type_name": "S", "component": "a"}]},
    "externals": {"systems": [{"id": "x", "label": "X", "category": "backend"}, {"id": "y", "label": "Y", "category": "backend"}]},
}


def minimal_document(**overrides: Any) -> Dict[str, Any]:
    """A fresh conforming document; ``overrides`` replace top-level keys."""
    document = copy.deepcopy(_MINIMAL)
    document.update(overrides)
    return document


def log_sink(root: Path, sink_id: str = "app", text: str = "started\n") -> Dict[str, Any]:
    """A ``file`` log sink under ``root/logs`` with ``text`` already written —
    log capture is enforced for local services, so every local test manifest
    declares one."""
    path = Path(root) / "logs" / f"{sink_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"id": sink_id, "kind": "file", "path": str(path)}


def local_manifest(root: Path, name: str = "Demo", description: str = "A demo service.",
                   logs: Optional[List[Dict[str, Any]]] = None, **extra: Any) -> Dict[str, Any]:
    """A conforming local manifest dict: root, a declared log sink, plus ``extra``."""
    manifest: Dict[str, Any] = {"name": name, "description": description, "root": str(root),
                                "logs": logs if logs is not None else [log_sink(Path(root))]}
    manifest.update(extra)
    return manifest


def write_manifest(manifests_dir: Path, service_id: str, manifest: Dict[str, Any]) -> Path:
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / f"{service_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path
