"""The language-independent half of the architecture compiler.

One compiler core, one language pack per language (``packs/``), one contract
(``tui_gateway/architecture_contract.py``). The core reads a service's
``architecture/config.json``, assigns every source file to a component (fail
closed), hands each file to the pack for its extension (a service may mix
languages), and assembles the conforming document: the system map, the
extraction map with one provenance entity per construction, declared additions
that must resolve, declared external systems that must be observed, boundary
groups, the gates, the inventory and the invariants the compiler itself proved.

Deterministic by construction: sorted collections, no timestamps, the model
validated against the contract before it is written, ``--check`` comparing
bytes. The Python ``ast`` compiler (``tools/architecture_compile_python.py``)
remains as the first-generation Python front end; its config shape, gates
builder and workflow reader are reused here unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # run as a module from any directory
    sys.path.insert(0, str(REPO_ROOT))

from tools.architecture_compile_python import (  # noqa: E402  (reused, never edited here)
    CONFIG_PATH,
    DEFAULT_EXCLUDES,
    MODEL_PATH,
    SCHEMA_VERSION,
    CompileError,
    _matches,
    build_ci,
    load_config,
)
from tools.architecture_compiler.packs import PACKS, pack_for_path  # noqa: E402
from tools.architecture_compiler.packs.base import (  # noqa: E402
    Citation,
    Declaration,
    LanguagePack,
    Rule,
    enclosing_type,
    mask,
    query_citations,
    regex_citations,
)
from tui_gateway.architecture_contract import CONTRACT_NAME, CONTRACT_VERSION, validate_document  # noqa: E402

COMPILER_ID = "tools/architecture_compiler"
CHECK_COMMAND = "python3 -m tools.architecture_compiler . --check"
SIGNATURE_RULE = "boundary.external_signature"
DECLARED_RULE = "declared.node"
EDGE_CLASSES = ("structure", "interplay", "lifecycle", "boundary", "usage")

CORE_RULES: Dict[str, Tuple[str, str]] = {
    "module": ("Every analysed source file is a construction: the file that declares what the other passes find", "wiring"),
    SIGNATURE_RULE: ("A configured external-system signature present in the code (scope code) or inside a string literal (scope strings)", "boundary"),
    DECLARED_RULE: ("A construction a person declared in architecture/config.json, citing the file and line it lives at (semantic layer, not extraction)", "wiring"),
}
SEMANTIC_RULES = {DECLARED_RULE}
LIMITATIONS = (
    "Static source evidence: dynamic dispatch, reflection, plugin registries, routes assembled at runtime and macro-generated code are invisible to the passes.",
    "A rule fires on the text the grammar exposes; a wrapper in another file hides the mechanism from its callers.",
    "External packages are attributed to the type whose body names the import; module-level use is attributed to the module.",
    "Declared external systems are observed through their configured signatures only; a system reached through an unlisted API is not attributed.",
)


# ── Config: the shared shape plus what the multi-language core adds ─────────


def load_extended_config(root: Path) -> Dict[str, Any]:
    """The Python compiler's config validation — which already normalises
    ``external_systems`` (signatures compiled to (pattern, scope, regex)) and
    ``external_groups`` (a category in one group at most) — then the keys this
    core adds: ``rules`` and ``languages``."""
    config = load_config(root)
    rules = config.get("rules") or []
    if not isinstance(rules, list):
        raise CompileError(f"{CONFIG_PATH}: 'rules' must be a list")
    for raw in rules:
        project_rule(raw)  # validates
    languages = config.get("languages")
    if languages is not None:
        if not isinstance(languages, list) or not all(isinstance(l, str) and l in PACKS for l in languages):
            raise CompileError(f"{CONFIG_PATH}: 'languages' must list pack names from {sorted(PACKS)}")
    return config


def project_rule(raw: Any) -> Tuple[str, Rule]:
    """A project rule from config: (language, Rule). Ids are prefixed with the
    language so they sit in that pack's namespace and never collide with the
    baseline."""
    if not isinstance(raw, dict):
        raise CompileError(f"{CONFIG_PATH}: every rule must be an object")
    for key in ("id", "description", "family", "language"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise CompileError(f"{CONFIG_PATH}: rule {key!r} must be a non-empty string: {raw}")
    if raw["language"] not in PACKS:
        raise CompileError(f"{CONFIG_PATH}: rule {raw['id']!r}: language {raw['language']!r} is not one of {sorted(PACKS)}")
    scope = str(raw.get("scope") or ("query" if raw.get("query") else "code"))
    pattern = raw.get("query") if scope == "query" else raw.get("regex")
    if not isinstance(pattern, str) or not pattern:
        raise CompileError(f"{CONFIG_PATH}: rule {raw['id']!r} needs a 'regex' (scope code|raw|strings) or a 'query' (scope query)")
    produces = raw.get("produces") or {}
    if produces and not isinstance(produces, dict):
        raise CompileError(f"{CONFIG_PATH}: rule {raw['id']!r}: 'produces' must be an object")
    rule_id = raw["id"] if raw["id"].startswith(raw["language"] + ".") else f"{raw['language']}.{raw['id']}"
    try:
        rule = Rule(
            id=rule_id, description=raw["description"], family=raw["family"], scope=scope, pattern=pattern,
            produces=produces.get("kind") if produces else None, sub_kind=str(produces.get("sub_kind") or "") if produces else "",
            label_group=int(produces.get("label_group") or 0) if produces else 0,
            relation=produces.get("relation") if produces else None, edge_class=produces.get("edge_class") if produces else None,
            label_prefix=str(produces.get("label_prefix") or "") if produces else "",
        )
    except (ValueError, re.error) as exc:
        raise CompileError(f"{CONFIG_PATH}: rule {raw['id']!r}: {exc}") from exc
    if rule.edge_class is not None and rule.edge_class not in EDGE_CLASSES:
        raise CompileError(f"{CONFIG_PATH}: rule {raw['id']!r}: edge_class must be one of {EDGE_CLASSES}")
    return raw["language"], rule


# ── Sources ──────────────────────────────────────────────────────────────────


def read_sources(root: Path, config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """Every source file a pack claims, assigned to one component, hashed."""
    source_root = (root / str(config.get("source_root") or ".")).resolve()
    if not source_root.is_dir():
        raise CompileError(f"source_root {source_root} is not a directory")
    excludes = tuple(DEFAULT_EXCLUDES) + tuple(str(p) for p in config.get("exclude") or [])
    allowed = set(config.get("languages") or PACKS)
    files: List[Dict[str, Any]] = []
    digest = hashlib.sha256()
    unassigned: List[str] = []
    for path in sorted(p for p in source_root.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(source_root).parts)):
        pack = pack_for_path(path)
        if pack is None or pack.name not in allowed:
            continue
        rel = path.relative_to(source_root).as_posix()
        if _matches(rel, excludes):
            continue
        text = path.read_text(encoding="utf-8")
        component = next((str(c["id"]) for c in config["components"] if _matches(rel, c["patterns"])), None)
        if component is None:
            unassigned.append(rel)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
        files.append({"rel": rel, "path": rel, "text": text, "component": component, "line_count": len(text.splitlines()), "pack": pack})
    if not files:
        raise CompileError(f"no source files for any language pack under {source_root} (packs: {', '.join(sorted(PACKS))})")
    if unassigned:
        raise CompileError("every file must belong to one component; unassigned: " + ", ".join(unassigned))
    return files, digest.hexdigest()


# ── Per-file extraction ──────────────────────────────────────────────────────


class FileExtraction:
    """One file through its pack: parse, mask, census, imports, rules."""

    def __init__(self, record: Dict[str, Any], config: Dict[str, Any], project_rules: Sequence[Rule], local_roots: frozenset,
                 parser: Any, language: Any) -> None:
        self.rel: str = record["rel"]
        self.component: str = record["component"]
        self.text: str = record["text"]
        self.line_count: int = record["line_count"]
        self.pack: LanguagePack = record["pack"]
        source = self.text.encode("utf-8")
        tree = parser.parse(source)
        root = tree.root_node
        self.code, self.raw, self.strings = mask(self.text, root, self.pack.comment_types, self.pack.string_types)
        self.declarations: List[Declaration] = sorted(self.pack.census(root, source), key=lambda d: (d.line, -d.end_line, d.kind, d.name))
        self.imports = self.pack.imports(root, source)
        self.external_roots: List[Tuple[str, int]] = [
            (imp.root, imp.line) for imp in self.imports
            if imp.root and imp.root not in self.pack.stdlib and not any(imp.root == local or imp.root.startswith(local + "/") for local in local_roots)
        ]
        self.entrypoints: List[Tuple[int, str]] = self.pack.entrypoints(root, self.text, self.rel)
        self.citations: List[Citation] = []
        for rule in list(self.pack.rules) + list(project_rules):
            if rule.scope == "query":
                self.citations.extend(query_citations(rule, language, root, source))
            else:
                self.citations.extend(regex_citations(rule, self.code, self.raw, self.strings))
        for rule in self.pack.rules:
            if rule.id.endswith(".import_external"):
                self.citations.extend(Citation(rule.id, line, root_name) for root_name, line in self.external_roots)
        self.citations.sort(key=lambda c: (c.line, c.rule, c.label))

    def enclosing(self, line: int) -> Optional[Declaration]:
        return enclosing_type(self.declarations, line)

    def cite(self, rule: str, line: int, label: str = "") -> None:
        self.citations.append(Citation(rule, line, label))


def local_roots_for(files: Sequence[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, frozenset]:
    """Per pack, the module roots that belong to the service itself."""
    by_pack: Dict[str, List[str]] = {}
    for record in files:
        by_pack.setdefault(record["pack"].name, []).append(record["rel"])
    return {name: PACKS[name].local_roots(rels, config) for name, rels in by_pack.items()}


# ── Identity ─────────────────────────────────────────────────────────────────


def module_id(component: str, rel: str) -> str:
    return f"module:{component}:{rel}"


def type_id(component: str, name: str) -> str:
    return f"type:{component}:{name}"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._/-]+", "-", text).strip("-") or "item"


# ── Model ────────────────────────────────────────────────────────────────────


def build_model(root: Path, config: Dict[str, Any], files: List[Dict[str, Any]], source_hash: str) -> Dict[str, Any]:
    from tree_sitter import Parser  # the dependency is required to compile; importing here keeps `--help` cheap
    import tree_sitter_language_pack as language_pack

    languages: Dict[str, Any] = {}  # keyed by grammar name: a pack may parse some extensions with a sibling grammar
    parsers: Dict[str, Any] = {}
    for record in files:
        grammar = record["pack"].grammar_for(record["rel"])
        if grammar not in parsers:
            languages[grammar] = language_pack.get_language(grammar)
            parsers[grammar] = Parser(languages[grammar])
    project_rules: Dict[str, List[Rule]] = {}
    for raw in config.get("rules") or []:
        language, rule = project_rule(raw)
        project_rules.setdefault(language, []).append(rule)
    locals_by_pack = local_roots_for(files, config)
    extractions = [
        FileExtraction(record, config, project_rules.get(record["pack"].name, []), locals_by_pack[record["pack"].name],
                       parsers[record["pack"].grammar_for(record["rel"])], languages[record["pack"].grammar_for(record["rel"])])
        for record in files
    ]
    rules_by_id: Dict[str, Rule] = {}
    for pack in PACKS.values():
        for rule in pack.rules:
            rules_by_id[rule.id] = rule
    for rules in project_rules.values():
        for rule in rules:
            rules_by_id[rule.id] = rule

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Set[Tuple[str, str, str, str]] = set()
    origins: Dict[str, List[Tuple[str, int, str]]] = {}
    types_by_name: Dict[str, List[Tuple[str, str, str]]] = {}  # name → [(component, language, node id)]
    enums_by_name: Dict[str, List[Tuple[str, str, str]]] = {}

    def resolve(index: Dict[str, List[Tuple[str, str, str]]], name: str, ex: "FileExtraction") -> Optional[str]:
        """The construction a bare name means from inside ``ex``: one declared in
        the same component first, else the only one in the same language."""
        candidates = index.get(name) or []
        same_component = [nid for component, _, nid in candidates if component == ex.component]
        if same_component:
            return same_component[0]
        same_language = [nid for _, language, nid in candidates if language == ex.pack.name]
        return same_language[0] if len(same_language) == 1 else None

    def add_node(node_id: str, **fields: Any) -> None:
        if node_id in nodes:
            return
        nodes[node_id] = {"id": node_id, "history_key": node_id, **fields}

    def add_origin(node_id: str, path: str, line: int, rule: str) -> None:
        origins.setdefault(node_id, []).append((path, line, rule))

    def add_edge(source: str, target: str, relation: str, edge_class: str) -> None:
        if edge_class not in EDGE_CLASSES:
            raise CompileError(f"edge class {edge_class!r} is not one of {EDGE_CLASSES}")
        if source != target:
            edges.add((source, target, relation, edge_class))

    # Modules, types, entrypoints.
    for ex in extractions:
        mid = module_id(ex.component, ex.rel)
        add_node(mid, kind="module", sub_kind=ex.pack.name, label=ex.rel, component=ex.component, path=ex.rel, line=1, owner_type=None, language=ex.pack.name)
        add_origin(mid, ex.rel, 1, "module")
        ex.cite("module", 1)
        for declaration in ex.declarations:
            if not declaration.is_type:
                continue
            tid = type_id(ex.component, declaration.name)
            kind = ex.pack.type_kind(declaration)
            add_node(tid, kind=kind, sub_kind=declaration.kind, label=declaration.name, component=ex.component, path=ex.rel,
                     line=declaration.line, owner_type=ex.rel, language=ex.pack.name)
            type_rule = f"{ex.pack.name}.type"
            add_origin(tid, ex.rel, declaration.line, type_rule)
            ex.cite(type_rule, declaration.line, declaration.name)
            add_edge(mid, tid, "declares", "structure")
            entry = (ex.component, ex.pack.name, tid)
            if entry not in types_by_name.setdefault(declaration.name, []):
                types_by_name[declaration.name].append(entry)
            if declaration.kind == "enum" and entry not in enums_by_name.setdefault(declaration.name, []):
                enums_by_name[declaration.name].append(entry)
        for line, label in ex.entrypoints:
            eid = f"entrypoint:{ex.component}:{ex.rel}"
            add_node(eid, kind="entrypoint", sub_kind="main", label=f"{label} · {ex.rel}", component=ex.component, path=ex.rel, line=line, owner_type=ex.rel, language=ex.pack.name)
            add_origin(eid, ex.rel, line, f"{ex.pack.name}.entrypoint")
            add_edge(mid, eid, "declares", "structure")
            ex.cite(f"{ex.pack.name}.entrypoint", line)

    def owner_of(ex: FileExtraction, line: int) -> str:
        declaration = ex.enclosing(line)
        return type_id(ex.component, declaration.name) if declaration else module_id(ex.component, ex.rel)

    # Declared external systems: signatures over code and strings, fail closed on silence.
    systems = config.get("external_systems") or []  # normalised by load_config: signatures are (pattern, scope, regex)
    system_hits: Dict[str, List[Tuple[str, int]]] = {s["id"]: [] for s in systems}
    absorbing: Dict[str, str] = {}  # import root → system id whose signature matches it
    for system in systems:
        compiled = [(regex, scope) for _, scope, regex in system["signatures"]]
        for ex in extractions:
            for pattern, scope in compiled:
                if scope == "strings":
                    for line, contents in ex.strings:
                        if pattern.search(contents):
                            system_hits[system["id"]].append((ex.rel, line))
                            ex.cite(SIGNATURE_RULE, line, system["id"])
                else:
                    for match in pattern.finditer(ex.code):
                        line = ex.code.count("\n", 0, match.start()) + 1
                        system_hits[system["id"]].append((ex.rel, line))
                        ex.cite(SIGNATURE_RULE, line, system["id"])
            for root_name, _ in ex.external_roots:
                if any(scope == "code" and pattern.search(root_name) for pattern, scope in compiled):
                    absorbing.setdefault(root_name, system["id"])
    silent = sorted(s["id"] for s in systems if not system_hits[s["id"]])
    if silent:
        raise CompileError("declared external system(s) never observed — no signature hit anywhere in the tree: " + ", ".join(silent))
    for system in systems:
        hits = sorted(set(system_hits[system["id"]]))
        xid = f"external:{system['id']}"
        add_node(xid, kind="external", sub_kind="system", label=system["label"], component=None, path=hits[0][0], line=hits[0][1],
                 owner_type="External systems", category=system["category"], description=system["description"], protocol=str(system.get("protocol") or ""))
        for path, line in hits:
            add_origin(xid, path, line, SIGNATURE_RULE)
    hits_by_owner: Dict[Tuple[str, str], int] = {}
    for ex in extractions:
        for citation in ex.citations:
            if citation.rule == SIGNATURE_RULE:
                add_edge(owner_of(ex, citation.line), f"external:{citation.label}", "uses", "boundary")
                key = (citation.label, ex.component)
                hits_by_owner[key] = hits_by_owner.get(key, 0) + 1

    # External packages, absorbed by a declared system when a signature names them.
    for ex in extractions:
        for root_name, line in ex.external_roots:
            target = f"external:{absorbing[root_name]}" if root_name in absorbing else f"external:{slug(root_name)}"
            if root_name not in absorbing:
                add_node(target, kind="external", sub_kind="package", label=root_name, component=None, path=ex.rel, line=line,
                         owner_type="External packages", category="package", description=f"{ex.pack.label} package {root_name}", protocol="")
                add_origin(target, ex.rel, line, f"{ex.pack.name}.import_external")
            # Attribute the package to every type whose body names it, else the module.
            users = [d for d in ex.declarations if d.is_type and re.search(r"\b" + re.escape(root_name.split("/")[-1].split("@")[-1]) + r"\b", ex.code[_offset(ex.code, d.line):_offset(ex.code, d.end_line + 1)])]
            if users:
                for declaration in users:
                    add_edge(type_id(ex.component, declaration.name), target, "uses", "boundary")
            else:
                add_edge(module_id(ex.component, ex.rel), target, "uses", "boundary")

    # Rule-produced constructions.
    for ex in extractions:
        for citation in ex.citations:
            rule = rules_by_id.get(citation.rule)
            if rule is None or rule.produces is None:
                continue
            label = citation.label or rule.id.rsplit(".", 1)[-1]
            if rule.produces == "external":
                node_id = f"external:{slug(label)}"
                add_node(node_id, kind="external", sub_kind=rule.sub_kind or "package", label=label, component=None, path=ex.rel, line=citation.line,
                         owner_type="External packages", category=rule.sub_kind or "package", description=rule.description, protocol="")
            elif rule.produces == "endpoint":
                node_id = f"endpoint:{ex.component}:{slug(rule.sub_kind or 'route')}:{slug(label)}"
                add_node(node_id, kind="endpoint", sub_kind=rule.sub_kind or "route", label=label, component=ex.component, path=ex.rel, line=citation.line,
                         owner_type=(ex.enclosing(citation.line).name if ex.enclosing(citation.line) else ex.rel))
            else:
                node_id = f"{rule.produces}:{ex.component}:{ex.rel}:{citation.line}"
                add_node(node_id, kind=rule.produces, sub_kind=rule.sub_kind or rule.id.rsplit(".", 1)[-1], label=label, component=ex.component, path=ex.rel,
                         line=citation.line, owner_type=(ex.enclosing(citation.line).name if ex.enclosing(citation.line) else ex.rel))
            add_origin(node_id, ex.rel, citation.line, rule.id)
            relation, edge_class = rule.wiring
            add_edge(owner_of(ex, citation.line), node_id, relation, edge_class)

    # State machines: an enum type assigned somewhere in the tree.
    for ex in extractions:
        rule_id = f"{ex.pack.name}.state_machine"
        if rule_id not in rules_by_id:
            continue
        visible_enums = {name: resolve(enums_by_name, name, ex) for name in enums_by_name}
        visible_enums = {name: nid for name, nid in visible_enums.items() if nid}
        typed_props: Dict[str, str] = {}
        if ex.pack.typed_property_pattern:
            for match in re.finditer(ex.pack.typed_property_pattern, ex.code, re.MULTILINE):
                if match.group(2) in visible_enums:
                    typed_props[match.group(1)] = match.group(2)
        separator = re.escape(ex.pack.enum_separator)
        for enum_name, enum_node in visible_enums.items():
            for match in re.finditer(r"\b(\w+)\s*=\s*" + re.escape(enum_name) + separator + r"\w+", ex.code):
                line = ex.code.count("\n", 0, match.start()) + 1
                ex.cite(rule_id, line, enum_name)
                add_origin(enum_node, ex.rel, line, rule_id)
                add_edge(owner_of(ex, line), enum_node, "transitions", "lifecycle")
        for prop, enum_name in typed_props.items():
            for match in re.finditer(r"\b" + re.escape(prop) + r"\s*=\s*\.\w+", ex.code):
                line = ex.code.count("\n", 0, match.start()) + 1
                ex.cite(rule_id, line, enum_name)
                add_origin(visible_enums[enum_name], ex.rel, line, rule_id)
                add_edge(owner_of(ex, line), visible_enums[enum_name], "transitions", "lifecycle")

    # Entrypoints construct what they name; pack-specific extra edges (SwiftUI triggers).
    for ex in extractions:
        for line, _ in ex.entrypoints:
            eid = f"entrypoint:{ex.component}:{ex.rel}"
            region = ex.code[_offset(ex.code, line):]
            for name in types_by_name:
                tid = resolve(types_by_name, name, ex)
                if tid and re.search(r"\b" + re.escape(name) + r"\s*[({]", region):
                    add_edge(eid, tid, "constructs", "structure")
        if ex.pack.extra_edges is not None:
            for source_name, target_name, relation, edge_class, line, rule_id in ex.pack.extra_edges(ex.code, ex.declarations, ex.rel):
                source_id, target_id = resolve(types_by_name, source_name, ex), resolve(types_by_name, target_name, ex)
                if source_id and target_id:
                    add_edge(source_id, target_id, relation, edge_class)
                    if rule_id in rules_by_id:
                        ex.cite(rule_id, line, target_name)
                        add_origin(target_id, ex.rel, line, rule_id)

    # Declared additions.
    declared = config.get("declared") or {}
    analysed = {ex.rel for ex in extractions}
    semantic_by_file: Dict[str, int] = {}
    component_ids = {c["id"] for c in config["components"]}
    for item in declared.get("nodes") or []:
        for key in ("id", "kind", "label", "path"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise CompileError(f"declared node needs string {key!r}: {item}")
        if item["path"] not in analysed or not isinstance(item.get("line"), int) or item["line"] < 1:
            raise CompileError(f"declared node {item['id']!r} must cite an analysed file and a line ≥ 1")
        if item["id"] in nodes:
            raise CompileError(f"declared node {item['id']!r} collides with an extracted construction")
        component = item.get("component")
        if component is not None and component not in component_ids:
            raise CompileError(f"declared node {item['id']!r} names an undeclared component {component!r}")
        add_node(item["id"], kind=item["kind"], sub_kind="declared", label=item["label"], component=component, path=item["path"], line=item["line"], owner_type=None, authority="declared")
        add_origin(item["id"], item["path"], item["line"], DECLARED_RULE)
        semantic_by_file[item["path"]] = semantic_by_file.get(item["path"], 0) + 1
    for item in declared.get("edges") or []:
        for key in ("source", "target", "relation"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise CompileError(f"declared edge needs string {key!r}: {item}")
        for end in ("source", "target"):
            if item[end] not in nodes:
                raise CompileError(f"declared edge {item['source']} -{item['relation']}-> {item['target']}: {end} {item[end]!r} is not on the map")
        add_edge(item["source"], item["target"], item["relation"], "usage")

    node_list = [nodes[k] for k in sorted(nodes)]
    edge_list = [{"source": s, "target": t, "relation": r, "class": c} for s, t, r, c in sorted(edges)]
    edge_keys = {(e["source"], e["target"], e["relation"]) for e in edge_list}

    flows: List[Dict[str, Any]] = []
    for item in declared.get("flows") or []:
        for key in ("id", "title"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise CompileError(f"declared flow needs string {key!r}: {item}")
        steps = item.get("steps")
        if not isinstance(steps, list) or not steps:
            raise CompileError(f"declared flow {item['id']!r} needs at least one step")
        for step in steps:
            key = (step.get("from"), step.get("to"), step.get("relation"))
            if key not in edge_keys:
                raise CompileError(f"declared flow {item['id']!r}: no edge {key[0]} -{key[2]}-> {key[1]} on the map")
        flows.append({"id": item["id"], "title": item["title"], "summary": str(item.get("summary") or ""), "authority": "declared", "status": "declared",
                      "steps": [{"from": s["from"], "to": s["to"], "relation": s["relation"], "note": str(s.get("note") or "")} for s in steps]})
    flows.sort(key=lambda f: f["id"])

    # Boundary groups by category, fail closed on an empty group.
    externals = [n for n in node_list if n["kind"] == "external"]
    boundary_groups = []
    for group in config.get("external_groups") or []:
        members = sorted(n["id"] for n in externals if n.get("category") in set(group["categories"]))
        if not members:
            raise CompileError(f"external group {group['id']!r} has no members: no external system carries one of its categories")
        boundary_groups.append({"id": group["id"], "label": group["label"], "description": str(group.get("description") or ""),
                                "categories": sorted(group["categories"]), "members": members})
    externals_section = {
        "systems": [
            {
                "id": n["id"][len("external:"):], "label": n["label"], "category": n.get("category") or "package", "description": n.get("description") or "",
                "protocol": n.get("protocol") or "", "authority": "observed", "description_authority": "specified" if n.get("sub_kind") == "system" else "observed",
                "hit_count": len(origins.get(n["id"], [])), "file_count": len({p for p, _, _ in origins.get(n["id"], [])}),
                "paths": sorted({p for p, _, _ in origins.get(n["id"], [])}),
                "component": max((k for k in hits_by_owner if k[0] == n["id"][len("external:"):]), key=lambda k: (hits_by_owner[k], k[1]), default=(None, None))[1],
            }
            for n in externals
        ],
        "groups": [{"id": g["id"], "label": g["label"], "description": g["description"], "categories": g["categories"]} for g in boundary_groups],
        "edges": [],
    }

    invariants = [
        {"id": "components-assign-every-file", "kind": "components_assign_every_file", "status": "holds", "checked": len(files),
         "why": "Every source file belongs to exactly one declared component; an unassigned file fails the build."},
        {"id": "entities-have-origin", "kind": "entities_have_origin", "status": "holds", "checked": len(node_list),
         "why": "Every construction on the map was extracted from a cited file and line, or declared by a person citing one."},
        {"id": "externals-declared-and-observed", "kind": "externals_declared_and_observed", "status": "holds", "checked": len(systems),
         "why": "Every declared external system was observed through at least one of its signatures; a silent declaration fails the build."},
    ]
    for item in declared.get("invariants") or []:
        for key in ("id", "kind", "why"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise CompileError(f"declared invariant needs string {key!r}: {item}")
        invariants.append({"id": item["id"], "kind": item["kind"], "status": "unchecked", "checked": 0, "why": item["why"]})
    if len({i["id"] for i in invariants}) != len(invariants):
        raise CompileError("duplicate invariant id")

    ci = build_ci(root, config)
    check_job = next((j["id"] for j in ci["jobs"] if any("tools.architecture_compiler" in s.get("command", "") and "--check" in s.get("command", "") for s in j["steps"])), None)
    if check_job and not any(c["job"] == check_job and "architecture_compiler" in c["command"] for c in ci["static_checks"]):
        ci["static_checks"].append({"name": "Architecture model is current", "command": CHECK_COMMAND, "job": check_job,
                                    "scripts": ["tools/architecture_compiler"], "evidence": {"path": CONFIG_PATH, "line": 1}})
        ci["static_checks"].sort(key=lambda c: (c["job"], c["name"]))
        ci["summary"]["static_checks"] = len(ci["static_checks"])
    invariants.append({"id": "gates-declared", "kind": "gates_declared", "status": "holds", "checked": len(ci["merge"]["inputs"]),
                       "why": "At least one gate must pass before Harness accepts a snapshot of this model."})

    descriptions: Dict[str, Tuple[str, str]] = dict(CORE_RULES)
    for rule in rules_by_id.values():
        descriptions[rule.id] = (rule.description, rule.family)
    used_packs = sorted({ex.pack.name for ex in extractions})
    for pack_name in used_packs:
        descriptions[f"{pack_name}.type"] = (f"A {PACKS[pack_name].label} type declaration (class, struct, enum, protocol, interface, trait, actor, extension)", "wiring")
        descriptions[f"{pack_name}.entrypoint"] = (f"Where a {PACKS[pack_name].label} program starts", "trigger")
    extraction = build_extraction(extractions, node_list, origins, semantic_by_file, descriptions)

    components = []
    for configured in config["components"]:
        owned = [ex for ex in extractions if ex.component == configured["id"]]
        names = sorted({d.name for ex in owned for d in ex.declarations if d.is_type})
        components.append({
            "id": configured["id"], "label": configured["label"], "description": configured["description"],
            "layer": str(configured.get("layer") or ""), "external": False,
            "files": [ex.rel for ex in owned], "declarations": names,
            "file_count": len(owned), "line_count": sum(ex.line_count for ex in owned), "declaration_count": len(names),
        })
    layers = [{"id": str(l["id"]), "label": str(l["label"]), "order": int(l["order"])} for l in config.get("layers") or []]
    limitations = list(LIMITATIONS)
    for pack_name in used_packs:
        limitations.extend(PACKS[pack_name].limitations)

    return {
        "schema_version": SCHEMA_VERSION,
        "contract": {"name": CONTRACT_NAME, "version": CONTRACT_VERSION},
        "title": config["title"],
        "description": config["description"],
        "repository": config.get("repository"),
        "source_tree_sha256": source_hash,
        "compiler": COMPILER_ID,
        "languages": used_packs,
        "components": components,
        "layers": layers,
        "interplay": {"nodes": node_list, "edges": edge_list, "flows": flows, "invariants": invariants, "pages": [], "boundary_groups": boundary_groups, "clusters": []},
        "extraction": extraction,
        "externals": externals_section,
        "ci": ci,
        "inventory": {"files": len(files), "lines": sum(f["line_count"] for f in files), "declarations": extraction["summary"]["declarations"],
                      "by_language": {name: {"files": sum(1 for f in files if f["pack"].name == name), "lines": sum(f["line_count"] for f in files if f["pack"].name == name)} for name in used_packs}},
        "evidence_metadata": {"class": "static_source", "rules": {k: v[0] for k, v in sorted(descriptions.items())}, "limitations": limitations},
    }


def _offset(text: str, line: int) -> int:
    """Byte offset of the start of a 1-based line (len(text) past the end)."""
    if line <= 1:
        return 0
    position = -1
    for _ in range(line - 1):
        position = text.find("\n", position + 1)
        if position < 0:
            return len(text)
    return position + 1


def build_extraction(extractions: List[FileExtraction], node_list: List[Dict[str, Any]], origins: Dict[str, List[Tuple[str, int, str]]],
                     semantic_by_file: Dict[str, int], descriptions: Dict[str, Tuple[str, str]]) -> Dict[str, Any]:
    pass_files: Dict[str, Set[str]] = {}
    pass_citations: Dict[str, int] = {}
    files: List[Dict[str, Any]] = []
    by_kind: Dict[str, Dict[str, int]] = {}
    for ex in sorted(extractions, key=lambda e: e.rel):
        mechanical = sorted({(c.rule, c.line) for c in ex.citations})
        for rule, _ in mechanical:
            pass_files.setdefault(rule, set()).add(ex.rel)
            pass_citations[rule] = pass_citations.get(rule, 0) + 1
        declarations = []
        for declaration in ex.declarations:
            rules = sorted({rule for rule, line in mechanical if declaration.line <= line <= declaration.end_line})
            bucket = by_kind.setdefault(declaration.kind, {"total": 0, "mapped": 0})
            bucket["total"] += 1
            bucket["mapped"] += 1 if rules else 0
            declarations.append({"kind": declaration.kind, "name": declaration.name, "line": declaration.line, "passes": rules})
        semantic = semantic_by_file.get(ex.rel, 0)
        if semantic:
            pass_files.setdefault(DECLARED_RULE, set()).add(ex.rel)
            pass_citations[DECLARED_RULE] = pass_citations.get(DECLARED_RULE, 0) + semantic
        files.append({
            "path": ex.rel, "component": ex.component, "language": ex.pack.name, "line_count": ex.line_count,
            "passes": sorted({rule for rule, _ in mechanical}), "citations": len(mechanical), "semantic_citations": semantic,
            "declaration_count": len(declarations), "mapped_declarations": sum(1 for d in declarations if d["passes"]),
            "touched": bool(mechanical), "declarations": declarations,
        })
    passes = [
        {"id": rule, "class": "semantic" if rule in SEMANTIC_RULES else "mechanical", "description": descriptions[rule][0],
         "files": len(pass_files[rule]), "citations": pass_citations[rule]}
        for rule in sorted(pass_files)
    ]
    entities = []
    for node in node_list:
        seen: Set[Tuple[str, int, str]] = set()
        node_origins = []
        for path, line, rule in sorted(origins.get(node["id"], [])):
            if (path, line, rule) in seen:
                continue
            seen.add((path, line, rule))
            node_origins.append({"path": path, "line": line, "rule": rule, "family": descriptions[rule][1]})
        entities.append({"id": node["id"], "kind": node["kind"], "label": node["label"], "component": node.get("component"), "origins": node_origins})
    type_kinds = {"class", "struct", "enum", "protocol", "interface", "trait", "actor", "extension", "impl", "type", "module"}
    return {
        "authority": "observed",
        "derivation": "Every (pass, file, line) a language pack cited, joined to the declarations of the analysed tree by body range; "
                      "one entity per construction on the map with the citations that produced it. Declared constructions are the semantic class.",
        "families": {"behaviour": "Concurrency, lifecycle and state", "boundary": "External packages and systems, HTTP, sockets, environment",
                     "store": "Persistent stores and file writes", "trigger": "Entrypoints, routes and UI triggers", "wiring": "Modules, types, RPC calls and declared constructions"},
        "files": files,
        "passes": passes,
        "entities": entities,
        "summary": {
            "files": len(files), "touched_files": sum(1 for f in files if f["touched"]), "untouched_files": sum(1 for f in files if not f["touched"]),
            "declarations": sum(f["declaration_count"] for f in files), "mapped_declarations": sum(f["mapped_declarations"] for f in files),
            "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
            "types": {"total": sum(v["total"] for k, v in by_kind.items() if k in type_kinds), "mapped": sum(v["mapped"] for k, v in by_kind.items() if k in type_kinds)},
            "functions": dict(by_kind.get("function", {"total": 0, "mapped": 0})),
            "citations": sum(f["citations"] for f in files), "semantic_citations": sum(f["semantic_citations"] for f in files),
            "passes": sum(1 for p in passes if p["class"] == "mechanical"),
            "entities": len(entities), "entities_with_origin": sum(1 for e in entities if e["origins"]),
        },
    }


# ── Driver ───────────────────────────────────────────────────────────────────


def compile_service(root: Path) -> Dict[str, Any]:
    """The conforming model for the service at ``root``; CompileError when the
    tree cannot be described or the result would not conform."""
    root = root.resolve()
    config = load_extended_config(root)
    files, source_hash = read_sources(root, config)
    model = build_model(root, config, files, source_hash)
    problems = validate_document(model)
    if problems:
        raise CompileError(f"compiled model does not conform to {CONTRACT_NAME} v{CONTRACT_VERSION}:\n  - " + "\n  - ".join(problems))
    return model


def serialized(model: Dict[str, Any]) -> str:
    return json.dumps(model, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: List[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    check = "--check" in argv
    quiet = "--quiet" in argv
    root = Path(args[0] if args else ".").resolve()
    try:
        text = serialized(compile_service(root))
    except CompileError as exc:
        print(f"architecture error: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:  # pragma: no cover - environment, not logic
        print(f"architecture error: the compiler needs tree-sitter and tree-sitter-language-pack ({exc}); pip install -r tools/architecture_compiler/requirements.txt", file=sys.stderr)
        return 1
    target = root / MODEL_PATH
    if check:
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current != text:
            print(f"architecture error: {MODEL_PATH} is stale; run: python3 -m tools.architecture_compiler {root}", file=sys.stderr)
            return 1
        if not quiet:
            print(f"{MODEL_PATH} is current ({CONTRACT_NAME} v{CONTRACT_VERSION})")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if not quiet:
        print(f"wrote {target.relative_to(root).as_posix()}")
    return 0
