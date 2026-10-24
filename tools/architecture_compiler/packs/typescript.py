"""TypeScript / JavaScript language pack (.ts, .tsx, .js, .jsx, .mjs, .cjs)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import Declaration, ImportRecord, LanguagePack, LocalImport, Rule, child_of_type, descendant_of_type, node_end_line, node_line, node_text, strip_quotes, walk

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


RESOLVE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js")


def resolve_specifier(specifier: str, rel: str, all_rels: "frozenset[str]") -> str:
    """The analysed file a relative specifier names (``./x``, ``../lib/x.js``), or ''."""
    if not specifier.startswith("."):
        return ""
    parts = rel.split("/")[:-1]
    for segment in specifier.split("/"):
        if segment == "..":
            parts = parts[:-1]
        elif segment and segment != ".":
            parts.append(segment)
    joined = "/".join(parts)
    stem = re.sub(r"\.(?:js|jsx|mjs|cjs|ts|tsx)$", "", joined)
    for candidate in [joined] + [stem + suffix for suffix in RESOLVE_SUFFIXES]:
        if candidate in all_rels:
            return candidate
    return ""


def _clause_bindings(clause: Any, source: bytes) -> List[Tuple[str, str]]:
    """(local name, imported symbol) for every binding an import clause makes;
    the symbol is '' when the clause binds the module (namespace or default)."""
    found: List[Tuple[str, str]] = []
    for child in clause.children:
        if child.type == "identifier":
            found.append((node_text(child, source), ""))
        elif child.type == "namespace_import":
            name = child_of_type(child, "identifier")
            if name is not None:
                found.append((node_text(name, source), ""))
        elif child.type == "named_imports":
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                names = [c for c in spec.children if c.type == "identifier"]
                if names and not any(c.type == "type" for c in spec.children):
                    found.append((node_text(names[-1], source), node_text(names[0], source)))
    return found


def local_imports(root: Any, source: bytes, rel: str, all_rels: "frozenset[str]") -> List[LocalImport]:
    """Relative imports and requires of other analysed files; ``import type`` is
    erased by the compiler and binds nothing at runtime."""
    records: List[LocalImport] = []
    for node in walk(root):
        if node.type == "import_statement":
            if any(c.type == "type" for c in node.children):
                continue
            string = child_of_type(node, "string")
            clause = child_of_type(node, "import_clause")
            if string is None or clause is None:
                continue
            module = resolve_specifier(strip_quotes(node_text(string, source)), rel, all_rels)
            if module:
                records.extend(LocalImport(local, module, symbol, node_line(node)) for local, symbol in _clause_bindings(clause, source))
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is None or value.type != "call_expression":
                continue
            function = value.children[0] if value.children else None
            if function is None or function.type != "identifier" or node_text(function, source) != "require":
                continue
            string = descendant_of_type(value, "string")
            name = node.child_by_field_name("name")
            if string is None or name is None:
                continue
            module = resolve_specifier(strip_quotes(node_text(string, source)), rel, all_rels)
            if not module:
                continue
            if name.type == "identifier":
                records.append(LocalImport(node_text(name, source), module, "", node_line(node)))
            elif name.type == "object_pattern":
                for prop in name.children:
                    if prop.type == "shorthand_property_identifier_pattern":
                        records.append(LocalImport(node_text(prop, source), module, node_text(prop, source), node_line(node)))
                    elif prop.type == "pair_pattern":
                        key, value_ = prop.child_by_field_name("key"), prop.child_by_field_name("value")
                        if key is not None and value_ is not None:
                            records.append(LocalImport(node_text(value_, source), module, node_text(key, source), node_line(node)))
    return records


WRAPPER_PREFIX_RE = re.compile(r"`(/[A-Za-z0-9_./-]*?)\$\{([A-Za-z_$][\w$]*)\}`")  # a template that is a literal prefix and one parameter hole
CONCAT_PREFIX_RE = re.compile(r"[\"'](/[A-Za-z0-9_./-]*?)[\"']\s*\+\s*([A-Za-z_$][\w$]*)\b")
TEMPLATE_HELPER_RE = re.compile(r"\b(?:const|let|function)\s+([A-Za-z_$][\w$]*)\s*=?\s*\(([^)]*)\)\s*(?::\s*string\s*)?(?:=>\s*|\{\s*return\s+)`([^`]*)`")


def wrapper_prefix(body: str, params: "set[str]") -> Optional[str]:
    """`/api/items${path}` or "/api/items" + path, with the hole one of the member's parameters."""
    for pattern in (WRAPPER_PREFIX_RE, CONCAT_PREFIX_RE):
        for match in pattern.finditer(body):
            if match.group(2) in params:
                return match.group(1)
    return None


def template_helpers(module_raw: str) -> Dict[str, str]:
    """Module functions whose body is one template literal (const space = (id) => `/spaces/${id}`)."""
    return {m.group(1): m.group(3) for m in TEMPLATE_HELPER_RE.finditer(module_raw)}


def inline_helpers(fragment: str, helpers: Dict[str, str], depth: int = 0) -> str:
    if depth > 3 or not helpers:
        return fragment

    def swap(match: "re.Match[str]") -> str:
        body = helpers.get(match.group(1))
        return inline_helpers(body, helpers, depth + 1) if body is not None else match.group(0)

    return re.sub(r"\$\{([A-Za-z_$][\w$]*)\([^{}]*\)\}", swap, fragment)


def literal_calls(body: str, member: str, module_raw: str) -> List[Tuple[str, int]]:
    """``this.<member>(`…`)`` / ``this.<member>("…")`` calls: the literal with the
    module's template helpers inlined (remaining holes stay as ``${x}`` segments)."""
    helpers = template_helpers(module_raw)
    found: List[Tuple[str, int]] = []
    for match in re.finditer(r"\bthis\." + re.escape(member) + r"(?:<[^>\n]*>)?\(\s*(?:`([^`]*)`|\"([^\"\n]*)\"|'([^'\n]*)')", body):
        found.append((inline_helpers(match.group(1) or match.group(2) or match.group(3) or "", helpers), match.start()))
    return found


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
    Rule("typescript.route", "A route registration with a literal path: app/router.get|post|put|delete|patch|all|use(\"/path\")", "trigger", "raw",
         r"\b(?:app|router|server|api)\.(?:get|post|put|delete|patch|all|use)\(\s*[\"'`]([^\"'`]+)[\"'`]", produces="endpoint", sub_kind="route", label_group=1,
         probe=r"\b(?:app|router|server|api)\.(?:get|post|put|delete|patch|all)\("),
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
    self_name="this",
    local_imports=local_imports,
    construct_pattern=r"\bnew\s+([A-Z][\w$]*)\s*\(",
    wrapper_prefix=wrapper_prefix,
    literal_calls=literal_calls,
    limitations=(
        "TypeScript: routes are recognised on app/router/server/api receivers; frameworks that register handlers by decorator or by object literal are not attributed.",
        "TypeScript: a route registration whose path is a variable or a template with a hole is counted as unparsed, not registered as an endpoint.",
        "TypeScript: dynamic import() with a computed specifier is not an import.",
    ),
)
