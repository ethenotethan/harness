"""TypeScript / JavaScript language pack (.ts, .tsx, .js, .jsx, .mjs, .cjs)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from .base import Declaration, ImportRecord, LanguagePack, Rule, child_of_type, descendant_of_type, node_end_line, node_line, node_text, strip_quotes, walk

NODE_BUILTINS = frozenset("""
assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns domain events fs http http2 https
inspector module net os path perf_hooks process punycode querystring readline repl stream string_decoder sys timers tls trace_events tty
url util v8 vm wasi worker_threads zlib test
""".split())

REQUIRE_RE = re.compile(r"\brequire\(\s*[\"']([^\"']+)[\"']\s*\)")
TYPED_PROPERTY_RE = re.compile(r"(?:let|const|var|private|public|protected|readonly)\s+(\w+)\s*:\s*(\w+)\b")

DECLARATIONS = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "type_alias_declaration": "type",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "method_definition": "function",
}


def census(root: Any, source: bytes) -> List[Declaration]:
    found: List[Declaration] = []
    for node in walk(root):
        kind = DECLARATIONS.get(node.type)
        if kind is None:
            continue
        name_node = child_of_type(node, "type_identifier", "identifier", "property_identifier")
        if name_node is None:
            continue
        found.append(Declaration(kind, node_text(name_node, source), node_line(node), node_end_line(node)))
    return found


def package_root(specifier: str) -> str:
    if specifier.startswith((".", "/")):
        return ""
    if specifier.startswith("node:"):
        return specifier[len("node:"):].split("/")[0]
    if specifier.startswith("@"):
        parts = specifier.split("/")
        return "/".join(parts[:2])
    return specifier.split("/")[0]


def imports(root: Any, source: bytes) -> List[ImportRecord]:
    records: List[ImportRecord] = []
    for node in walk(root):
        if node.type == "import_statement":
            string = child_of_type(node, "string")
            if string is not None:
                root_name = package_root(strip_quotes(node_text(string, source)))
                if root_name:
                    records.append(ImportRecord(root_name, node_line(node), root_name))
        elif node.type == "call_expression":
            function = node.children[0] if node.children else None
            if function is not None and function.type == "identifier" and node_text(function, source) == "require":
                string = descendant_of_type(node, "string")
                if string is not None:
                    root_name = package_root(strip_quotes(node_text(string, source)))
                    if root_name:
                        records.append(ImportRecord(root_name, node_line(node), root_name))
    return records


def entrypoints(root: Any, text: str, rel: str) -> List[Tuple[int, str]]:
    match = re.search(r"^\s*(?:export\s+)?(?:async\s+)?function\s+main\s*\(", text, re.MULTILINE)
    if match:
        return [(text.count("\n", 0, match.start()) + 1, "main")]
    match = re.search(r"\.listen\s*\(", text)
    if match:
        return [(text.count("\n", 0, match.start()) + 1, "listen")]
    return []


def local_roots(rels: Sequence[str], config: Dict[str, Any]) -> frozenset:
    return frozenset(str(m) for m in config.get("local_modules") or [])


RULES: Tuple[Rule, ...] = (
    Rule("typescript.import_external", "An import or require of a package outside Node's builtins and outside this service (relative specifiers are local)", "boundary", "code", r"(?!x)x"),
    Rule("typescript.route", "A route registration: app/router.get|post|put|delete|patch|all|use(\"/path\")", "trigger", "raw",
         r"\b(?:app|router|server|api)\.(?:get|post|put|delete|patch|all|use)\(\s*[\"'`]([^\"'`]+)[\"'`]", produces="endpoint", sub_kind="route", label_group=1),
    Rule("typescript.http_client", "An HTTP client: fetch, axios, got, http(s).request/get", "boundary", "code",
         r"\bfetch\s*\(|\baxios\b|\bgot\s*\(|\bhttps?\.(?:request|get)\s*\(", produces="resource", sub_kind="http_client"),
    Rule("typescript.socket_or_server", "A server or socket: .listen(), createServer, WebSocket, net.connect, socket.io", "boundary", "code",
         r"\.listen\s*\(|\bcreateServer\s*\(|\bWebSocket\b|\bnet\.connect\s*\(|\bio\s*\(\s*(?:server|http)", produces="resource", sub_kind="socket"),
    Rule("typescript.file_write", "A file or directory written: writeFile(Sync), appendFile(Sync), createWriteStream, mkdir(Sync)", "store", "code",
         r"\bwriteFileSync?\s*\(|\bappendFileSync?\s*\(|\bcreateWriteStream\s*\(|\bmkdirSync?\s*\(", produces="store", sub_kind="file"),
    Rule("typescript.process_spawn", "A child process: spawn, exec, execSync, execFile, fork, child_process", "behaviour", "code",
         r"\b(?:spawn|exec|execSync|execFile|execFileSync|fork)\s*\(|\bchild_process\b"),
    Rule("typescript.env_read", "An environment read: process.env", "boundary", "code", r"\bprocess\.env\b"),
    Rule("typescript.persistent_store", "A persistence API: localStorage, indexedDB, sqlite3/better-sqlite3, mongoose, prisma, knex, redis, level", "store", "code",
         r"\blocalStorage\b|\bindexedDB\b|\bbetter[-_]sqlite3\b|\bsqlite3?\b|\bmongoose\b|\bprisma\b|\bknex\b|\bredis\b|\blevel(?:up|db)?\s*\(", produces="store", sub_kind="mechanism"),
    Rule("typescript.concurrency", "Concurrency: new Worker, worker_threads, setInterval, Promise.all/allSettled/race, queueMicrotask", "behaviour", "code",
         r"\bnew\s+Worker\s*\(|\bworker_threads\b|\bsetInterval\s*\(|\bPromise\.(?:all|allSettled|race)\s*\(|\bqueueMicrotask\s*\("),
    Rule("typescript.state_machine", "An enum member assigned to a variable or property: a lifecycle state, and each assignment a transition", "behaviour", "code", r"(?!x)x"),
)

PACK = LanguagePack(
    name="typescript",
    label="TypeScript",
    extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    grammar="typescript",
    grammar_by_extension={".tsx": "tsx", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript"},
    comment_types=frozenset({"comment", "html_comment"}),
    string_types=frozenset({"string", "template_string"}),
    stdlib=NODE_BUILTINS,
    rules=RULES,
    census=census,
    imports=imports,
    entrypoints=entrypoints,
    local_roots=local_roots,
    enum_separator=".",
    typed_property_pattern=TYPED_PROPERTY_RE.pattern,
    limitations=(
        "TypeScript: routes are recognised on app/router/server/api receivers; frameworks that register handlers by decorator or by object literal are not attributed.",
        "TypeScript: dynamic import() with a computed specifier is not an import.",
    ),
)
