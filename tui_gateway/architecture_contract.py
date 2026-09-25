"""The hermes.architecture document contract, version 1.

This file is the contract between a service's architecture compiler, the Harness
gateway that validates and serves the document (`architecture.describe`), and
every renderer of it (Portal natively, the static observatory on the web). It is
vendored byte-for-byte into each repository that speaks the contract and pinned
by digest there, so a change to the contract is a change in every repository at
once, on record, rather than a drift one side discovers at decode time.

What the contract fixes:

  * The document is JSON with a semver `schema_version`; the major number is the
    contract major. A consumer that supports major 1 accepts every 1.x document.
  * Five sections are REQUIRED: `components`, `interplay` (the system map),
    `extraction` (where every construction on the map was extracted from),
    `ci` (the gates that defend the model) and `inventory`, plus
    `evidence_metadata`. A service either proves where its map came from and
    what defends it, or it is not a conforming service. `stores`, `externals`,
    `layers`, `edges` and `behavior` are optional.
  * Identifiers are unique and every reference resolves: edges to nodes, flow
    steps to edges, entities to nodes (one-to-one: every construction has a
    provenance), origins to files and passes, gate wiring to jobs, the merge
    gate to at least one job.
  * Unknown fields are allowed everywhere (a 1.x minor may add them); unknown
    enum values are rejected only where the renderers branch on them.

The validator is deliberately dependency-free (no jsonschema) so both
repositories run the identical code in CI. `SCHEMA` is also exportable as a JSON
Schema (draft 2020-12 subset) for documentation and third-party tooling.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Tuple

CONTRACT_NAME = "hermes.architecture"
CONTRACT_MAJOR = 1
CONTRACT_MINOR = 0
CONTRACT_VERSION = f"{CONTRACT_MAJOR}.{CONTRACT_MINOR}"

REQUIRED_SECTIONS: Tuple[str, ...] = ("components", "interplay", "extraction", "ci", "inventory", "evidence_metadata")
OPTIONAL_SECTIONS: Tuple[str, ...] = ("stores", "externals", "layers", "edges", "behavior")

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
IDENTIFIER_RE = r"^\S.*$"

# ── Schema ───────────────────────────────────────────────────────────────────
# A JSON Schema subset: type (string or list), required, properties,
# additionalProperties (bool), items, enum, minItems, minimum, pattern, $ref
# into $defs. Anything not listed under `properties` is allowed (forward compat).


def _obj(required: List[str], properties: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "required": required, "properties": properties}
    schema.update(extra)
    return schema


def _arr(items: Any, min_items: int = 0) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "array", "items": items}
    if min_items:
        schema["minItems"] = min_items
    return schema


_STR: Dict[str, Any] = {"type": "string"}
_ID: Dict[str, Any] = {"type": "string", "pattern": IDENTIFIER_RE}
_INT: Dict[str, Any] = {"type": "integer", "minimum": 0}
_BOOL: Dict[str, Any] = {"type": "boolean"}
_NULLABLE_STR: Dict[str, Any] = {"type": ["string", "null"]}
_STR_LIST: Dict[str, Any] = _arr(_STR)

SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"https://hermes.dev/contracts/{CONTRACT_NAME}/v{CONTRACT_MAJOR}",
    "title": f"{CONTRACT_NAME} document v{CONTRACT_VERSION}",
    "type": "object",
    "required": ["schema_version", "title", "description", "source_tree_sha256", *REQUIRED_SECTIONS],
    "properties": {
        "schema_version": {"type": "string", "pattern": SEMVER_RE.pattern},
        "title": _STR,
        "description": _STR,
        "repository": _NULLABLE_STR,
        "source_tree_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        "components": _arr({"$ref": "#/$defs/component"}, 1),
        "interplay": {"$ref": "#/$defs/interplay"},
        "extraction": {"$ref": "#/$defs/extraction"},
        "ci": {"$ref": "#/$defs/ci"},
        "inventory": _obj(["files", "lines", "declarations"], {"files": _INT, "lines": _INT, "declarations": _INT}),
        "evidence_metadata": _obj(["class", "rules", "limitations"], {"class": _STR, "rules": {"type": "object"}, "limitations": _STR_LIST}),
        "layers": _arr(_obj(["id", "label", "order"], {"id": _ID, "label": _STR, "order": {"type": "integer"}})),
        "edges": _arr(_obj(["source", "target", "type"], {"source": _ID, "target": _ID, "type": _STR, "authority": _STR})),
        "stores": _obj(["items"], {"count": _INT, "items": _arr({"$ref": "#/$defs/store"})}),
        "externals": _obj(["systems"], {"systems": _arr({"$ref": "#/$defs/external"}), "groups": _arr(_obj(["id", "label"], {"id": _ID, "label": _STR})), "edges": {"type": "array"}}),
        "behavior": {"type": "object"},
    },
    "$defs": {
        "evidence": _obj(["path", "line"], {"path": _STR, "line": {"type": "integer", "minimum": 1}, "excerpt": _STR}),
        "component": _obj(
            ["id", "label", "description", "files", "declarations", "file_count", "line_count", "declaration_count"],
            {
                "id": _ID, "label": _STR, "description": _STR, "layer": _STR, "external": _BOOL,
                "files": _STR_LIST, "declarations": _STR_LIST,
                "file_count": _INT, "line_count": _INT, "declaration_count": _INT,
            },
        ),
        "interplay": _obj(
            ["nodes", "edges", "flows", "invariants"],
            {
                "nodes": _arr({"$ref": "#/$defs/node"}, 1),
                "edges": _arr({"$ref": "#/$defs/interplay_edge"}),
                "flows": _arr({"$ref": "#/$defs/flow"}),
                "invariants": _arr({"$ref": "#/$defs/invariant"}, 1),
                "pages": _arr(_obj(["id", "label"], {"id": _ID, "label": _STR, "roots": _STR_LIST, "components": _STR_LIST})),
                "boundary_groups": _arr(_obj(["id", "label", "members"], {"id": _ID, "label": _STR, "members": _STR_LIST, "description": _STR})),
                "clusters": _arr(_obj(["id", "node_ids"], {"id": _ID, "node_ids": _STR_LIST, "owner_type": _NULLABLE_STR})),
                "triggers": {"type": "array"},
                "launch": {"type": "object"},
                "machines": {"type": "object"},
            },
        ),
        "node": _obj(
            ["id", "kind", "label"],
            {
                "id": _ID, "kind": _ID, "label": _STR, "sub_kind": _NULLABLE_STR, "component": _NULLABLE_STR,
                "path": _STR, "line": {"type": "integer", "minimum": 1}, "page": _NULLABLE_STR,
                "owner_type": _NULLABLE_STR, "cluster": _STR, "history_key": _STR,
                "evidence": _arr({"$ref": "#/$defs/evidence"}),
            },
        ),
        "interplay_edge": _obj(
            ["source", "target", "relation", "class"],
            {
                "source": _ID, "target": _ID, "relation": _ID,
                "class": {"type": "string", "enum": ["structure", "interplay", "lifecycle", "boundary", "usage"]},
            },
        ),
        "flow": _obj(
            ["id", "title", "steps"],
            {
                "id": _ID, "title": _STR, "summary": _STR, "page": _NULLABLE_STR, "authority": _STR,
                "status": _STR, "interaction": _STR, "outcome": _STR, "journey": _STR,
                "steps": _arr(_obj(["from", "to", "relation"], {"from": _ID, "to": _ID, "relation": _ID, "note": _STR}), 1),
                "evidence": _arr({"$ref": "#/$defs/evidence"}),
            },
        ),
        "invariant": _obj(
            ["id", "kind", "status", "why"],
            {
                "id": _ID, "kind": _ID, "why": _STR, "checked": _INT,
                "status": {"type": "string", "enum": ["holds", "violated", "unchecked"]},
            },
        ),
        "extraction": _obj(
            ["authority", "derivation", "files", "passes", "entities", "summary"],
            {
                "authority": {"type": "string", "enum": ["observed", "declared"]},
                "derivation": _STR,
                "families": {"type": "object"},
                "files": _arr({"$ref": "#/$defs/extraction_file"}, 1),
                "passes": _arr({"$ref": "#/$defs/extraction_pass"}, 1),
                "entities": _arr({"$ref": "#/$defs/extraction_entity"}, 1),
                "summary": _obj(
                    ["files", "touched_files", "untouched_files", "declarations", "mapped_declarations", "entities", "entities_with_origin", "passes"],
                    {
                        "files": _INT, "touched_files": _INT, "untouched_files": _INT, "declarations": _INT,
                        "mapped_declarations": _INT, "entities": _INT, "entities_with_origin": _INT, "passes": _INT,
                        "citations": _INT, "semantic_citations": _INT, "by_kind": {"type": "object"},
                        "types": {"type": "object"}, "functions": {"type": "object"},
                    },
                ),
            },
        ),
        "extraction_file": _obj(
            ["path", "line_count", "declarations", "passes", "citations", "touched", "declaration_count", "mapped_declarations"],
            {
                "path": _STR, "component": _NULLABLE_STR, "line_count": _INT, "citations": _INT, "semantic_citations": _INT,
                "touched": _BOOL, "declaration_count": _INT, "mapped_declarations": _INT, "passes": _STR_LIST,
                "declarations": _arr(_obj(["kind", "name", "line", "passes"], {"kind": _ID, "name": _STR, "line": {"type": "integer", "minimum": 1}, "passes": _STR_LIST})),
            },
        ),
        "extraction_pass": _obj(
            ["id", "class", "description", "files", "citations"],
            {"id": _ID, "class": {"type": "string", "enum": ["mechanical", "semantic"]}, "description": _STR, "files": _INT, "citations": _INT},
        ),
        "extraction_entity": _obj(
            ["id", "kind", "label", "origins"],
            {
                "id": _ID, "kind": _ID, "label": _STR, "component": _NULLABLE_STR,
                "origins": _arr(_obj(["path", "line", "rule", "family"], {"path": _STR, "line": {"type": "integer", "minimum": 1}, "rule": _ID, "family": _ID, "via": _STR}), 1),
            },
        ),
        "ci": _obj(
            ["workflows", "jobs", "edges", "merge", "ratchets", "static_checks", "summary"],
            {
                "workflows": _arr({"$ref": "#/$defs/ci_workflow"}, 1),
                "jobs": _arr({"$ref": "#/$defs/ci_job"}, 1),
                "edges": _arr(_obj(["source", "target", "kind"], {"source": _ID, "target": _ID, "kind": _ID, "label": _STR})),
                "triggers": _arr(_obj(["id", "event", "workflows"], {"id": _ID, "event": _STR, "workflows": _STR_LIST})),
                "merge": _obj(["id", "label", "inputs"], {"id": _ID, "label": _STR, "inputs": _arr(_ID, 1)}),
                "ratchets": _arr({"$ref": "#/$defs/ci_ratchet"}),
                "static_checks": _arr(_obj(["name", "command", "job"], {"name": _STR, "command": _STR, "job": _ID, "scripts": _STR_LIST, "evidence": {"$ref": "#/$defs/evidence"}})),
                "architectural": {"type": "object"},
                "families": {"type": "object"},
                "limitations": _STR_LIST,
                "summary": _obj(["workflows", "jobs", "gates"], {"workflows": _INT, "jobs": _INT, "gates": _INT, "ratchets": _INT, "static_checks": _INT}),
            },
        ),
        "ci_workflow": _obj(
            ["id", "label", "family", "jobs"],
            {"id": _ID, "label": _STR, "name": _STR, "family": _ID, "jobs": _STR_LIST, "events": _STR_LIST, "path": _STR, "question": _STR},
        ),
        "ci_job": _obj(
            ["id", "name", "workflow", "family", "role", "needs"],
            {
                "id": _ID, "key": _STR, "name": _STR, "workflow": _ID, "family": _ID,
                "role": {"type": "string", "enum": ["gate", "post-merge", "manual", "disabled", "local"]},
                "needs": _STR_LIST, "runs_on": _STR, "condition": _NULLABLE_STR,
                "steps": {"type": "array"}, "step_count": _INT, "scripts": _STR_LIST,
                "artifacts_in": _STR_LIST, "artifacts_out": _STR_LIST, "pins": {"type": "array"},
                "evidence": {"$ref": "#/$defs/evidence"},
            },
        ),
        "ci_ratchet": _obj(
            ["id", "title", "job", "measures", "floor"],
            {"id": _ID, "title": _STR, "job": _ID, "measures": _STR, "floor": _STR, "patch": _NULLABLE_STR, "source": {"type": "object"}, "source_path": _STR, "current": {"type": "object"}},
        ),
        "store": _obj(["id", "label", "kind"], {"id": _ID, "label": _STR, "kind": _STR, "component": _NULLABLE_STR, "persistence": {"type": "array"}, "evidence": {"$ref": "#/$defs/evidence"}}),
        "external": _obj(["id", "label", "category"], {"id": _ID, "label": _STR, "category": _STR, "description": _STR, "component": _NULLABLE_STR, "hit_count": _INT, "file_count": _INT}),
    },
}


# ── Structural validation ────────────────────────────────────────────────────

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve(schema: Dict[str, Any]) -> Dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    assert ref.startswith("#/$defs/"), ref
    return SCHEMA["$defs"][ref[len("#/$defs/"):]]


def _check(value: Any, schema: Dict[str, Any], path: str, problems: List[str]) -> None:
    schema = _resolve(schema)
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS[t](value) for t in types):
            problems.append(f"{path}: expected {' or '.join(types)}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")
        return
    if isinstance(value, str) and "pattern" in schema and not re.match(schema["pattern"], value):
        problems.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema and value < schema["minimum"]:
        problems.append(f"{path}: {value} is below the minimum {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing required field {key!r}")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                _check(child, properties[key], f"{path}.{key}", problems)
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}: unexpected field {key!r}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{path}: needs at least {schema['minItems']} item(s), has {len(value)}")
        items = schema.get("items")
        if items is not None:
            for index, child in enumerate(value):
                _check(child, items, f"{path}[{index}]", problems)


def validate_structure(document: Any) -> List[str]:
    """Shape problems only: types, required fields, enums, patterns."""
    problems: List[str] = []
    _check(document, SCHEMA, "document", problems)
    return problems


# ── Version ──────────────────────────────────────────────────────────────────


def parse_version(document: Any) -> Tuple[int, int, int]:
    """The document's semver as integers; (0, 0, 0) when absent or malformed."""
    if not isinstance(document, dict):
        return (0, 0, 0)
    match = SEMVER_RE.match(str(document.get("schema_version") or ""))
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)  # type: ignore[return-value]


