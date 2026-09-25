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
from typing import Any, Dict, List, Sequence, Tuple

from .base import Declaration, ImportRecord, LanguagePack, Rule, child_of_type, node_end_line, node_line, node_text, walk

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
    Rule("python.route", "A route decorator (Flask/FastAPI/aiohttp style: route, get, post, put, delete, patch, websocket, api_route)", "trigger", "raw",
         r"@\w+(?:\.\w+)*\.(?:route|get|post|put|delete|patch|websocket|api_route)\(\s*[\"']([^\"']+)", produces="endpoint", sub_kind="route", label_group=1),
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
    limitations=(
        "Python: decorators assembled at runtime, getattr-based wiring and plugin registries are invisible to the passes.",
    ),
)
