#!/usr/bin/env python3
"""Deterministic architecture compiler for Python services.

A service conforms to the `hermes.architecture` contract when it can prove,
mechanically, where its system map came from (the extraction map) and what
defends the model (the gates). A hand-written architecture.json cannot do
either. This compiler reads a Python service's tree plus a small
``architecture/config.json`` and emits a conforming ``architecture/model/model.json``:

* **Components** — every ``.py`` file is assigned to exactly one declared
  component by the first matching pattern; an unassigned file fails the build.
* **Extraction** — the ``ast`` module gives the declaration census (classes,
  functions, methods, with their body ranges); a fixed table of passes cites
  ``(file, line)`` for what each recognises: classes, entrypoints, routes,
  subprocess spawns, file writes, HTTP clients, sockets, threads and tasks,
  environment reads, external imports, enum-typed state machines and
  persistent stores. Whatever the passes do not know does not exist on the map.
* **System map** — one node per module, class, entrypoint, endpoint, store,
  resource and external package the passes found, wired by structure
  (``declares``, ``constructs``, ``exposes``), lifecycle (``persists-to``,
  ``holds``, ``transitions``) and boundary (``uses``) edges. A person may add
  nodes, edges, flows and invariants under ``declared``; those carry the
  ``declared`` authority and must resolve.
* **Gates** — the local commands that must pass before Harness accepts a
  snapshot (at minimum this compiler's own ``--check``) and, when configured,
  the GitHub Actions workflows, wired into one merge gate.

Every construction on the map has provenance (one extraction entity per node),
the output is validated against the vendored contract before it is written, and
``--check`` fails when the committed model drifts from the tree. Output is
byte-deterministic: sorted keys, sorted collections, no timestamps.

Usage:
    python3 tools/architecture_compile_python.py <service-root> [--check] [--quiet]

Config (``<root>/architecture/config.json``):
    {
      "title": "Home Awareness", "description": "…", "repository": "owner/name",
      "source_root": ".", "exclude": ["tests/**"],
      "components": [{"id": "capture", "label": "Capture", "description": "…", "patterns": ["capture/**"]}],
      "layers": [{"id": "core", "label": "Core", "order": 1}],
      "declared": {"nodes": [...], "edges": [...], "flows": [...], "invariants": [...]},
      "gates": {"commands": [{"id": "check", "name": "Model is current",
                              "command": "python3 tools/architecture_compile_python.py . --check"}],
                "workflows": ".github/workflows", "workflow_families": {"tests": "behavior"}}
    }
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import io
import json
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # run as a script from any directory
    sys.path.insert(0, str(REPO_ROOT))

from tui_gateway.architecture_contract import CONTRACT_NAME, CONTRACT_VERSION, validate_document  # noqa: E402

SCHEMA_VERSION = "1.0.0"
CONFIG_PATH = "architecture/config.json"
MODEL_PATH = "architecture/model/model.json"
DEFAULT_EXCLUDES = ("**/__pycache__/**", ".venv/**", "venv/**", "node_modules/**", "build/**", "dist/**", "architecture/**")
EDGE_CLASSES = ("structure", "interplay", "lifecycle", "boundary", "usage")
ROLE_VALUES = ("gate", "post-merge", "manual", "disabled", "local")


class CompileError(RuntimeError):
    """A fail-closed condition: the tree cannot be described by this config."""


# ── The extractor's grammar ──────────────────────────────────────────────────
# Each pass is one rule of what the compiler recognises. A citation is (rule,
# file, line). The family groups rules the way the renderers colour wires.

PASSES: Dict[str, Tuple[str, str]] = {
    "py.module": ("Every analysed module is a construction: the file that declares what the other passes find", "wiring"),
    "py.class": ("A class definition (nested ones included)", "wiring"),
    "py.entrypoint": ("An `if __name__ == \"__main__\":` guard or a module-level `main` function", "trigger"),
    "py.route": ("A route decorator (Flask/FastAPI/aiohttp style: route, get, post, put, delete, patch, websocket, api_route)", "trigger"),
    "py.subprocess": ("A subprocess spawn: subprocess.*, os.system, os.popen, asyncio.create_subprocess_*", "behaviour"),
    "py.file_write": ("A file opened for writing or appending, or Path.write_text/write_bytes", "store"),
    "py.http_client": ("An HTTP client call: requests, httpx, urllib.request, aiohttp.ClientSession, http.client", "boundary"),
    "py.socket": ("A socket or server: socket.*, websockets.*, asyncio.start_server/open_connection", "boundary"),
    "py.thread_or_task": ("Concurrency: threading.Thread, multiprocessing.Process, asyncio.create_task/ensure_future/gather, concurrent.futures", "behaviour"),
    "py.env_read": ("An environment read: os.environ[...], os.environ.get, os.getenv", "boundary"),
    "py.import_external": ("A module-level import of a package outside the standard library and outside this service", "boundary"),
    "py.state_machine": ("An Enum subclass whose members are assigned to an attribute somewhere in the tree: a lifecycle state, and each assignment a transition", "behaviour"),
    "py.persistent_store": ("A persistent store site: sqlite3.connect, json.dump, pickle.dump, shelve.open, dbm.open", "store"),
    "py.boundary.external_signature": ("A configured external-system signature present in the code: a regex matched against comment- and string-masked code, or, string-scoped, against string-literal contents only", "boundary"),
    "declared.node": ("A construction a person declared in architecture/config.json, citing the file and line it lives at (semantic layer, not extraction)", "wiring"),
}
SEMANTIC_PASSES = {"declared.node"}

LIMITATIONS = [
    "Static source evidence: dynamic dispatch, decorators assembled at runtime, getattr-based wiring and plugin registries are invisible to the passes.",
    "External imports are attributed to the classes whose bodies name the imported alias; module-level use is attributed to the module.",
    "A route is recognised by its decorator's attribute name; frameworks that register handlers by call (app.add_url_rule) are not attributed.",
    "Stores and resources are recognised by call site; a wrapper in another file hides the mechanism from the caller.",
    "External systems are attributed per configured signature; a system reached only through an unlisted API or a wrapper in another file is not attributed to the caller.",
    "A declared system whose code-scoped signature matches an imported package absorbs that package: the import is an origin of the system and no separate package node is drawn.",
]

ROUTE_ATTRIBUTES = {"route", "get", "post", "put", "delete", "patch", "websocket", "api_route", "head", "options"}
SUBPROCESS_PREFIXES = ("subprocess.", "os.system", "os.popen", "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell")
HTTP_PREFIXES = ("requests.", "httpx.", "urllib.request.", "aiohttp.ClientSession", "http.client.")
SOCKET_PREFIXES = ("socket.socket", "socket.create_connection", "socket.create_server", "websockets.", "asyncio.start_server", "asyncio.open_connection")
TASK_PREFIXES = ("threading.Thread", "multiprocessing.Process", "asyncio.create_task", "asyncio.ensure_future", "asyncio.gather", "concurrent.futures.")
STORE_CALLS = {"sqlite3.connect": "sqlite", "json.dump": "json", "pickle.dump": "pickle", "shelve.open": "shelve", "dbm.open": "dbm"}
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "enum.Enum", "enum.IntEnum", "enum.StrEnum", "enum.Flag", "enum.IntFlag"}
STDLIB: Set[str] = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


# ── Config ───────────────────────────────────────────────────────────────────


def load_config(root: Path) -> Dict[str, Any]:
    path = root / CONFIG_PATH
    if not path.is_file():
        raise CompileError(f"{CONFIG_PATH} not found under {root}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompileError(f"{CONFIG_PATH} is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise CompileError(f"{CONFIG_PATH} must be a JSON object")
    for key in ("title", "description"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise CompileError(f"{CONFIG_PATH}: {key!r} must be a non-empty string")
    components = config.get("components")
    if not isinstance(components, list) or not components:
        raise CompileError(f"{CONFIG_PATH}: 'components' must be a non-empty list")
    seen: Set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise CompileError(f"{CONFIG_PATH}: every component must be an object")
        for key in ("id", "label", "description"):
            if not isinstance(component.get(key), str) or not component[key].strip():
                raise CompileError(f"{CONFIG_PATH}: component {key!r} must be a non-empty string")
        if component["id"] in seen:
            raise CompileError(f"{CONFIG_PATH}: duplicate component id {component['id']!r}")
        seen.add(component["id"])
        patterns = component.get("patterns")
        if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
            raise CompileError(f"{CONFIG_PATH}: component {component['id']!r} needs a non-empty 'patterns' list")
    gates = config.get("gates")
    if not isinstance(gates, dict):
        raise CompileError(f"{CONFIG_PATH}: 'gates' is required — declare the commands that must pass before a snapshot is accepted")
    commands = gates.get("commands") or []
    if not isinstance(commands, list):
        raise CompileError(f"{CONFIG_PATH}: gates.commands must be a list")
    for command in commands:
        if not isinstance(command, dict) or not all(isinstance(command.get(k), str) and command[k] for k in ("id", "name", "command")):
            raise CompileError(f"{CONFIG_PATH}: every gates.commands entry needs string id, name and command")
        if command.get("role", "local") not in ("gate", "local"):
            raise CompileError(f"{CONFIG_PATH}: gates.commands[{command['id']}].role must be 'gate' or 'local'")
    if not commands and not gates.get("workflows"):
        raise CompileError(f"{CONFIG_PATH}: declare at least one gate (gates.commands) or a workflows directory")
    declared = config.get("declared") or {}
    if not isinstance(declared, dict):
        raise CompileError(f"{CONFIG_PATH}: 'declared' must be an object")
    config["external_systems"] = normalize_external_systems(config.get("external_systems") or [])
    config["external_groups"] = normalize_external_groups(config.get("external_groups") or [])
    return config


def normalize_external_systems(raw: Any) -> List[Dict[str, Any]]:
    """Declared external systems, Portal-shaped: each carries a human description
    (specified authority) and regex signatures, either plain strings (matched
    against comment- and string-masked code) or {pattern, scope} with scope
    "code" or "strings" (string-literal contents only, for hostnames and paths)."""
    if not isinstance(raw, list):
        raise CompileError(f"{CONFIG_PATH}: 'external_systems' must be a list")
    systems: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise CompileError(f"{CONFIG_PATH}: every external system must be an object")
        for key in ("id", "label", "category", "description"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise CompileError(f"{CONFIG_PATH}: external system {key!r} must be a non-empty string ({item.get('id')!r})")
        if item["id"] in seen:
            raise CompileError(f"{CONFIG_PATH}: duplicate external system id {item['id']!r}")
        seen.add(item["id"])
        signatures = item.get("signatures")
        if not isinstance(signatures, list) or not signatures:
            raise CompileError(f"{CONFIG_PATH}: external system {item['id']!r} needs a non-empty 'signatures' list")
        compiled: List[Tuple[str, str, Any]] = []
        for signature in signatures:
            if isinstance(signature, str):
                pattern, scope = signature, "code"
            elif isinstance(signature, dict) and isinstance(signature.get("pattern"), str):
                pattern, scope = signature["pattern"], str(signature.get("scope") or "code")
            else:
                raise CompileError(f"{CONFIG_PATH}: external system {item['id']!r} has a malformed signature {signature!r}")
            if scope not in ("code", "strings"):
                raise CompileError(f"{CONFIG_PATH}: external system {item['id']!r} signature scope must be 'code' or 'strings'")
            try:
                compiled.append((pattern, scope, re.compile(pattern)))
            except re.error as exc:
                raise CompileError(f"{CONFIG_PATH}: external system {item['id']!r} signature {pattern!r} is not a valid regex: {exc}") from exc
        systems.append({"id": item["id"], "label": item["label"], "category": item["category"], "description": item["description"],
                        "protocol": str(item["protocol"]) if item.get("protocol") else None, "signatures": compiled})
    return systems


def normalize_external_groups(raw: Any) -> List[Dict[str, Any]]:
    """Boundary groups (Portal's boundary_groups): a category belongs to at most
    one group, and every group must end up with a member."""
    if not isinstance(raw, list):
        raise CompileError(f"{CONFIG_PATH}: 'external_groups' must be a list")
    groups: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    owner_of_category: Dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise CompileError(f"{CONFIG_PATH}: every external group must be an object")
        for key in ("id", "label"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise CompileError(f"{CONFIG_PATH}: external group {key!r} must be a non-empty string ({item.get('id')!r})")
        if item["id"] in seen_ids:
            raise CompileError(f"{CONFIG_PATH}: duplicate external group id {item['id']!r}")
        seen_ids.add(item["id"])
        categories = item.get("categories")
        if not isinstance(categories, list) or not categories or not all(isinstance(c, str) and c for c in categories):
            raise CompileError(f"{CONFIG_PATH}: external group {item['id']!r} needs a non-empty 'categories' list")
        for category in categories:
            if category in owner_of_category:
                raise CompileError(f"{CONFIG_PATH}: category {category!r} belongs to both {owner_of_category[category]!r} and {item['id']!r}")
            owner_of_category[category] = item["id"]
        groups.append({"id": item["id"], "label": item["label"], "description": str(item.get("description") or ""), "categories": sorted(categories)})
    return groups


def mask_source(text: str) -> Tuple[str, List[Tuple[int, str]]]:
    """The text with comments and string-literal contents blanked (offsets and
    newlines preserved, so line numbers survive) plus every string literal's
    contents with its starting line. Falls back to the raw text when the file
    does not tokenize."""
    chars = list(text)
    line_starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            line_starts.append(index + 1)

    def offset(row: int, col: int) -> int:
        return line_starts[row - 1] + col if 0 < row <= len(line_starts) else len(text)

    def blank(start: int, end: int) -> None:
        for index in range(start, min(end, len(chars))):
            if chars[index] != "\n":
                chars[index] = " "

    strings: List[Tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                blank(offset(*token.start), offset(*token.end))
            elif token.type == tokenize.STRING:
                body = token.string
                quote_at = min((i for i, ch in enumerate(body) if ch in "\"'"), default=None)
                if quote_at is None:
                    continue
                quote = body[quote_at:quote_at + 3] if body[quote_at:quote_at + 3] in ('"""', "'''") else body[quote_at]
                content = body[quote_at + len(quote):len(body) - len(quote)]
                start = offset(*token.start) + quote_at + len(quote)
                blank(start, start + len(content))
                strings.append((token.start[0], content))
            elif token.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                blank(offset(*token.start), offset(*token.end))
                strings.append((token.start[0], token.string))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return text, []
    return "".join(chars), strings


# ── Sources ──────────────────────────────────────────────────────────────────


def _matches(rel: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(rel, pattern):
            return True
        # `dir/**` also means the directory's own direct children.
        if pattern.endswith("/**") and rel.startswith(pattern[:-2]):
            return True
    return False


def read_sources(root: Path, config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    source_root = (root / str(config.get("source_root") or ".")).resolve()
    if not source_root.is_dir():
        raise CompileError(f"source_root {source_root} is not a directory")
    excludes = tuple(DEFAULT_EXCLUDES) + tuple(str(p) for p in config.get("exclude") or [])
    files: List[Dict[str, Any]] = []
    digest = hashlib.sha256()
    unassigned: List[str] = []
    for path in sorted(p for p in source_root.rglob("*.py") if not any(part.startswith(".") for part in p.relative_to(source_root).parts)):
        rel = path.relative_to(source_root).as_posix()
        if _matches(rel, excludes):
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            raise CompileError(f"{rel}: cannot parse ({exc.msg} at line {exc.lineno})") from exc
        component = next((str(c["id"]) for c in config["components"] if _matches(rel, c["patterns"])), None)
        if component is None:
            unassigned.append(rel)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
        files.append({"rel": rel, "path": rel, "text": text, "tree": tree, "component": component, "line_count": len(text.splitlines())})
    if not files:
        raise CompileError(f"no Python files under {source_root}")
    if unassigned:
        raise CompileError("every file must belong to one component; unassigned: " + ", ".join(unassigned))
    return files, digest.hexdigest()


def local_packages(files: List[Dict[str, Any]]) -> Set[str]:
    names: Set[str] = set()
    for record in files:
        first = record["rel"].split("/", 1)[0]
        names.add(first[:-3] if first.endswith(".py") else first)
    return names


# ── Per-file extraction ──────────────────────────────────────────────────────


def dotted(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return None


class FileExtraction:
    """Everything the passes see in one file."""

    def __init__(self, record: Dict[str, Any], locals_: Set[str]) -> None:
        self.rel: str = record["rel"]
        self.component: str = record["component"]
        self.line_count: int = record["line_count"]
        self.masked_code, self.strings = mask_source(record["text"])
        self.tree: ast.Module = record["tree"]
        self.locals = locals_
        self.declarations: List[Dict[str, Any]] = []
        self.citations: List[Tuple[str, int]] = []
        self.classes: List[Dict[str, Any]] = []      # {name, qualname, line, end_line, bases, is_enum}
        self.imports: List[Dict[str, Any]] = []      # {root, alias, line}
        self.alias_to_module: Dict[str, str] = {}
        self.entrypoint_line: Optional[int] = None
        self.routes: List[Dict[str, Any]] = []       # {line, label, owner}
        self.stores: List[Dict[str, Any]] = []       # {line, label, mechanism}
        self.resources: List[Dict[str, Any]] = []    # {line, label, sub_kind}
        self.enum_assignments: List[Tuple[str, int]] = []  # (enum name, line)
        self.constructed_in_main: List[Tuple[str, int]] = []  # (class name, line)
        self.external_refs_by_owner: Dict[str, Set[str]] = {}  # owner qualname or "" (module) → external roots
        self.run()

    # -- helpers --------------------------------------------------------------

    def cite(self, rule: str, line: int) -> None:
        self.citations.append((rule, line))

    def normalized(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        head, _, tail = name.partition(".")
        module = self.alias_to_module.get(head)
        if module is None:
            return name
        return f"{module}.{tail}" if tail else module

    def enclosing_class(self, line: int) -> Optional[str]:
        best: Optional[Dict[str, Any]] = None
        for cls in self.classes:
            if cls["line"] <= line <= cls["end_line"] and (best is None or cls["line"] > best["line"]):
                best = cls
        return best["qualname"] if best else None

    # -- passes ---------------------------------------------------------------

    def run(self) -> None:
        self.cite("py.module", 1)
        self.collect_imports()
        self.collect_declarations(self.tree, prefix="")
        self.collect_entrypoint()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self.inspect_call(node)
            elif isinstance(node, ast.Subscript):
                name = self.normalized(dotted(node.value))
                if name == "os.environ":
                    self.cite("py.env_read", node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.inspect_decorators(node)
            elif isinstance(node, ast.Assign):
                self.inspect_assignment(node)
        self.collect_external_references()

    def collect_imports(self) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    self.alias_to_module[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else root
                    self.record_import(root, alias.asname or root, node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                root = node.module.split(".")[0]
                for alias in node.names:
                    self.alias_to_module[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                    self.record_import(root, alias.asname or alias.name, node.lineno)

    def record_import(self, root: str, alias: str, line: int) -> None:
        external = root not in STDLIB and root not in self.locals
        self.imports.append({"root": root, "alias": alias, "line": line, "external": external})
        if external:
            self.cite("py.import_external", line)

    def collect_declarations(self, parent: ast.AST, prefix: str) -> None:
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, ast.ClassDef):
                qualname = f"{prefix}{node.name}"
                bases = [dotted(b) or "" for b in node.bases]
                is_enum = any(self.normalized(b) in ENUM_BASES or b in ENUM_BASES for b in bases)
                self.classes.append({"name": node.name, "qualname": qualname, "line": node.lineno, "end_line": node.end_lineno or node.lineno, "is_enum": is_enum})
                self.declarations.append({"kind": "class", "name": qualname, "line": node.lineno, "end_line": node.end_lineno or node.lineno})
                self.cite("py.class", node.lineno)
                self.collect_declarations(node, prefix=f"{qualname}.")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{node.name}"
                self.declarations.append({"kind": "func", "name": qualname, "line": node.lineno, "end_line": node.end_lineno or node.lineno})
                self.collect_declarations(node, prefix=f"{qualname}.")
            else:
                self.collect_declarations(node, prefix=prefix)

    def collect_entrypoint(self) -> None:
        main_body: List[ast.stmt] = []
        for node in self.tree.body:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                left = node.test.left
                comparators = node.test.comparators
                if isinstance(left, ast.Name) and left.id == "__name__" and comparators and isinstance(comparators[0], ast.Constant) and comparators[0].value == "__main__":
                    self.entrypoint_line = self.entrypoint_line or node.lineno
                    self.cite("py.entrypoint", node.lineno)
                    main_body.extend(node.body)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
                self.entrypoint_line = self.entrypoint_line or node.lineno
                self.cite("py.entrypoint", node.lineno)
                main_body.extend(node.body)
        for statement in main_body:
            for node in ast.walk(statement):
                if isinstance(node, ast.Call):
                    name = dotted(node.func)
                    if name and name[:1].isupper():
                        self.constructed_in_main.append((name.split(".")[-1], node.lineno))

    def inspect_decorators(self, node: ast.AST) -> None:
        for decorator in getattr(node, "decorator_list", []):
            name = dotted(decorator)
            if not name:
                continue
            attribute = name.split(".")[-1]
            if attribute in ROUTE_ATTRIBUTES and isinstance(decorator, ast.Call) and "." in name:
                label = f"{attribute} {node.name}"  # type: ignore[attr-defined]
                if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                    label = f"{attribute.upper()} {decorator.args[0].value}"
                self.cite("py.route", decorator.lineno)
                self.routes.append({"line": decorator.lineno, "label": label, "owner": self.enclosing_class(decorator.lineno)})

    def inspect_call(self, node: ast.Call) -> None:
        name = self.normalized(dotted(node.func))
        if not name:
            return
        line = node.lineno
        if name.startswith(SUBPROCESS_PREFIXES):
            self.cite("py.subprocess", line)
        if name.startswith(HTTP_PREFIXES):
            self.cite("py.http_client", line)
            self.resources.append({"line": line, "label": name, "sub_kind": "http_client"})
        if name.startswith(SOCKET_PREFIXES):
            self.cite("py.socket", line)
            self.resources.append({"line": line, "label": name, "sub_kind": "socket"})
        if name.startswith(TASK_PREFIXES):
            self.cite("py.thread_or_task", line)
        if name in ("os.environ.get", "os.getenv"):
            self.cite("py.env_read", line)
        if name in STORE_CALLS:
            self.cite("py.persistent_store", line)
            self.stores.append({"line": line, "label": name, "mechanism": STORE_CALLS[name]})
        if name == "open" and self.opens_for_writing(node):
            self.cite("py.file_write", line)
        if name.endswith((".write_text", ".write_bytes")):
            self.cite("py.file_write", line)

    @staticmethod
    def opens_for_writing(node: ast.Call) -> bool:
        mode: Optional[str] = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            mode = node.args[1].value
        for keyword in node.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                mode = keyword.value.value
        return bool(mode) and any(flag in mode for flag in "wa+x")  # type: ignore[union-attr]

    def inspect_assignment(self, node: ast.Assign) -> None:
        value = dotted(node.value)
        if not value or "." not in value:
            return
        enum_name = value.rsplit(".", 1)[0].split(".")[-1]
        if any(isinstance(t, ast.Attribute) for t in node.targets):
            self.enum_assignments.append((enum_name, node.lineno))

    def collect_external_references(self) -> None:
        aliases = {imp["alias"]: imp["root"] for imp in self.imports if imp["external"]}
        if not aliases:
            return
        by_owner: Dict[str, Set[str]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and node.id in aliases:
                owner = self.enclosing_class(node.lineno) or ""
                by_owner.setdefault(owner, set()).add(aliases[node.id])
        self.external_refs_by_owner = by_owner


# ── The map ──────────────────────────────────────────────────────────────────


def module_id(component: str, rel: str) -> str:
    return f"module:{component}:{rel}"


def class_id(component: str, qualname: str) -> str:
    return f"class:{component}:{qualname}"


def build_model(root: Path, config: Dict[str, Any], files: List[Dict[str, Any]], source_hash: str) -> Dict[str, Any]:
    locals_ = local_packages(files)
    extractions = [FileExtraction(record, locals_) for record in files]
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Set[Tuple[str, str, str, str]] = set()
    origins: Dict[str, List[Tuple[str, int, str]]] = {}  # node id → (path, line, rule)
    enum_nodes_by_name: Dict[str, str] = {}
    class_nodes_by_name: Dict[str, str] = {}

    def add_node(node_id: str, **fields: Any) -> None:
        if node_id in nodes:
            return
        nodes[node_id] = {"id": node_id, "history_key": node_id, **fields}

    def add_origin(node_id: str, path: str, line: int, rule: str) -> None:
        origins.setdefault(node_id, []).append((path, line, rule))

    def add_edge(source: str, target: str, relation: str, edge_class: str) -> None:
        assert edge_class in EDGE_CLASSES, edge_class
        if source != target:
            edges.add((source, target, relation, edge_class))

    for ex in extractions:
        mid = module_id(ex.component, ex.rel)
        add_node(mid, kind="module", sub_kind="file", label=ex.rel, component=ex.component, path=ex.rel, line=1, owner_type=None)
        add_origin(mid, ex.rel, 1, "py.module")
        for cls in ex.classes:
            cid = class_id(ex.component, cls["qualname"])
            add_node(cid, kind="class", sub_kind="state_machine" if cls["is_enum"] else "class", label=cls["qualname"],
                     component=ex.component, path=ex.rel, line=cls["line"], owner_type=ex.rel)
            add_origin(cid, ex.rel, cls["line"], "py.class")
            add_edge(mid, cid, "declares", "structure")
            class_nodes_by_name.setdefault(cls["name"], cid)
            if cls["is_enum"]:
                enum_nodes_by_name.setdefault(cls["name"], cid)
        if ex.entrypoint_line is not None:
            eid = f"entrypoint:{ex.component}:{ex.rel}"
            add_node(eid, kind="entrypoint", sub_kind="main", label=f"main · {ex.rel}", component=ex.component, path=ex.rel, line=ex.entrypoint_line, owner_type=ex.rel)
            add_origin(eid, ex.rel, ex.entrypoint_line, "py.entrypoint")
            add_edge(mid, eid, "declares", "structure")

    def owner_of(ex: FileExtraction, line: int) -> str:
        qualname = ex.enclosing_class(line)
        return class_id(ex.component, qualname) if qualname else module_id(ex.component, ex.rel)

    # Declared external systems: every signature hit is a citation, every system
    # must be observed at least once, and a code-scoped signature that matches an
    # imported package absorbs that package (the import becomes an origin of the
    # system; no separate package node is drawn).
    systems = config.get("external_systems") or []
    system_hits: Dict[str, List[Tuple[str, int, str]]] = {system["id"]: [] for system in systems}
    absorbed_roots: Set[str] = set()
    for system in systems:
        for ex in extractions:
            seen_lines: Set[int] = set()
            for _, scope, regex in system["signatures"]:
                if scope == "code":
                    for match in regex.finditer(ex.masked_code):
                        seen_lines.add(ex.masked_code.count("\n", 0, match.start()) + 1)
                else:
                    for line, content in ex.strings:
                        if regex.search(content):
                            seen_lines.add(line)
            for line in sorted(seen_lines):
                ex.cite("py.boundary.external_signature", line)
                system_hits[system["id"]].append((ex.rel, line, "py.boundary.external_signature"))
            for imp in ex.imports:
                if imp["external"] and any(scope == "code" and regex.search(imp["root"]) for _, scope, regex in system["signatures"]):
                    absorbed_roots.add(imp["root"])
                    system_hits[system["id"]].append((ex.rel, imp["line"], "py.import_external"))
        if not system_hits[system["id"]]:
            raise CompileError(f"external system {system['id']!r} is declared but none of its signatures matches the tree (a declaration must be observed)")

    by_rel = {ex.rel: ex for ex in extractions}
    for ex in extractions:
        for imp in ex.imports:
            if not imp["external"] or imp["root"] in absorbed_roots:
                continue
            xid = f"external:{imp['root']}"
            add_node(xid, kind="external", sub_kind="package", label=imp["root"], component=None, path=ex.rel, line=imp["line"],
                     owner_type="External packages", category="package")
            add_origin(xid, ex.rel, imp["line"], "py.import_external")
        for owner, roots in ex.external_refs_by_owner.items():
            owner_id = class_id(ex.component, owner) if owner else module_id(ex.component, ex.rel)
            for root_name in roots:
                if root_name not in absorbed_roots:
                    add_edge(owner_id, f"external:{root_name}", "uses", "boundary")
    externals_systems: List[Dict[str, Any]] = []
    for system in systems:
        hits = sorted(set(system_hits[system["id"]]))
        xid = f"external:{system['id']}"
        first_path, first_line, _ = hits[0]
        add_node(xid, kind="external", sub_kind="system", label=system["label"], component=None, path=first_path, line=first_line,
                 owner_type="External systems", category=system["category"], description=system["description"], protocol=system["protocol"])
        components_hit: Counter = Counter()
        for path, line, rule in hits:
            add_origin(xid, path, line, rule)
            ex = by_rel[path]
            components_hit[ex.component] += 1
            add_edge(owner_of(ex, line), xid, "uses", "boundary")
        paths = sorted({path for path, _, _ in hits})
        top = sorted(components_hit.items(), key=lambda item: (-item[1], item[0]))
        externals_systems.append({
            "id": system["id"], "label": system["label"], "category": system["category"], "description": system["description"],
            "protocol": system["protocol"], "hit_count": len(hits), "file_count": len(paths), "paths": paths,
            "component": top[0][0] if top else None, "component_ids": sorted(components_hit),
            "signatures": [{"pattern": pattern, "scope": scope} for pattern, scope, _ in system["signatures"]],
            "authority": "observed", "description_authority": "specified", "evidence_class": "static_source",
        })
    for ex in extractions:
        for store in ex.stores:
            sid = f"store:{ex.component}:{ex.rel}:{store['line']}"
            add_node(sid, kind="store", sub_kind=store["mechanism"], label=store["label"], component=ex.component, path=ex.rel, line=store["line"], owner_type=ex.enclosing_class(store["line"]))
            add_origin(sid, ex.rel, store["line"], "py.persistent_store")
            add_edge(owner_of(ex, store["line"]), sid, "persists-to", "lifecycle")
        for resource in ex.resources:
            rid = f"resource:{ex.component}:{ex.rel}:{resource['line']}"
            add_node(rid, kind="resource", sub_kind=resource["sub_kind"], label=resource["label"], component=ex.component, path=ex.rel, line=resource["line"], owner_type=ex.enclosing_class(resource["line"]))
            add_origin(rid, ex.rel, resource["line"], "py.http_client" if resource["sub_kind"] == "http_client" else "py.socket")
            add_edge(owner_of(ex, resource["line"]), rid, "holds", "lifecycle")
        for route in ex.routes:
            pid = f"endpoint:{ex.component}:{ex.rel}:{route['line']}"
            add_node(pid, kind="endpoint", sub_kind="route", label=route["label"], component=ex.component, path=ex.rel, line=route["line"], owner_type=route["owner"])
            add_origin(pid, ex.rel, route["line"], "py.route")
            add_edge(owner_of(ex, route["line"]), pid, "exposes", "structure")
        for class_name, line in ex.constructed_in_main:
            target = class_nodes_by_name.get(class_name)
            if target and ex.entrypoint_line is not None:
                add_edge(f"entrypoint:{ex.component}:{ex.rel}", target, "constructs", "structure")
    # State machines: an enum assigned to an attribute anywhere in the tree.
    for ex in extractions:
        for enum_name, line in ex.enum_assignments:
            target = enum_nodes_by_name.get(enum_name)
            if not target:
                continue
            ex.cite("py.state_machine", line)
            add_origin(target, ex.rel, line, "py.state_machine")
            add_edge(owner_of(ex, line), target, "transitions", "lifecycle")

    # Declared additions: a person's assertions, resolving against what exists.
    declared = config.get("declared") or {}
    analysed = {ex.rel: ex for ex in extractions}
    semantic_by_file: Dict[str, int] = {}
    for item in declared.get("nodes") or []:
        for key in ("id", "kind", "label", "path"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise CompileError(f"declared node needs string {key!r}: {item}")
        if item["path"] not in analysed or not isinstance(item.get("line"), int) or item["line"] < 1:
            raise CompileError(f"declared node {item['id']!r} must cite an analysed file and a line ≥ 1")
        if item["id"] in nodes:
            raise CompileError(f"declared node {item['id']!r} collides with an extracted construction")
        component = item.get("component")
        if component is not None and component not in {c["id"] for c in config["components"]}:
            raise CompileError(f"declared node {item['id']!r} names an undeclared component {component!r}")
        add_node(item["id"], kind=item["kind"], sub_kind="declared", label=item["label"], component=component, path=item["path"], line=item["line"], owner_type=None, authority="declared")
        add_origin(item["id"], item["path"], item["line"], "declared.node")
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

    # Invariants: what the compiler proved, plus what a person declared unchecked.
    boundary_groups: List[Dict[str, Any]] = []
    for group in config.get("external_groups") or []:
        members = sorted(n["id"] for n in node_list if n["kind"] == "external" and n.get("category") in group["categories"])
        if not members:
            raise CompileError(f"external group {group['id']!r} has no member: no external system or package carries a category in {group['categories']}")
        boundary_groups.append({**group, "members": members})

    invariants = [
        {"id": "externals-declared-and-observed", "kind": "externals_declared_and_observed", "status": "holds", "checked": len(systems),
         "why": "Every declared external system is observed by at least one signature hit; a declaration nothing matches fails the build."},
        {"id": "components-assign-every-file", "kind": "components_assign_every_file", "status": "holds", "checked": len(files),
         "why": "Every Python file belongs to exactly one declared component; an unassigned file fails the build."},
        {"id": "entities-have-origin", "kind": "entities_have_origin", "status": "holds", "checked": len(node_list),
         "why": "Every construction on the map was extracted from a cited file and line, or declared by a person citing one."},
        {"id": "externals-are-imports", "kind": "externals_are_imports", "status": "holds", "checked": sum(1 for n in node_list if n["kind"] == "external"),
         "why": "Every external package on the map comes from a module-level import outside the standard library and this service."},
    ]
    for item in declared.get("invariants") or []:
        for key in ("id", "kind", "why"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise CompileError(f"declared invariant needs string {key!r}: {item}")
        invariants.append({"id": item["id"], "kind": item["kind"], "status": "unchecked", "checked": 0, "why": item["why"]})
    ids = [i["id"] for i in invariants]
    if len(ids) != len(set(ids)):
        raise CompileError("duplicate invariant id")
    invariants.sort(key=lambda i: i["id"])

    ci = build_ci(root, config)
    invariants.append({"id": "gates-declared", "kind": "gates_declared", "status": "holds", "checked": len(ci["merge"]["inputs"]),
                       "why": "At least one gate must pass before Harness accepts a snapshot of this model."})
    invariants.sort(key=lambda i: i["id"])

    extraction = build_extraction(extractions, node_list, origins, semantic_by_file)
    components = []
    for configured in config["components"]:
        owned = [ex for ex in extractions if ex.component == configured["id"]]
        declared_names = sorted({d["name"] for ex in owned for d in ex.declarations if d["kind"] == "class"})
        components.append({
            "id": configured["id"], "label": configured["label"], "description": configured["description"],
            "layer": str(configured.get("layer") or ""), "external": False,
            "files": [ex.rel for ex in owned], "declarations": declared_names,
            "file_count": len(owned), "line_count": sum(f["line_count"] for f in files if f["component"] == configured["id"]),
            "declaration_count": len(declared_names),
        })
    layers = [{"id": str(l["id"]), "label": str(l["label"]), "order": int(l["order"])} for l in config.get("layers") or []]

    model: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract": {"name": CONTRACT_NAME, "version": CONTRACT_VERSION},
        "title": config["title"],
        "description": config["description"],
        "repository": config.get("repository"),
        "source_tree_sha256": source_hash,
        "compiler": "tools/architecture_compile_python.py",
        "components": components,
        "layers": layers,
        "interplay": {"nodes": node_list, "edges": edge_list, "flows": flows, "invariants": invariants, "pages": [], "boundary_groups": boundary_groups, "clusters": []},
        "externals": {"systems": externals_systems,
                      "groups": [{k: g[k] for k in ("id", "label", "description", "categories")} for g in boundary_groups],
                      "edges": []},
        "extraction": extraction,
        "ci": ci,
        "inventory": {"files": len(files), "lines": sum(f["line_count"] for f in files), "declarations": extraction["summary"]["declarations"],
                      "python_files": len(files), "python_lines": sum(f["line_count"] for f in files)},
        "evidence_metadata": {"class": "static_source", "rules": {k: v[0] for k, v in sorted(PASSES.items())}, "limitations": list(LIMITATIONS)},
    }
    return model


def build_extraction(extractions: List[FileExtraction], node_list: List[Dict[str, Any]], origins: Dict[str, List[Tuple[str, int, str]]],
                     semantic_by_file: Dict[str, int]) -> Dict[str, Any]:
    pass_files: Dict[str, Set[str]] = {}
    pass_citations: Dict[str, int] = {}
    files: List[Dict[str, Any]] = []
    by_kind: Dict[str, Dict[str, int]] = {}
    for ex in sorted(extractions, key=lambda e: e.rel):
        mechanical = sorted(set(ex.citations))
        for rule, _ in mechanical:
            pass_files.setdefault(rule, set()).add(ex.rel)
            pass_citations[rule] = pass_citations.get(rule, 0) + 1
        declarations = []
        for declaration in sorted(ex.declarations, key=lambda d: (d["line"], -d["end_line"], d["name"])):
            rules = sorted({rule for rule, line in mechanical if declaration["line"] <= line <= declaration["end_line"]})
            bucket = by_kind.setdefault(declaration["kind"], {"total": 0, "mapped": 0})
            bucket["total"] += 1
            bucket["mapped"] += 1 if rules else 0
            declarations.append({"kind": declaration["kind"], "name": declaration["name"], "line": declaration["line"], "passes": rules})
        semantic = semantic_by_file.get(ex.rel, 0)
        if semantic:
            pass_files.setdefault("declared.node", set()).add(ex.rel)
            pass_citations["declared.node"] = pass_citations.get("declared.node", 0) + semantic
        files.append({
            "path": ex.rel, "component": ex.component,
            "line_count": ex.line_count,
            "passes": sorted({rule for rule, _ in mechanical}), "citations": len(mechanical), "semantic_citations": semantic,
            "declaration_count": len(declarations), "mapped_declarations": sum(1 for d in declarations if d["passes"]),
            "touched": bool(mechanical), "declarations": declarations,
        })
    passes = [
        {"id": rule, "class": "semantic" if rule in SEMANTIC_PASSES else "mechanical", "description": PASSES[rule][0],
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
            node_origins.append({"path": path, "line": line, "rule": rule, "family": PASSES[rule][1]})
        entities.append({"id": node["id"], "kind": node["kind"], "label": node["label"], "component": node.get("component"), "origins": node_origins})
    return {
        "authority": "observed",
        "derivation": "Every (pass, file, line) the compiler cited, joined to the declarations of the analysed tree by body range; "
                      "one entity per construction on the map with the citations that produced it. Declared constructions are the semantic class.",
        "families": {"behaviour": "Concurrency, lifecycle and state", "boundary": "External packages, HTTP, sockets, environment",
                     "store": "Persistent stores and file writes", "trigger": "Entrypoints and routes", "wiring": "Modules, classes and declared constructions"},
        "files": files,
        "passes": passes,
        "entities": entities,
        "summary": {
            "files": len(files), "touched_files": sum(1 for f in files if f["touched"]), "untouched_files": sum(1 for f in files if not f["touched"]),
            "declarations": sum(f["declaration_count"] for f in files), "mapped_declarations": sum(f["mapped_declarations"] for f in files),
            "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
            "types": dict(by_kind.get("class", {"total": 0, "mapped": 0})), "functions": dict(by_kind.get("func", {"total": 0, "mapped": 0})),
            "citations": sum(f["citations"] for f in files), "semantic_citations": sum(f["semantic_citations"] for f in files),
            "passes": sum(1 for p in passes if p["class"] == "mechanical"),
            "entities": len(entities), "entities_with_origin": sum(1 for e in entities if e["origins"]),
        },
    }


# ── Gates ────────────────────────────────────────────────────────────────────


def build_ci(root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    gates = config["gates"]
    jobs: List[Dict[str, Any]] = []
    workflows: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    triggers: List[Dict[str, Any]] = []
    commands = gates.get("commands") or []
    if commands:
        job_ids = []
        for command in commands:
            job_id = f"local/{command['id']}"
            job_ids.append(job_id)
            jobs.append({
                "id": job_id, "key": command["id"], "name": command["name"], "workflow": "local", "family": "posture",
                "role": command.get("role", "local"), "needs": [], "runs_on": "the service's host", "condition": None,
                "steps": [{"name": command["name"], "command": command["command"], "line": 0}], "step_count": 1,
                "scripts": [], "artifacts_in": [], "artifacts_out": [], "pins": [],
                "evidence": {"path": CONFIG_PATH, "line": 1},
            })
        workflows.append({"id": "local", "label": "Local gates", "name": "Local gates", "family": "posture", "jobs": job_ids,
                          "events": ["snapshot"], "path": CONFIG_PATH,
                          "question": "Which commands must pass on the service's own host before Harness accepts a snapshot of this model?"})
        triggers.append({"id": "trigger:snapshot", "event": "snapshot", "workflows": ["local"]})
        for job_id in job_ids:
            edges.append({"source": "trigger:snapshot", "target": job_id, "kind": "trigger", "label": "snapshot"})
    workflows_dir = gates.get("workflows")
    if workflows_dir:
        gh_workflows, gh_jobs, gh_triggers, gh_edges = read_github_workflows(root, str(workflows_dir), gates.get("workflow_families") or {})
        workflows.extend(gh_workflows)
        jobs.extend(gh_jobs)
        triggers.extend(gh_triggers)
        edges.extend(gh_edges)
    jobs.sort(key=lambda j: j["id"])
    workflows.sort(key=lambda w: w["id"])
    merge_inputs = sorted(j["id"] for j in jobs if j["role"] in ("gate", "local"))
    if not merge_inputs:
        raise CompileError("no gate defends this model: declare gates.commands or a workflow that runs on pull requests")
    for job_id in merge_inputs:
        edges.append({"source": job_id, "target": "merge:snapshot", "kind": "gates", "label": "must pass"})
    edges.sort(key=lambda e: (e["source"], e["target"], e["kind"]))
    check_job = next((j["id"] for j in jobs if any("architecture_compile_python" in s.get("command", "") and "--check" in s.get("command", "") for s in j["steps"])), None)
    static_checks = []
    if check_job:
        static_checks.append({"name": "Architecture model is current", "command": "python3 tools/architecture_compile_python.py . --check",
                              "job": check_job, "scripts": ["tools/architecture_compile_python.py"], "evidence": {"path": CONFIG_PATH, "line": 1}})
    families = {"posture": {"question": "Which checks must pass on the service's host before a snapshot is accepted?"}}
    for workflow in workflows:
        families.setdefault(workflow["family"], {"question": f"What do the {workflow['family']} workflows defend?"})
    return {
        "workflows": workflows, "jobs": jobs, "edges": edges, "triggers": sorted(triggers, key=lambda t: t["id"]),
        "merge": {"id": "merge:snapshot", "label": "Snapshot accepted", "inputs": merge_inputs},
        "ratchets": [], "static_checks": static_checks, "families": dict(sorted(families.items())),
        "architectural": {"invariants": [], "lint_rules": [], "tests": [], "runs_in": []},
        "limitations": ["A gate here is a command or job the config or a workflow declares; the compiler does not run it, Harness does (architecture.check) and refuses a snapshot when it fails.",
                        "GitHub jobs are read from workflow files; branch protection is not read, so a gate is a job that runs on pull requests, not a proof GitHub requires it."],
        "summary": {"workflows": len(workflows), "jobs": len(jobs), "gates": len(merge_inputs), "ratchets": 0, "static_checks": len(static_checks)},
    }


def read_github_workflows(root: Path, directory: str, families: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    folder = root / directory
    if not folder.is_dir():
        raise CompileError(f"gates.workflows {directory!r} is not a directory")
    workflows: List[Dict[str, Any]] = []
    jobs: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    by_event: Dict[str, List[str]] = {}
    for path in sorted(list(folder.glob("*.yml")) + list(folder.glob("*.yaml"))):
        try:
            document = parse_yaml_subset(path.read_text(encoding="utf-8"))
        except YamlSubsetError as exc:
            raise CompileError(f"{path.relative_to(root).as_posix()}: {exc}") from exc
        if not isinstance(document, dict):
            continue
        stem = path.stem
        on = document.get("on") if "on" in document else document.get(True)
        events = sorted(on.keys()) if isinstance(on, dict) else ([str(on)] if isinstance(on, str) else [str(e) for e in on or []])
        raw_jobs = document.get("jobs") if isinstance(document.get("jobs"), dict) else {}
        job_ids = []
        for key in sorted(raw_jobs):
            spec = raw_jobs[key] if isinstance(raw_jobs[key], dict) else {}
            job_id = f"{stem}/{key}"
            job_ids.append(job_id)
            needs_raw = spec.get("needs")
            needs = [f"{stem}/{n}" for n in (needs_raw if isinstance(needs_raw, list) else [needs_raw] if isinstance(needs_raw, str) else [])]
            condition = spec.get("if")
            if isinstance(condition, str) and condition.strip().lower() in ("false", "${{ false }}"):
                role = "disabled"
            elif "pull_request" in events and not (isinstance(condition, str) and "pull_request" in condition and "!=" in condition):
                role = "gate"
            elif "push" in events or "schedule" in events:
                role = "post-merge"
            else:
                role = "manual"
            steps = []
            for index, step in enumerate(spec.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                entry: Dict[str, Any] = {"name": str(step.get("name") or step.get("uses") or f"step {index + 1}"), "line": 0}
                if isinstance(step.get("run"), str):
                    entry["command"] = step["run"].strip()
                if isinstance(step.get("uses"), str):
                    entry["uses"] = step["uses"]
                steps.append(entry)
            jobs.append({
                "id": job_id, "key": key, "name": str(spec.get("name") or key), "workflow": stem, "family": families.get(stem, "behavior"),
                "role": role, "needs": needs, "runs_on": str(spec.get("runs-on") or ""), "condition": condition if isinstance(condition, str) else None,
                "steps": steps, "step_count": len(steps), "scripts": [], "artifacts_in": [], "artifacts_out": [], "pins": [],
                "evidence": {"path": path.relative_to(root).as_posix(), "line": max(1, raw_jobs.lines.get(key, 1) if isinstance(raw_jobs, YamlMap) else 1)},
            })
            for need in needs:
                edges.append({"source": need, "target": job_id, "kind": "needs", "label": "needs"})
        workflows.append({"id": stem, "label": str(document.get("name") or stem), "name": str(document.get("name") or stem), "family": families.get(stem, "behavior"),
                          "jobs": job_ids, "events": events, "path": path.relative_to(root).as_posix(),
                          "question": f"What does the {stem} workflow defend?"})
        for event in events:
            by_event.setdefault(event, []).append(stem)
        for job in jobs:
            if job["workflow"] == stem and not job["needs"]:
                for event in events:
                    edges.append({"source": f"trigger:{event}", "target": job["id"], "kind": "trigger", "label": event})
    triggers = [{"id": f"trigger:{event}", "event": event, "workflows": sorted(stems)} for event, stems in sorted(by_event.items())]
    return workflows, jobs, triggers, edges


# ── YAML subset (ported from Portal's architecture compiler) ─────────────────


class YamlMap(dict):
    """A mapping that remembers the 1-based line each key was declared on."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: Dict[str, int] = {}


class YamlSubsetError(CompileError):
    pass


def _yaml_split_key(content: str) -> Optional[Tuple[str, str]]:
    quote: Optional[str] = None
    for index, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'" and index == 0:
            quote = char
            continue
        if char == ":" and (index + 1 == len(content) or content[index + 1] in " \t"):
            key = content[:index].strip()
            if key.startswith(("'", '"')) and key.endswith(key[0]) and len(key) >= 2:
                key = key[1:-1]
            if not key or "{" in key or "[" in key:
                return None
            return key, content[index + 1:]
    return None


def _yaml_strip_comment(content: str) -> str:
    quote: Optional[str] = None
    for index, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or content[index - 1] in " \t"):
            return content[:index].rstrip()
    return content.rstrip()


def _yaml_scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return None
    if text[0] == '"' and text.endswith('"') and len(text) >= 2:
        return re.sub(r'\\(["\\/])', r"\1", text[1:-1]).replace("\\n", "\n").replace("\\t", "\t")
    if text[0] == "'" and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text[0] == "[" and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items: List[str] = []
        quote: Optional[str] = None
        current = ""
        for char in inner:
            if quote:
                current += char
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
                current += char
            elif char == ",":
                items.append(current)
                current = ""
            else:
                current += char
        items.append(current)
        return [_yaml_scalar(item) for item in items]
    if text == "{}":
        return YamlMap()
    if text in ("~", "null"):
        return None
    return text


class _YamlSubsetParser:
    def __init__(self, text: str) -> None:
        self.raw = text.splitlines()
        self.lines: List[List[Any]] = []
        for number, raw in enumerate(self.raw, start=1):
            stripped = raw.lstrip(" ")
            if stripped.startswith("\t") or "\t" in raw[: len(raw) - len(stripped)]:
                raise YamlSubsetError(f"tab indentation at line {number}")
            content = _yaml_strip_comment(stripped)
            if not content or content == "---":
                self.lines.append([None, "", number])
                continue
            self.lines.append([len(raw) - len(stripped), content, number])
        self.position = 0

    def peek(self) -> Optional[List[Any]]:
        while self.position < len(self.lines) and self.lines[self.position][0] is None:
            self.position += 1
        return self.lines[self.position] if self.position < len(self.lines) else None

    def advance(self) -> None:
        self.position += 1

    def parse_document(self) -> Any:
        first = self.peek()
        if first is None:
            return YamlMap()
        value = self.parse_node(first[0])
        trailing = self.peek()
        if trailing is not None:
            raise YamlSubsetError(f"unexpected content at line {trailing[2]}")
        return value

    def parse_node(self, indent: int) -> Any:
        line = self.peek()
        if line is None or line[0] < indent:
            return None
        if line[1] == "-" or line[1].startswith("- "):
            return self.parse_sequence(line[0])
        return self.parse_mapping(line[0])

    def parse_mapping(self, indent: int) -> YamlMap:
        result = YamlMap()
        while True:
            line = self.peek()
            if line is None or line[0] < indent:
                break
            if line[0] > indent:
                raise YamlSubsetError(f"unexpected indentation at line {line[2]}")
            if line[1] == "-" or line[1].startswith("- "):
                break
            split = _yaml_split_key(line[1])
            if split is None:
                raise YamlSubsetError(f"expected a mapping entry at line {line[2]}")
            key, rest = split
            self.advance()
            result.lines[key] = line[2]
            rest = rest.strip()
            if rest == "":
                following = self.peek()
                if following is not None and following[0] > indent:
                    result[key] = self.parse_node(following[0])
                elif following is not None and following[0] == indent and (following[1] == "-" or following[1].startswith("- ")):
                    result[key] = self.parse_sequence(indent)
                else:
                    result[key] = None
            elif rest[0] in "|>":
                result[key] = self.parse_block_scalar(indent, rest)
            else:
                result[key] = _yaml_scalar(rest)
        return result

    def parse_sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            line = self.peek()
            if line is None or line[0] < indent:
                break
            if line[0] > indent:
                raise YamlSubsetError(f"unexpected indentation at line {line[2]}")
            if not (line[1] == "-" or line[1].startswith("- ")):
                break
            rest = line[1][1:].strip()
            if rest == "":
                self.advance()
                following = self.peek()
                items.append(self.parse_node(following[0]) if following is not None and following[0] > indent else None)
            elif rest[0] in "|>":
                self.advance()
                items.append(self.parse_block_scalar(indent, rest))
            elif rest[0] not in "\"'[{" and _yaml_split_key(rest) is not None:
                line[0] = indent + 2
                line[1] = rest
                items.append(self.parse_mapping(indent + 2))
            else:
                self.advance()
                items.append(_yaml_scalar(rest))
        return items

    def parse_block_scalar(self, indent: int, header: str) -> str:
        style = header[0]
        chomp = "strip" if "-" in header[1:] else ("keep" if "+" in header[1:] else "clip")
        start = self.position
        block: List[str] = []
        block_indent: Optional[int] = None
        while self.position < len(self.raw):
            raw = self.raw[self.position]
            stripped = raw.lstrip(" ")
            if stripped == "":
                block.append("")
                self.position += 1
                continue
            current_indent = len(raw) - len(stripped)
            if current_indent <= indent:
                break
            if block_indent is None:
                block_indent = current_indent
            if current_indent < block_indent:
                break
            block.append(raw[block_indent:])
            self.position += 1
        if self.position == start:
            return ""
        while block and block[-1] == "":
            block.pop()
        if style == "|":
            text = "\n".join(block)
        else:
            paragraphs: List[List[str]] = [[]]
            for line in block:
                if line == "":
                    paragraphs.append([])
                else:
                    paragraphs[-1].append(line)
            text = "\n".join(" ".join(paragraph) for paragraph in paragraphs)
        return text if chomp == "strip" else text + "\n"


def parse_yaml_subset(text: str) -> Any:
    return _YamlSubsetParser(text).parse_document()


# ── Driver ───────────────────────────────────────────────────────────────────


def compile_service(root: Path) -> Dict[str, Any]:
    """The conforming model for the service at ``root``; raises CompileError when
    the tree cannot be described or the result would not conform."""
    root = root.resolve()
    config = load_config(root)
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
    target = root / MODEL_PATH
    if check:
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current != text:
            print(f"architecture error: {MODEL_PATH} is stale; run: python3 tools/architecture_compile_python.py {root}", file=sys.stderr)
            return 1
        if not quiet:
            print(f"{MODEL_PATH} is current ({CONTRACT_NAME} v{CONTRACT_VERSION})")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if not quiet:
        print(f"wrote {target.relative_to(root).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
