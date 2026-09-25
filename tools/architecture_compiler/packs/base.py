"""What a language pack is.

A pack is the language-specific front end of the architecture compiler: the
grammar to parse with, which node types are comments and string literals (so
signatures can be scoped to code or to strings), how to take the declaration
census (types and functions with their body ranges), how imports name the
packages they pull in, where the program starts, and the **rule table** — one
entry per pass of the extractor's grammar, each a regex over masked code, raw
text or string contents, or a tree-sitter query, with the family the renderers
colour it by and the construction (if any) it puts on the map.

Everything a pack computes is cited by (file, line). Whatever a pack's rules do
not recognise does not exist on the map; that is the point of the extraction
map, and why the census counts every declaration whether or not a rule fired.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

FAMILIES = ("store", "behaviour", "boundary", "wiring", "trigger")
TYPE_KINDS = ("class", "struct", "enum", "protocol", "interface", "trait", "actor", "extension", "impl", "type", "module")
NODE_KINDS = ("external", "store", "resource", "endpoint")
SCOPES = ("code", "raw", "strings", "query")

# The relation and edge class a produced construction is wired to its owner by.
EDGE_FOR_KIND = {
    "external": ("uses", "boundary"),
    "store": ("persists-to", "lifecycle"),
    "resource": ("holds", "lifecycle"),
    "endpoint": ("exposes", "structure"),
}


@dataclass(frozen=True)
class Rule:
    """One pass of the grammar.

    ``scope`` says what ``pattern`` is matched against: ``code`` (comments and
    string contents blanked, positions preserved), ``raw`` (comments blanked
    only, so string arguments are visible), ``strings`` (each string literal's
    contents on its own) or ``query`` (a tree-sitter query whose ``@site``
    capture is the citation and optional ``@label`` capture the label).
    ``produces`` names the construction the rule puts on the map, or nothing
    when the rule only cites (a citation still maps the enclosing declaration).
    """

    id: str
    description: str
    family: str
    scope: str = "code"
    pattern: str = ""
    produces: Optional[str] = None
    sub_kind: str = ""
    label_group: int = 0
    relation: Optional[str] = None
    edge_class: Optional[str] = None
    label_prefix: str = ""

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"rule {self.id}: family {self.family!r} is not one of {FAMILIES}")
        if self.scope not in SCOPES:
            raise ValueError(f"rule {self.id}: scope {self.scope!r} is not one of {SCOPES}")
        if self.produces is not None and self.produces not in NODE_KINDS:
            raise ValueError(f"rule {self.id}: produces {self.produces!r} is not one of {NODE_KINDS}")
        if self.scope != "query":
            re.compile(self.pattern)

    @property
    def wiring(self) -> Tuple[str, str]:
        relation, edge_class = EDGE_FOR_KIND.get(self.produces or "", ("uses", "usage"))
        return (self.relation or relation, self.edge_class or edge_class)


@dataclass(frozen=True)
class Declaration:
    kind: str
    name: str
    line: int
    end_line: int

    @property
    def is_type(self) -> bool:
        return self.kind in TYPE_KINDS


@dataclass(frozen=True)
class Citation:
    rule: str
    line: int
    label: str = ""


@dataclass(frozen=True)
class ImportRecord:
    root: str
    line: int
    alias: str = ""


@dataclass
class LanguagePack:
    """A language's front end. Behaviour that differs per language is a
    callable; behaviour that is data is data."""

    name: str
    label: str
    extensions: Tuple[str, ...]
    grammar: str
    comment_types: frozenset
    string_types: frozenset
    stdlib: frozenset
    rules: Tuple[Rule, ...]
    census: Callable[[Any, bytes], List[Declaration]]
    imports: Callable[[Any, bytes], List[ImportRecord]]
    entrypoints: Callable[[Any, str, str], List[Tuple[int, str]]]
    local_roots: Callable[[Sequence[str], Dict[str, Any]], frozenset] = field(default=lambda rels, config: frozenset())
    type_kind: Callable[[Declaration], str] = field(default=lambda declaration: "type")
    enum_separator: str = "."
    typed_property_pattern: str = ""  # regex with groups (name, type) for `var x: Type` style declarations
    extra_edges: Optional[Callable[..., List[Tuple[str, str, str, str, int, str]]]] = None
    limitations: Tuple[str, ...] = ()
    grammar_by_extension: Dict[str, str] = field(default_factory=dict)  # an extension that parses with a sibling grammar (.tsx → tsx)

    def __post_init__(self) -> None:
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"pack {self.name}: duplicate rule ids")
        for rule in self.rules:
            if not rule.id.startswith(f"{self.name}."):
                raise ValueError(f"pack {self.name}: rule {rule.id} must be prefixed {self.name}.")

    def grammar_for(self, path: str) -> str:
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        return self.grammar_by_extension.get(suffix, self.grammar)

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)


# ── Generic helpers packs share ──────────────────────────────────────────────


def node_line(node: Any) -> int:
    return int(node.start_point.row) + 1


def node_end_line(node: Any) -> int:
    return int(node.end_point.row) + 1


def node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def walk(node: Any) -> Iterable[Any]:
    """Every node, depth first, in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def child_of_type(node: Any, *types: str) -> Optional[Any]:
    for child in node.children:
        if child.type in types:
            return child
    return None


