"""Python language pack on the tree-sitter floor.

The first-generation Python compiler (``tools/architecture_compile_python.py``)
uses the ``ast`` module and remains the reference for Python-only services;
this pack exists so a mixed-language service runs every file through the same
front end, and so the Python rules are written the same way as every other
language's.
"""
from __future__ import annotations

import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import Declaration, ImportRecord, LanguagePack, LocalImport, Rule, child_of_type, node_end_line, node_line, node_text, walk
from .python_process import TASK_STARTER_PATTERNS, loops, resident_state, schema

PY_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
ENUM_BASE_RE = re.compile(r"\b(?:enum\.)?(?:Enum|IntEnum|StrEnum|Flag|IntFlag)\b")


def census(root: Any, source: bytes) -> List[Declaration]:
    found: List[Declaration] = []
    for node in walk(root):
        if node.type == "class_definition":
            name_node = child_of_type(node, "identifier")
            if name_node is not None:
                bases = child_of_type(node, "argument_list")
                is_enum = bases is not None and bool(ENUM_BASE_RE.search(node_text(bases, source)))
                found.append(Declaration("enum" if is_enum else "class", node_text(name_node, source), node_line(node), node_end_line(node)))
        elif node.type == "function_definition":
            name_node = child_of_type(node, "identifier")
            if name_node is not None:
                found.append(Declaration("function", node_text(name_node, source), node_line(node), node_end_line(node)))
    return found


def imports(root: Any, source: bytes) -> List[ImportRecord]:
    records: List[ImportRecord] = []
    for node in walk(root):
        if node.type == "import_statement":
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    dotted = child if child.type == "dotted_name" else child_of_type(child, "dotted_name")
                    if dotted is not None:
                        root_name = node_text(dotted, source).split(".")[0]
                        records.append(ImportRecord(root_name, node_line(node), root_name))
        elif node.type == "import_from_statement":
            if child_of_type(node, "relative_import") is not None:
                continue
            dotted = child_of_type(node, "dotted_name")
            if dotted is not None:
                root_name = node_text(dotted, source).split(".")[0]
                records.append(ImportRecord(root_name, node_line(node), root_name))
    return records


def _module_candidates(dotted: str, rel: str, dots: int) -> List[str]:
    """The analysed files a (possibly relative) dotted module name may be."""
    if dots:
        base_parts = rel.split("/")[:-1]
        base_parts = base_parts[:len(base_parts) - (dots - 1)] if dots > 1 else base_parts
        base = "/".join(base_parts)
    else:
        base = ""
    path = "/".join(part for part in dotted.split(".") if part)
    joined = f"{base}/{path}" if base and path else (base or path)
    return [f"{joined}.py", f"{joined}/__init__.py"] if joined else [f"{base}/__init__.py"]


def local_imports(root: Any, source: bytes, rel: str, all_rels: "frozenset[str]") -> List[LocalImport]:
    """What a file binds from other files of the service, by import statement:
    ``import a.b as m`` and ``from . import m`` bind a module; ``from a.b import
    f`` binds ``f`` from ``a/b.py`` unless ``a/b/f.py`` is itself a file."""
    records: List[LocalImport] = []

    def existing(candidates: List[str]) -> str:
        return next((c for c in candidates if c in all_rels), "")

    for node in walk(root):
        if node.type == "import_statement":
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    dotted = child if child.type == "dotted_name" else child_of_type(child, "dotted_name")
                    alias = child_of_type(child, "identifier") if child.type == "aliased_import" else None
                    if dotted is None:
                        continue
                    name = node_text(dotted, source)
                    module = existing(_module_candidates(name, rel, 0))
                    if module:
                        records.append(LocalImport(node_text(alias, source) if alias is not None else name, module, "", node_line(node)))
        elif node.type == "import_from_statement":
            relative = child_of_type(node, "relative_import")
            dots = 0
            dotted_text = ""
            if relative is not None:
                prefix = child_of_type(relative, "import_prefix")
                dots = len(node_text(prefix, source)) if prefix is not None else 0
                inner = child_of_type(relative, "dotted_name")
                dotted_text = node_text(inner, source) if inner is not None else ""
            else:
                dotted = child_of_type(node, "dotted_name")
                if dotted is None:
                    continue
                dotted_text = node_text(dotted, source)
            package = existing(_module_candidates(dotted_text, rel, dots))
            names = [c for c in node.children if c.type in ("dotted_name", "aliased_import")]
            if relative is None:
                names = names[1:]  # the first dotted_name is the module
            for child in names:
                imported = child if child.type == "dotted_name" else child_of_type(child, "dotted_name")
                alias = child_of_type(child, "identifier") if child.type == "aliased_import" else None
                if imported is None:
                    continue
                symbol = node_text(imported, source)
                local = node_text(alias, source) if alias is not None else symbol
                submodule = existing(_module_candidates(f"{dotted_text}.{symbol}" if dotted_text else symbol, rel, dots))
                if submodule:
                    records.append(LocalImport(local, submodule, "", node_line(node)))
                elif package:
                    records.append(LocalImport(local, package, symbol, node_line(node)))
    return records


