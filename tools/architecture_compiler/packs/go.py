"""Go language pack."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from .base import Declaration, ImportRecord, LanguagePack, Rule, child_of_type, node_end_line, node_line, node_text, strip_quotes, walk

GO_STDLIB = frozenset("""
archive bufio builtin bytes cmp compress container context crypto database debug embed encoding errors expvar flag fmt go hash html image
index io iter log maps math mime net os path plugin reflect regexp runtime slices sort strconv strings structs sync syscall testing text time
unicode unique unsafe internal
""".split())


def census(root: Any, source: bytes) -> List[Declaration]:
    found: List[Declaration] = []
    for node in walk(root):
        if node.type == "type_declaration":
            for spec in node.children:
                if spec.type != "type_spec":
                    continue
                name_node = child_of_type(spec, "type_identifier")
                if name_node is None:
                    continue
                kind = "struct" if child_of_type(spec, "struct_type") else "interface" if child_of_type(spec, "interface_type") else "type"
                found.append(Declaration(kind, node_text(name_node, source), node_line(node), node_end_line(node)))
        elif node.type == "function_declaration":
            name_node = child_of_type(node, "identifier")
            if name_node is not None:
                found.append(Declaration("function", node_text(name_node, source), node_line(node), node_end_line(node)))
        elif node.type == "method_declaration":
            name_node = child_of_type(node, "field_identifier")
            if name_node is not None:
                found.append(Declaration("function", node_text(name_node, source), node_line(node), node_end_line(node)))
    return found


def import_root(path: str) -> str:
    parts = path.split("/")
    if "." not in parts[0]:
        return parts[0]  # standard library: no domain in the first segment
    return "/".join(parts[:3]) if len(parts) >= 3 else path


def imports(root: Any, source: bytes) -> List[ImportRecord]:
    records: List[ImportRecord] = []
    for node in walk(root):
        if node.type == "import_spec":
            string = child_of_type(node, "interpreted_string_literal", "raw_string_literal")
            if string is not None:
                path = strip_quotes(node_text(string, source))
                records.append(ImportRecord(import_root(path), node_line(node), path))
    return records


def entrypoints(root: Any, text: str, rel: str) -> List[Tuple[int, str]]:
    match = re.search(r"^func\s+main\s*\(", text, re.MULTILINE)
    return [(text.count("\n", 0, match.start()) + 1, "main")] if match else []


def local_roots(rels: Sequence[str], config: Dict[str, Any]) -> frozenset:
    roots = set(str(m) for m in config.get("local_modules") or [])
    return frozenset(import_root(r) for r in roots) | frozenset(roots)


RULES: Tuple[Rule, ...] = (
    Rule("go.import_external", "An import of a module outside the Go standard library and outside this module (the first path segment carries a domain)", "boundary", "code", r"(?!x)x"),
    Rule("go.route", "A route registration: HandleFunc/Handle(\"/path\") or a router's GET/POST/PUT/DELETE/PATCH(\"/path\")", "trigger", "raw",
         r"\b(?:HandleFunc|Handle|GET|POST|PUT|DELETE|PATCH)\(\s*\"([^\"]+)\"", produces="endpoint", sub_kind="route", label_group=1),
    Rule("go.http_client", "An HTTP client: http.Get/Post/Head/NewRequest/Client/DefaultClient", "boundary", "code",
         r"\bhttp\.(?:Get|Post|Head|NewRequest|NewRequestWithContext|Client|DefaultClient)\b", produces="resource", sub_kind="http_client"),
    Rule("go.socket_or_server", "A listener or connection: net.Listen*/Dial*, http.ListenAndServe*, grpc.Dial/NewServer, websocket", "boundary", "code",
         r"\bnet\.(?:Listen|Dial)\w*\(|\bhttp\.ListenAndServe\w*\(|\bgrpc\.(?:Dial|NewServer|NewClient)\(|\bwebsocket\.\w+\(", produces="resource", sub_kind="socket"),
    Rule("go.file_write", "A file or directory written: os.WriteFile/Create/OpenFile/Mkdir(All), ioutil.WriteFile", "store", "code",
         r"\bos\.(?:WriteFile|Create|OpenFile|MkdirAll|Mkdir)\(|\bioutil\.WriteFile\(", produces="store", sub_kind="file"),
    Rule("go.process_spawn", "A child process: exec.Command(Context), syscall.Exec", "behaviour", "code", r"\bexec\.Command(?:Context)?\(|\bsyscall\.Exec\("),
    Rule("go.env_read", "An environment read: os.Getenv/LookupEnv/Environ", "boundary", "code", r"\bos\.(?:Getenv|LookupEnv|Environ)\("),
    Rule("go.persistent_store", "A persistence API: sql.Open, bolt/badger/gorm.Open, redis client", "store", "code",
         r"\bsql\.Open\(|\bbolt\.Open\(|\bbadger\.Open\(|\bgorm\.Open\(|\bredis\.New\w*\(", produces="store", sub_kind="mechanism"),
    Rule("go.concurrency", "Concurrency: go statements, sync.WaitGroup, channels", "behaviour", "code",
         r"^\s*go\s+\w|\bgo\s+func\b|\bsync\.WaitGroup\b|\bmake\(\s*chan\b"),
    Rule("go.state_machine", "A typed constant set assigned to a field (Go has no enums; this pass fires only for declared enum types)", "behaviour", "code", r"(?!x)x"),
)

PACK = LanguagePack(
    name="go",
    label="Go",
    extensions=(".go",),
    grammar="go",
    comment_types=frozenset({"comment"}),
    string_types=frozenset({"interpreted_string_literal", "raw_string_literal", "rune_literal"}),
    stdlib=GO_STDLIB,
    rules=RULES,
    census=census,
    imports=imports,
    entrypoints=entrypoints,
    local_roots=local_roots,
    enum_separator=".",
    limitations=(
        "Go: the service's own module path must be listed in config `local_modules` for its internal packages to count as local rather than external.",
        "Go: routes registered through a mux variable named other than by method (e.g. r.Handle) are recognised by method name only.",
    ),
)