def descendant_of_type(node: Any, *types: str) -> Optional[Any]:
    for candidate in walk(node):
        if candidate.type in types:
            return candidate
    return None


def strip_quotes(text: str) -> str:
    """A string literal's contents: the outer delimiters removed, whatever they are."""
    text = text.strip()
    for opener, closer in (('"""', '"""'), ("'''", "'''"), ('r"', '"'), ('b"', '"'), ('f"', '"'), ('"', '"'), ("'", "'"), ("`", "`")):
        if text.startswith(opener) and text.endswith(closer) and len(text) >= len(opener) + len(closer):
            return text[len(opener):len(text) - len(closer)]
    return text


def mask(source: str, root: Any, comment_types: frozenset, string_types: frozenset) -> Tuple[str, str, List[Tuple[int, str]]]:
    """Blank comments (and, for ``code``, string contents) while preserving
    every character position, so regex matches map back to lines. Returns
    (code, raw, strings) where strings is [(line, contents)]."""
    data = source.encode("utf-8")
    code = bytearray(data)
    raw = bytearray(data)
    strings: List[Tuple[int, str]] = []

    def blank(buffer: bytearray, start: int, end: int) -> None:
        for index in range(start, end):
            if buffer[index] not in (0x0A, 0x0D):
                buffer[index] = 0x20

    for node in walk(root):
        if node.type in comment_types:
            blank(code, node.start_byte, node.end_byte)
            blank(raw, node.start_byte, node.end_byte)
        elif node.type in string_types:
            # Only the outermost string node counts; nested string nodes (interpolations) are inside it.
            parent = node.parent
            nested = False
            while parent is not None:
                if parent.type in string_types:
                    nested = True
                    break
                parent = parent.parent
            if nested:
                continue
            strings.append((node_line(node), strip_quotes(node_text(node, data))))
            blank(code, node.start_byte, node.end_byte)
    return code.decode("utf-8", errors="replace"), raw.decode("utf-8", errors="replace"), strings


def line_of_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def regex_citations(rule: Rule, code: str, raw: str, strings: List[Tuple[int, str]]) -> List[Citation]:
    """Where a regex rule fires, with the label the rule asks for."""
    found: List[Citation] = []
    pattern = re.compile(rule.pattern, re.MULTILINE)
    if rule.scope == "strings":
        for line, contents in strings:
            for match in pattern.finditer(contents):
                label = match.group(rule.label_group) if rule.label_group else match.group(0)
                found.append(Citation(rule.id, line, (rule.label_prefix + (label or "")).strip()))
        return found
    text = code if rule.scope == "code" else raw
    for match in pattern.finditer(text):
        label = match.group(rule.label_group) if rule.label_group else match.group(0)
        found.append(Citation(rule.id, line_of_offset(text, match.start()), (rule.label_prefix + (label or "")).strip()))
    return found


def query_citations(rule: Rule, language: Any, root: Any, source: bytes) -> List[Citation]:
    """Where a tree-sitter query rule fires: one citation per match at ``@site``."""
    from tree_sitter import Query  # imported lazily: the module documents itself without the dependency

    query = Query(language, rule.pattern)
    try:  # py-tree-sitter ≥ 0.25 runs queries through a cursor; older releases on the query itself
        from tree_sitter import QueryCursor

        matches = QueryCursor(query).matches(root)
    except ImportError:  # pragma: no cover - depends on the installed runtime
        matches = query.matches(root)
    found: List[Citation] = []
    for _, captures in matches:
        sites = captures.get("site") or []
        if not sites:
            continue
        labels = captures.get("label") or []
        label = node_text(labels[0], source) if labels else ""
        found.append(Citation(rule.id, node_line(sites[0]), (rule.label_prefix + label).strip()))
    return found


def enclosing_type(declarations: Sequence[Declaration], line: int) -> Optional[Declaration]:
    """The innermost type-kind declaration whose body contains ``line``."""
    best: Optional[Declaration] = None
    for declaration in declarations:
        if not declaration.is_type or not (declaration.line <= line <= declaration.end_line):
            continue
        if best is None or declaration.line > best.line or (declaration.line == best.line and declaration.end_line < best.end_line):
            best = declaration
    return best