def handler_line(root: Any, source: bytes, line: int) -> Optional[int]:
    """The function a decorator on ``line`` decorates: where its ``def`` starts."""
    for node in walk(root):
        if node.type != "decorated_definition":
            continue
        decorators = [c for c in node.children if c.type == "decorator"]
        if not any(node_line(d) <= line <= node_end_line(d) for d in decorators):
            continue
        definition = child_of_type(node, "function_definition", "class_definition")
        if definition is not None:
            return node_line(definition)
    return None


WRAPPER_FORMS = (
    r"f[\"'](/[A-Za-z0-9_./-]*?)\{(\w+)\}[\"']",                              # f"/api/items{path}"
    r"[\"'](/[A-Za-z0-9_./-]*?)[\"']\s*\+\s*(\w+)\b",                           # "/api/items" + path
    r"[\"'](/[A-Za-z0-9_./-]*?)%s[\"']\s*%\s*\(?(\w+)\b",                        # "/api/items%s" % path
    r"[\"'](/[A-Za-z0-9_./-]*?)\{\}[\"']\.format\(\s*(\w+)\b",                   # "/api/items{}".format(path)
)


def wrapper_prefix(body: str, params: "set[str]") -> Optional[str]:
    """The literal prefix a member lends when its body spells a path as that
    prefix plus one of its own parameters."""
    for form in WRAPPER_FORMS:
        for match in re.finditer(form, body):
            if match.group(2) in params:
                return match.group(1)
    return None


def literal_calls(body: str, member: str, module_raw: str) -> List[Tuple[str, int]]:
    """``self.<member>("literal")`` and ``self.<member>(f"…")`` calls in a body:
    the literal (an f-string's holes kept as ``{x}`` segments) and its offset."""
    found: List[Tuple[str, int]] = []
    for match in re.finditer(r"\bself\." + re.escape(member) + r"\(\s*(?:f|r|rf|fr)?([\"'])([^\"'\n]*)\1", body):
        found.append((match.group(2), match.start()))
    return found


def entrypoints(root: Any, text: str, rel: str) -> List[Tuple[int, str]]:
    match = re.search(r"^if\s+__name__\s*==\s*[\"']__main__[\"']", text, re.MULTILINE)
    if match:
        return [(text.count("\n", 0, match.start()) + 1, "__main__")]
    match = re.search(r"^def\s+main\s*\(", text, re.MULTILINE)
    return [(text.count("\n", 0, match.start()) + 1, "main")] if match else []


def local_roots(rels: Sequence[str], config: Dict[str, Any]) -> frozenset:
    roots = set(str(m) for m in config.get("local_modules") or [])
    for rel in rels:
        first = rel.split("/", 1)[0]
        roots.add(first[:-3] if first.endswith(".py") else first)
    return frozenset(roots)


