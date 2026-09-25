"""Rust language pack."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from .base import Declaration, ImportRecord, LanguagePack, Rule, child_of_type, node_end_line, node_line, node_text, walk

RUST_STDLIB = frozenset({"std", "core", "alloc", "proc_macro", "test"})
LOCAL_PREFIXES = {"crate", "self", "super"}


def census(root: Any, source: bytes) -> List[Declaration]:
    found: List[Declaration] = []
    for node in walk(root):
        if node.type in ("struct_item", "enum_item", "trait_item", "union_item", "type_item"):
            name_node = child_of_type(node, "type_identifier")
            if name_node is not None:
                kind = {"struct_item": "struct", "enum_item": "enum", "trait_item": "trait", "union_item": "struct", "type_item": "type"}[node.type]
                found.append(Declaration(kind, node_text(name_node, source), node_line(node), node_end_line(node)))
        elif node.type == "impl_item":
            names = [c for c in node.children if c.type in ("type_identifier", "generic_type", "scoped_type_identifier")]
            if names:
                found.append(Declaration("impl", node_text(names[-1], source).split("<")[0], node_line(node), node_end_line(node)))
        elif node.type == "function_item" or node.type == "function_signature_item":
            name_node = child_of_type(node, "identifier")
            if name_node is not None:
                found.append(Declaration("function", node_text(name_node, source), node_line(node), node_end_line(node)))
        elif node.type == "mod_item":
            name_node = child_of_type(node, "identifier")
            if name_node is not None:
                found.append(Declaration("module", node_text(name_node, source), node_line(node), node_end_line(node)))
    return found


def use_root(text: str) -> str:
    body = re.sub(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+", "", text.strip()).rstrip(";").strip()
    body = body.lstrip("::")
    first = re.split(r"::|\s|\{", body, maxsplit=1)[0].strip()
    return first


def imports(root: Any, source: bytes) -> List[ImportRecord]:
    records: List[ImportRecord] = []
    for node in walk(root):
        if node.type == "use_declaration":
            root_name = use_root(node_text(node, source))
            if root_name and root_name not in LOCAL_PREFIXES:
                records.append(ImportRecord(root_name, node_line(node), root_name))
        elif node.type == "extern_crate_declaration":
            name_node = child_of_type(node, "identifier")
            if name_node is not None:
                records.append(ImportRecord(node_text(name_node, source), node_line(node), node_text(name_node, source)))
    return records


def entrypoints(root: Any, text: str, rel: str) -> List[Tuple[int, str]]:
    match = re.search(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+main\s*\(", text, re.MULTILINE)
    return [(text.count("\n", 0, match.start()) + 1, "main")] if match else []


def local_roots(rels: Sequence[str], config: Dict[str, Any]) -> frozenset:
    roots = set(str(m) for m in config.get("local_modules") or [])
    for rel in rels:
        roots.add(rel.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    return frozenset(roots)


RULES: Tuple[Rule, ...] = (
    Rule("rust.import_external", "A `use` of a crate outside std/core/alloc and outside this crate (crate::/self::/super:: paths are local)", "boundary", "code", r"(?!x)x"),
    Rule("rust.route", "A route: #[get|post|put|delete|patch|route(\"/path\")] or .route(\"/path\")", "trigger", "raw",
         r"(?:#\[(?:get|post|put|delete|patch|route)|\.route)\(\s*\"([^\"]+)\"", produces="endpoint", sub_kind="route", label_group=1),
    Rule("rust.http_client", "An HTTP client call: reqwest::*(…), Client::new(), hyper::Client::*, ureq::*(…)", "boundary", "code",
         r"\breqwest::\w+(?:::\w+)*\s*\(|\bClient::(?:new|builder)\s*\(|\bhyper::Client::|\bureq::\w+\s*\(", produces="resource", sub_kind="http_client"),
    Rule("rust.socket_or_server", "A listener or connection: TcpListener::bind, TcpStream::connect, UdpSocket::bind, tokio::net, axum/actix servers", "boundary", "code",
         r"\bTcpListener::bind\b|\bTcpStream::connect\b|\bUdpSocket::bind\b|\btokio::net::|\baxum::(?:Server|serve)\b|\bHttpServer::new\b", produces="resource", sub_kind="socket"),
    Rule("rust.file_write", "A file or directory written: fs::write, File::create, fs::create_dir(_all), OpenOptions", "store", "code",
         r"\bfs::write\(|\bFile::create\(|\bfs::create_dir(?:_all)?\(|\bOpenOptions::new\(\)", produces="store", sub_kind="file"),
    Rule("rust.process_spawn", "A child process: Command::new", "behaviour", "code", r"\bCommand::new\("),
    Rule("rust.env_read", "An environment read: env::var(_os), env::vars", "boundary", "code", r"\benv::vars?(?:_os)?\("),
    Rule("rust.persistent_store", "A persistence API: rusqlite, sled, sqlx, diesel, redis::Client, Connection::open", "store", "code",
         r"\brusqlite::|\bsled::open\(|\bsqlx::|\bdiesel::|\bredis::Client\b|\bConnection::open\(", produces="store", sub_kind="mechanism"),
    Rule("rust.concurrency", "Concurrency: tokio::spawn, thread::spawn, tokio::task, async_std::task::spawn, channels, Arc<Mutex<…>>", "behaviour", "code",
         r"\btokio::spawn\(|\bthread::spawn\(|\btokio::task::|\basync_std::task::spawn\(|\bmpsc::channel\(|\bArc<Mutex<"),
    Rule("rust.state_machine", "An enum variant assigned to a field or binding: a lifecycle state, and each assignment a transition", "behaviour", "code", r"(?!x)x"),
)

PACK = LanguagePack(
    name="rust",
    label="Rust",
    extensions=(".rs",),
    grammar="rust",
    comment_types=frozenset({"line_comment", "block_comment"}),
    string_types=frozenset({"string_literal", "raw_string_literal", "char_literal"}),
    stdlib=RUST_STDLIB,
    rules=RULES,
    census=census,
    imports=imports,
    entrypoints=entrypoints,
    local_roots=local_roots,
    enum_separator="::",
    typed_property_pattern=r"\blet\s+(?:mut\s+)?(\w+)\s*:\s*(\w+)\b",
    limitations=(
        "Rust: procedural macros and derive expansions are seen as source text, not as what they generate; routes declared by attribute are recognised by the attribute's text.",
    ),
)