def validate_version(document: Any) -> List[str]:
    major, minor, _ = parse_version(document)
    if major == 0 and minor == 0:
        return [f"document.schema_version: missing or not semver (contract {CONTRACT_NAME} v{CONTRACT_VERSION})"]
    if major != CONTRACT_MAJOR:
        return [f"document.schema_version: major {major} is not supported (this consumer speaks {CONTRACT_NAME} v{CONTRACT_MAJOR}.x)"]
    return []


# ── Cross-reference validation ───────────────────────────────────────────────


def _ids(items: Any, key: str = "id") -> List[str]:
    return [str(item[key]) for item in items or [] if isinstance(item, dict) and key in item]


def _duplicates(values: List[str]) -> List[str]:
    seen: set = set()
    dupes: List[str] = []
    for value in values:
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes


def validate_references(document: Dict[str, Any]) -> List[str]:
    """Every reference resolves and every construction has provenance and gates.
    Assumes the structure validated; tolerates missing pieces without crashing."""
    problems: List[str] = []
    components = document.get("components") or []
    component_ids = set(_ids(components))
    for dupe in _duplicates(_ids(components)):
        problems.append(f"components: duplicate id {dupe!r}")

    interplay = document.get("interplay") if isinstance(document.get("interplay"), dict) else {}
    nodes = interplay.get("nodes") or []
    node_ids = set(_ids(nodes))
    for dupe in _duplicates(_ids(nodes)):
        problems.append(f"interplay.nodes: duplicate id {dupe!r}")
    for node in nodes:
        if isinstance(node, dict) and node.get("component") not in (None, "") and node["component"] not in component_ids:
            problems.append(f"interplay.nodes[{node.get('id')}]: component {node['component']!r} is not declared")
    for index, edge in enumerate(interplay.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        for end in ("source", "target"):
            if edge.get(end) not in node_ids:
                problems.append(f"interplay.edges[{index}]: {end} {edge.get(end)!r} is not a node")
    # Flow steps walk the drawn map, which also has page pseudo-nodes (page:<id>)
    # and the lifecycle wiring the renderer derives from operations and triggers;
    # the compiler's own flows-traceable invariant checks each hop against that
    # fuller set. The contract checks that every step ends on something that exists.
    # Steps name nodes by their stable history_key (the identity that survives
    # re-extraction), falling back to the id, plus page pseudo-nodes (page:<id>).
    step_targets = node_ids | {str(n.get("history_key")) for n in nodes if isinstance(n, dict) and n.get("history_key")}
    step_targets |= {f"page:{p['id']}" for p in interplay.get("pages") or [] if isinstance(p, dict) and "id" in p}
    for flow in interplay.get("flows") or []:
        if not isinstance(flow, dict):
            continue
        for index, step in enumerate(flow.get("steps") or []):
            if not isinstance(step, dict):
                continue
            for end in ("from", "to"):
                if step.get(end) not in step_targets:
                    problems.append(f"interplay.flows[{flow.get('id')}].steps[{index}]: {end} {step.get(end)!r} is not on the map")
    for dupe in _duplicates(_ids(interplay.get("invariants") or [])):
        problems.append(f"interplay.invariants: duplicate id {dupe!r}")

    extraction = document.get("extraction") if isinstance(document.get("extraction"), dict) else {}
    files = extraction.get("files") or []
    file_paths = set(str(f.get("path")) for f in files if isinstance(f, dict))
    for dupe in _duplicates([str(f.get("path")) for f in files if isinstance(f, dict)]):
        problems.append(f"extraction.files: duplicate path {dupe!r}")
    passes = extraction.get("passes") or []
    pass_ids = set(_ids(passes))
    if not any(isinstance(p, dict) and p.get("class") == "mechanical" for p in passes):
        problems.append("extraction.passes: no mechanical pass — an extraction map must come from the extractor, not only from people")
    for record in files:
        if not isinstance(record, dict):
            continue
        if record.get("component") not in (None, "") and record["component"] not in component_ids:
            problems.append(f"extraction.files[{record.get('path')}]: component {record['component']!r} is not declared")
        for pass_id in record.get("passes") or []:
            if pass_id not in pass_ids:
                problems.append(f"extraction.files[{record.get('path')}]: pass {pass_id!r} is not declared")
        if bool(record.get("touched")) != (int(record.get("citations") or 0) > 0):
            problems.append(f"extraction.files[{record.get('path')}]: touched must equal citations > 0")
    entities = extraction.get("entities") or []
    entity_ids = set(_ids(entities))
    for dupe in _duplicates(_ids(entities)):
        problems.append(f"extraction.entities: duplicate id {dupe!r}")
    for missing in sorted(node_ids - entity_ids):
        problems.append(f"extraction.entities: construction {missing!r} has no provenance")
    for orphan in sorted(entity_ids - node_ids):
        problems.append(f"extraction.entities: {orphan!r} is not a construction on the map")
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        for index, origin in enumerate(entity.get("origins") or []):
            if not isinstance(origin, dict):
                continue
            if origin.get("path") not in file_paths:
                problems.append(f"extraction.entities[{entity.get('id')}].origins[{index}]: {origin.get('path')!r} is not an analysed file")
            rule = str(origin.get("rule") or "")
            if rule not in pass_ids and not rule.startswith("map."):
                problems.append(f"extraction.entities[{entity.get('id')}].origins[{index}]: rule {rule!r} is not a declared pass")

    ci = document.get("ci") if isinstance(document.get("ci"), dict) else {}
    jobs = ci.get("jobs") or []
    job_ids = set(_ids(jobs))
    for dupe in _duplicates(_ids(jobs)):
        problems.append(f"ci.jobs: duplicate id {dupe!r}")
    workflow_ids = set(_ids(ci.get("workflows") or []))
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("workflow") not in workflow_ids:
            problems.append(f"ci.jobs[{job.get('id')}]: workflow {job.get('workflow')!r} is not declared")
        for need in job.get("needs") or []:
            if need not in job_ids:
                problems.append(f"ci.jobs[{job.get('id')}]: needs {need!r} which is not a job")
    for workflow in ci.get("workflows") or []:
        if isinstance(workflow, dict):
            for job_id in workflow.get("jobs") or []:
                if job_id not in job_ids:
                    problems.append(f"ci.workflows[{workflow.get('id')}]: lists job {job_id!r} which is not declared")
    trigger_ids = set(_ids(ci.get("triggers") or []))
    merge = ci.get("merge") if isinstance(ci.get("merge"), dict) else {}
    wire_targets = job_ids | trigger_ids | ({str(merge.get("id"))} if merge.get("id") else set())
    for index, edge in enumerate(ci.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        for end in ("source", "target"):
            if edge.get(end) not in wire_targets:
                problems.append(f"ci.edges[{index}]: {end} {edge.get(end)!r} is not a job, trigger or the merge gate")
    for job_id in merge.get("inputs") or []:
        if job_id not in job_ids:
            problems.append(f"ci.merge: input {job_id!r} is not a job")
    for ratchet in ci.get("ratchets") or []:
        if isinstance(ratchet, dict) and ratchet.get("job") not in job_ids:
            problems.append(f"ci.ratchets[{ratchet.get('id')}]: job {ratchet.get('job')!r} is not declared")
    for check in ci.get("static_checks") or []:
        if isinstance(check, dict) and check.get("job") not in job_ids:
            problems.append(f"ci.static_checks[{check.get('name')}]: job {check.get('job')!r} is not declared")
    return problems


def validate_document(document: Any) -> List[str]:
    """Every problem with a document, or an empty list when it conforms.
    Version first (so an unsupported major is one clear message), then shape,
    then references (only when the shape holds, so messages stay meaningful)."""
    problems = validate_version(document)
    if problems:
        return problems
    problems = validate_structure(document)
    if problems:
        return problems
    return validate_references(document)


def conforms(document: Any) -> bool:
    return not validate_document(document)


# ── Export and pinning ───────────────────────────────────────────────────────


def schema_json() -> str:
    """The schema as JSON Schema text, stable across runs, for docs and tooling."""
    return json.dumps(SCHEMA, indent=2, sort_keys=True) + "\n"


def schema_digest() -> str:
    """sha256 of the exported schema: the value each repository pins."""
    return hashlib.sha256(schema_json().encode("utf-8")).hexdigest()


def describe_contract() -> Dict[str, Any]:
    """What a gateway advertises and what a consumer checks against."""
    return {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "major": CONTRACT_MAJOR,
        "minor": CONTRACT_MINOR,
        "required_sections": list(REQUIRED_SECTIONS),
        "optional_sections": list(OPTIONAL_SECTIONS),
        "schema_digest": schema_digest(),
    }


if __name__ == "__main__":  # pragma: no cover - CLI convenience
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--schema":
        sys.stdout.write(schema_json())
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--digest":
        print(schema_digest())
        raise SystemExit(0)
    documents = sys.argv[1:] or ["architecture/model/model.json"]
    status = 0
    for path in documents:
        with open(path, "rb") as handle:
            loaded = json.loads(handle.read().decode("utf-8"))
        issues = validate_document(loaded)
        if issues:
            status = 1
            print(f"{path}: {len(issues)} problem(s)")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print(f"{path}: conforms to {CONTRACT_NAME} v{CONTRACT_VERSION}")
    raise SystemExit(status)