RULES: Tuple[Rule, ...] = (
    Rule("python.import_external", "A module-level import of a package outside the standard library and outside this service", "boundary", "code", r"(?!x)x"),
    Rule("python.route", "A route decorator (Flask/FastAPI/aiohttp style: route, get, post, put, delete, patch, websocket, api_route) with a literal path", "trigger", "raw",
         r"@\w+(?:\.\w+)*\.(?:route|get|post|put|delete|patch|websocket|api_route)\(\s*[\"']([^\"']+)", produces="endpoint", sub_kind="route", label_group=1,
         probe=r"@\w+(?:\.\w+)*\.(?:route|get|post|put|delete|patch|websocket|api_route)\("),
    Rule("python.http_client", "An HTTP client call: requests, httpx, urllib.request, aiohttp.ClientSession, http.client", "boundary", "code",
         r"\b(?:requests|httpx)\.\w+\(|\burllib\.request\.|\baiohttp\.ClientSession\b|\bhttp\.client\.", produces="resource", sub_kind="http_client"),
    Rule("python.socket_or_server", "A socket or server: socket.*, websockets.*, asyncio.start_server/open_connection", "boundary", "code",
         r"\bsocket\.(?:socket|create_connection|create_server)\(|\bwebsockets\.|\basyncio\.(?:start_server|open_connection)\(", produces="resource", sub_kind="socket"),
    Rule("python.file_write", "A file opened for writing or appending, or Path.write_text/write_bytes", "store", "raw",
         r"\bopen\([^)\n]*[\"'][wax]b?[\"']|\.write_(?:text|bytes)\(", produces="store", sub_kind="file"),
    Rule("python.process_spawn", "A subprocess spawn: subprocess.*, os.system, os.popen, asyncio.create_subprocess_*", "behaviour", "code",
         r"\bsubprocess\.\w+\(|\bos\.(?:system|popen)\(|\basyncio\.create_subprocess_\w+\("),
    Rule("python.env_read", "An environment read: os.environ, os.getenv", "boundary", "code", r"\bos\.environ\b|\bos\.getenv\("),
    Rule("python.persistent_store", "A persistent store site: sqlite3.connect, json.dump, pickle.dump, shelve.open, dbm.open", "store", "code",
         r"\bsqlite3\.connect\(|\bjson\.dump\(|\bpickle\.dump\(|\bshelve\.open\(|\bdbm\.open\(", produces="store", sub_kind="mechanism"),
    Rule("python.concurrency", "Concurrency: threading.Thread, multiprocessing.Process, asyncio.create_task/ensure_future/gather, concurrent.futures", "behaviour", "code",
         r"\bthreading\.Thread\(|\bmultiprocessing\.Process\(|\basyncio\.(?:create_task|ensure_future|gather)\(|\bconcurrent\.futures\."),
    Rule("python.state_machine", "An Enum subclass whose members are assigned to an attribute somewhere in the tree: a lifecycle state, and each assignment a transition", "behaviour", "code", r"(?!x)x"),
    # The four below are driven by the core (process.py) from the pack's loops/resident_state/schema hooks; the pattern never fires on its own.
    Rule("python.launch.loop", "A paced loop the entry point runs for the process's life — `while` on a stop flag or forever, its body sleeping, waiting or blocking on a read — named for its holder, with the interval expression and the calls its body makes in source order", "behaviour", "code", r"(?!x)x", produces="tick", sub_kind="loop"),
    Rule("python.launch.starter", "Periodic work a module the entry point loads runs for the process's life: a function handed to a thread, timer or task (`Thread(target=f)`, `create_task(f())`) whose body holds a paced loop, or a loop-holding function called at module level; closures handed out per job are not starters", "behaviour", "code", r"(?!x)x", produces="tick", sub_kind="starter"),
    Rule("python.state.resident", "A collection a process keeps in its own memory: a module-level name or an instance field initialised to a dict, list, set or deque in a module the entry point loads, and written somewhere in that module; a class field counts only where the class is constructed outside its body. The process keeps it, and so does every loop whose calls write it", "behaviour", "code", r"(?!x)x", produces="state"),
    Rule("python.schema.table", "A table the file declares: `CREATE TABLE name (…)` inside a string literal, or a SQLAlchemy class with `__tablename__`", "store", "code", r"(?!x)x", produces="table", sub_kind="sql"),
    Rule("python.schema.references", "A foreign key: a column's `REFERENCES other(col) [ON DELETE …]`, a table-level `FOREIGN KEY (…) REFERENCES other(…)`, or a SQLAlchemy `ForeignKey(\"other.col\", ondelete=…)` — the table references the table named, with the columns on both sides and the delete rule", "store", "code", r"(?!x)x"),
)

PACK = LanguagePack(
    name="python",
    label="Python",
    extensions=(".py",),
    grammar="python",
    comment_types=frozenset({"comment"}),
    string_types=frozenset({"string", "concatenated_string"}),
    stdlib=PY_STDLIB,
    rules=RULES,
    census=census,
    imports=imports,
    entrypoints=entrypoints,
    local_roots=local_roots,
    enum_separator=".",
    typed_property_pattern=r"\b(\w+)\s*:\s*(\w+)\s*=",
    self_name="self",
    local_imports=local_imports,
    construct_pattern=r"(?<![\w.])([A-Z][A-Za-z0-9_]*)\s*\(",
    wrapper_prefix=wrapper_prefix,
    literal_calls=literal_calls,
    handler_line=handler_line,
    loops=loops,
    task_starter_patterns=TASK_STARTER_PATTERNS,
    resident_state=resident_state,
    schema=schema,
    limitations=(
        "Python: decorators assembled at runtime, getattr-based wiring and plugin registries are invisible to the passes.",
        "Python: a route decorator whose path is not a literal is counted as unparsed, not registered as an endpoint.",
        "Python: a loop is a tick only when its body is paced by a timer, a wait or a blocking read; a loop that spins on data is an algorithm, not a schedule. SQL tables are read from CREATE TABLE text inside string literals; tables built by an ORM's metadata at runtime are not seen.",
    ),
)
