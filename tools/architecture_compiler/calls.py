"""The call index: who calls what, by name, across the files of a service.

Language-independent, built from what the packs already give the core — the
declaration census (members with body ranges), the masked code, the local
imports a file makes and three facts a pack states (how a method refers to its
own object, what constructing a type looks like, and how a file names another
file). On it the core proves ``store-calls-resolve`` (every call on a receiver
typed as a store lands on a member that store declares), follows a route's
handler to the stores and systems it reaches through injected dependencies
(``usage.injected_method``), composes the paths a client spells through a
wrapper member (``usage.client_wrapper``) and finds the state a loop writes.

Three precision guards keep the index from over-reaching:

1. ``self.<field>.<sub>….m(`` resolves only through fields whose type is
   known (annotated, or constructed in the type's own body) and only to the
   class the chain ends on — never to every class that happens to define ``m``.
2. ``new X(…).m(`` / ``X(…).m(`` resolves to ``X.m`` and nothing else.
3. A call whose receiver cannot be typed resolves to a method name only when
   exactly one store declares that name; a name several stores share is left
   unresolved rather than attributed to all of them.

A type whose header names a base the service does not declare (a framework or
library class) may inherit members the index cannot see; a miss on such a type
is not counted against it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .packs.base import Declaration, LocalImport

CALL_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)((?:\.[A-Za-z_$][\w$]*)*)\.([A-Za-z_$][\w$]*)\s*\(")
BARE_CALL_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
BINDING_RE = re.compile(r"(?<![\w$.])(?:(?:const|let|var)\s+)?([A-Za-z_$][\w$]*)\s*(?::\s*([A-Z][\w$]*)[^=\n]*)?=\s*(?:new\s+)?([A-Z][\w$]*)\s*\(")
ANNOTATED_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\??:\s*(?:Optional\[|Readonly<)?([A-Z][\w$]*)")
FIELD_DECL_RE = re.compile(r"^[ \t]+(?:(?:private|public|protected|readonly|static|declare|override|accessor)\s+)*(#?[A-Za-z_$][\w$]*)\s*[?!]?\s*:\s*(?:Optional\[|Readonly<)?([A-Z][\w$]*)\b", re.MULTILINE)
MODULE_BINDING_RE = re.compile(r"^(?:export\s+)?(?:(?:const|let|var)\s+)?([A-Za-z_$][\w$]*)\s*(?::\s*[^=\n]+?)?\s*=\s*(?:new\s+)?([A-Z][\w$]*)\s*\(", re.MULTILINE)
CLASS_HEADER_RE = re.compile(r"\b(?:class|struct|actor)\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>\s*)?(?:extends\s+([\w$.]+)|\(([^)]*)\)|:\s*([\w$.]+))?")


@dataclass
class Member:
    """A function or method with its body, or a file's top-level statements
    (``owner`` empty, ``name`` empty): the unit the index follows calls between."""

    id: str
    path: str
    component: str
    language: str
    owner: str
    name: str
    line: int
    end_line: int
    code: str          # body with comments and string contents blanked
    raw: str           # body with comments blanked, strings visible
    nested: bool = False
    synthetic: bool = False  # a registration's argument span standing in for an inline handler; not a declaration
    params: Set[str] = field(default_factory=set)
    param_types: Dict[str, str] = field(default_factory=dict)
    bindings: Dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else (self.name or "<module>")


@dataclass(frozen=True)
class Call:
    chain: Tuple[str, ...]  # receiver path, empty for a bare call
    method: str
    offset: int             # into the member's code


def excerpt(text: str, line: int) -> str:
    lines = text.split("\n")
    return lines[line - 1].strip()[:160] if 0 < line <= len(lines) else ""


def _offset(text: str, line: int) -> int:
    if line <= 1:
        return 0
    position = -1
    for _ in range(line - 1):
        position = text.find("\n", position + 1)
        if position < 0:
            return len(text)
    return position + 1


def balanced(text: str, open_at: int) -> int:
    """The offset just past the bracket that closes the one at ``open_at``
    (``(``, ``[`` or ``{``), or len(text) when it never closes."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    closer = pairs.get(text[open_at:open_at + 1])
    if closer is None:
        return open_at
    depth = 0
    for index in range(open_at, len(text)):
        char = text[index]
        if char in pairs:
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def signature_params(code: str) -> Tuple[Set[str], Dict[str, str]]:
    """Parameter names (and the type each annotated one names) of a member
    whose text starts at its declaration line."""
    open_paren = code.find("(")
    if open_paren < 0:
        return set(), {}
    inside = code[open_paren + 1:balanced(code, open_paren) - 1]
    names: Set[str] = set()
    types: Dict[str, str] = {}
    depth = 0
    start = 0
    parts: List[str] = []
    for index, char in enumerate(inside):
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(inside[start:index])
            start = index + 1
    parts.append(inside[start:])
    for part in parts:
        match = re.match(r"\s*(?:(?:private|public|protected|readonly|static)\s+)*(?:\.\.\.|\*\*?)?([A-Za-z_$][\w$]*)", part)
        if not match:
            continue
        names.add(match.group(1))
        annotated = ANNOTATED_RE.search(part)
        if annotated and annotated.group(1) == match.group(1):
            types[match.group(1)] = annotated.group(2)
    return names, types


class CallIndex:
    def __init__(self, extractions: Sequence[Any], resolve_type: Callable[[str, Any], Optional[str]], store_rules: FrozenSet[str],
                 store_type_ids: FrozenSet[str]) -> None:
        self.extractions = list(extractions)
        self.by_path: Dict[str, Any] = {ex.rel: ex for ex in self.extractions}
        self.resolve_type = resolve_type
        self.members: Dict[str, Member] = {}
        self.module_functions: Dict[Tuple[str, str], str] = {}
        self.members_of_type: Dict[Tuple[str, str], Dict[str, str]] = {}   # (component, type) → name → member id
        self.module_bindings: Dict[str, Dict[str, str]] = {}
        self.local_imports: Dict[str, Dict[str, LocalImport]] = {}
        self.type_bases: Dict[Tuple[str, str], List[str]] = {}
        self.type_lines: Dict[Tuple[str, str], Tuple[str, int, int]] = {}
        self._field_types: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
        self._callees: Dict[str, List[Tuple[str, int]]] = {}
        all_rels = frozenset(self.by_path)
        for ex in self.extractions:
            self._index_file(ex, all_rels)
        # Store types: a type the pack already puts on the map as a store, or one whose body
        # carries a store-family citation. Store modules: every file that carries one — a call
        # on the module's name (`store.save(`) must land on a function the module declares.
        self.store_types: Set[Tuple[str, str]] = set()
        self.store_modules: Set[str] = set()
        for ex in self.extractions:
            store_lines = [c.line for c in ex.citations if c.rule in store_rules]
            if not store_lines:
                continue
            self.store_modules.add(ex.rel)
            for declaration in ex.declarations:
                if declaration.is_type and any(declaration.line <= line <= declaration.end_line for line in store_lines):
                    self.store_types.add((ex.component, declaration.name))
        for type_id in store_type_ids:
            _, component, name = type_id.split(":", 2)
            self.store_types.add((component, name))

    # ── Building ─────────────────────────────────────────────────────────────

    def _index_file(self, ex: Any, all_rels: FrozenSet[str]) -> None:
        pack = ex.pack
        code, raw = ex.code, ex.raw
        types = [d for d in ex.declarations if d.is_type]
        functions = [d for d in ex.declarations if d.kind == "function"]

        def enclosing(declaration: Declaration, candidates: Iterable[Declaration]) -> Optional[Declaration]:
            best = None
            for other in candidates:
                if other is declaration or not (other.line <= declaration.line and declaration.end_line <= other.end_line):
                    continue
                if other.line == declaration.line and other.end_line == declaration.end_line:
                    continue
                if best is None or other.line > best.line:
                    best = other
            return best

        for declaration in types:
            header = CLASS_HEADER_RE.search(code, _offset(code, declaration.line), _offset(code, declaration.line + 1))
            bases: List[str] = []
            if header and header.group(1) == declaration.name:
                raw_bases = header.group(2) or header.group(3) or header.group(4) or ""
                bases = [b.strip().split("(")[0].split("<")[0].split("=")[0].strip() for b in raw_bases.split(",")]
                bases = [b.split(".")[-1] for b in bases if b and b[0].isalpha()]
            self.type_bases[(ex.component, declaration.name)] = bases
            self.type_lines[(ex.component, declaration.name)] = (ex.rel, declaration.line, declaration.end_line)
            self.members_of_type.setdefault((ex.component, declaration.name), {})

        blanked = code
        for declaration in functions:
            owner_type = enclosing(declaration, types)
            outer_function = enclosing(declaration, functions)
            nested = outer_function is not None and (owner_type is None or outer_function.line > owner_type.line)
            start, end = _offset(code, declaration.line), _offset(code, declaration.end_line + 1)
            owner = owner_type.name if owner_type else ""
            if nested and outer_function is not None:
                owner = f"{owner}.{outer_function.name}" if owner else outer_function.name
            member_id = f"{ex.rel}#{owner}.{declaration.name}" if owner else f"{ex.rel}#{declaration.name}"
            if member_id in self.members:  # several functions of one name in a file (decorated handlers named `_`): each is its own member
                member_id = f"{member_id}@{declaration.line}"
            member = Member(member_id, ex.rel, ex.component, pack.name, owner, declaration.name, declaration.line, declaration.end_line,
                            code[start:end], raw[start:end], nested=nested)
            member.params, member.param_types = signature_params(member.code)
            member.bindings = {m.group(1): (m.group(2) or m.group(3)) for m in BINDING_RE.finditer(member.code)}
            self.members[member.id] = member
            if owner_type is not None and not nested:
                self.members_of_type[(ex.component, owner_type.name)].setdefault(declaration.name, member.id)
            elif not nested:
                self.module_functions.setdefault((ex.rel, declaration.name), member.id)
            if not nested and owner_type is None:
                blanked = blanked[:start] + re.sub(r"[^\n]", " ", blanked[start:end]) + blanked[end:]
        for declaration in types:
            start, end = _offset(code, declaration.line), _offset(code, declaration.end_line + 1)
            blanked = blanked[:start] + re.sub(r"[^\n]", " ", blanked[start:end]) + blanked[end:]
        top = Member(f"{ex.rel}#", ex.rel, ex.component, pack.name, "", "", 1, ex.line_count, blanked, raw)
        top.bindings = {m.group(1): (m.group(2) or m.group(3)) for m in BINDING_RE.finditer(blanked)}
        self.members[top.id] = top
        self.module_bindings[ex.rel] = {m.group(1): m.group(2) for m in MODULE_BINDING_RE.finditer(code)}
        imports: Dict[str, LocalImport] = {}
        if pack.local_imports is not None:
            for record in pack.local_imports(ex.tree_root, ex.source, ex.rel, all_rels):
                imports.setdefault(record.local, record)
        self.local_imports[ex.rel] = imports

    # ── Types ────────────────────────────────────────────────────────────────

    def type_members(self, component: str, type_name: str) -> Dict[str, str]:
        return self.members_of_type.get((component, type_name), {})

    def resolve_type_key(self, name: str, ex: Any) -> Optional[Tuple[str, str]]:
        type_id = self.resolve_type(name, ex)
        if not type_id:
            return None
        _, component, type_name = type_id.split(":", 2)
        return (component, type_name)

    def field_types(self, component: str, type_name: str) -> Dict[str, List[str]]:
        """The type each field of a type holds: constructed in the body
        (``self.f = X(…)``, ``this.f = new X(…)``), annotated on the field, or
        annotated on a constructor parameter the body assigns to the field."""
        key = (component, type_name)
        if key in self._field_types:
            return self._field_types[key]
        found: Dict[str, List[str]] = {}
        location = self.type_lines.get(key)
        self._field_types[key] = found
        if location is None:
            return found
        path, line, end_line = location
        ex = self.by_path[path]
        body = ex.code[_offset(ex.code, line):_offset(ex.code, end_line + 1)]
        self_name = ex.pack.self_name

        def add(name: str, type_name_: str) -> None:
            if type_name_ and self.resolve_type(type_name_, ex) and type_name_ not in found.setdefault(name, []):
                found[name].append(type_name_)

        for match in FIELD_DECL_RE.finditer(body):
            add(match.group(1), match.group(2))
        if self_name:
            for match in re.finditer(r"(?<![\w$])" + re.escape(self_name) + r"\.(#?[A-Za-z_$][\w$]*)\s*(?::\s*(?:Optional\[)?([A-Z][\w$]*)[^=\n]*)?=\s*(?:new\s+)?([A-Z][\w$]*)\s*\(", body):
                add(match.group(1), match.group(2) or match.group(3))
            for match in re.finditer(r"(?<![\w$])" + re.escape(self_name) + r"\.(#?[A-Za-z_$][\w$]*)\s*:\s*(?:Optional\[)?([A-Z][\w$]*)", body):
                add(match.group(1), match.group(2))
            for member_id in self.type_members(component, type_name).values():
                member = self.members[member_id]
                for match in re.finditer(r"(?<![\w$])" + re.escape(self_name) + r"\.(#?[A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*(?:;|$)", member.code, re.MULTILINE):
                    if match.group(2) in member.param_types:
                        add(match.group(1), member.param_types[match.group(2)])
                # A constructor parameter with an accessibility modifier declares the field itself.
                signature_end = balanced(member.code, member.code.find("(")) if "(" in member.code else 0
                for match in re.finditer(r"\b(?:private|public|protected|readonly)\s+(?:readonly\s+)?([A-Za-z_$][\w$]*)\s*[?!]?\s*:\s*([A-Z][\w$]*)", member.code[:signature_end]):
                    add(match.group(1), match.group(2))
        return found

    def assigned_fields(self, component: str, type_name: str) -> Set[str]:
        """Names the type's body assigns as fields of its own object (``self.x = …``):
        calling one is a call through a callable field, not a missing member."""
        location = self.type_lines.get((component, type_name))
        if location is None:
            return set()
        path, line, end_line = location
        ex = self.by_path[path]
        if not ex.pack.self_name:
            return set()
        body = ex.code[_offset(ex.code, line):_offset(ex.code, end_line + 1)]
        return {m.group(1) for m in re.finditer(r"(?<![\w$])" + re.escape(ex.pack.self_name) + r"\.(#?[A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=(?!=)", body)}

    def has_foreign_base(self, component: str, type_name: str) -> bool:
        """Whether the type extends something the service does not declare, so
        members may be inherited from outside the index."""
        ex = self.by_path[self.type_lines[(component, type_name)][0]]
        return any(self.resolve_type_key(base, ex) is None for base in self.type_bases.get((component, type_name), []))

    def member_named(self, component: str, type_name: str, method: str, ex: Any, depth: int = 0) -> Optional[str]:
        """The member a type declares under ``method``, looking up declared bases."""
        found = self.type_members(component, type_name).get(method)
        if found or depth > 4:
            return found
        for base in self.type_bases.get((component, type_name), []):
            key = self.resolve_type_key(base, ex)
            if key:
                found = self.member_named(key[0], key[1], method, self.by_path[self.type_lines[key][0]], depth + 1)
                if found:
                    return found
        return None

    # ── Calls ────────────────────────────────────────────────────────────────

    @staticmethod
    def calls_in(code: str) -> List[Call]:
        found = [Call(tuple([m.group(1)] + [p for p in m.group(2).split(".") if p]), m.group(3), m.start()) for m in CALL_RE.finditer(code)]
        found.extend(Call((), m.group(1), m.start()) for m in BARE_CALL_RE.finditer(code))
        found.sort(key=lambda c: c.offset)
        return found

    def receiver(self, member: Member, chain: Sequence[str]) -> Tuple[List[Tuple[str, str]], Optional[str]]:
        """What a receiver chain denotes from inside ``member``: the (component,
        type) candidates it may be an instance of, or the module it names."""
        ex = self.by_path[member.path]
        head = chain[0]
        types: List[Tuple[str, str]] = []
        module: Optional[str] = None
        imports = self.local_imports.get(member.path, {})
        if ex.pack.self_name and head == ex.pack.self_name and member.owner:
            types = [(member.component, member.owner.split(".")[0])]
        elif head in member.bindings or head in member.param_types:
            key = self.resolve_type_key(member.bindings.get(head) or member.param_types[head], ex)
            types = [key] if key else []
        elif head in self.module_bindings.get(member.path, {}):
            key = self.resolve_type_key(self.module_bindings[member.path][head], ex)
            types = [key] if key else []
        elif head in imports:
            record = imports[head]
            if record.member:
                key = self.resolve_type_key(record.member, self.by_path[record.module])
                types = [key] if key else []
            else:
                module = record.module
        else:
            # `import a.b.c` binds a dotted name: the whole chain may be the module itself.
            dotted = ".".join(chain)
            if dotted in imports and not imports[dotted].member:
                return [], imports[dotted].module
            # A type's own name as receiver: a static call on a type this file declares or imports.
            key = self.resolve_type_key(head, ex) if head[:1].isupper() else None
            if key and (self.type_lines[key][0] == member.path or head in {i.member for i in imports.values()}):
                types = [key]
        for sub in chain[1:]:
            types = [self.resolve_type_key(t, self.by_path[self.type_lines[c][0]]) or (c[0], t) for c in types for t in self.field_types(c[0], c[1]).get(sub, [])]
            module = None
        return types, module

    def constructed_calls(self, member: Member) -> List[Tuple[str, str, int]]:
        """``new X(…).m(`` / ``X(…).m(``: (type name, method, offset) — guard 2."""
        ex = self.by_path[member.path]
        if not ex.pack.construct_pattern:
            return []
        found: List[Tuple[str, str, int]] = []
        for match in re.finditer(ex.pack.construct_pattern, member.code):
            end = balanced(member.code, match.end() - 1)
            chained = re.match(r"\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", member.code[end:end + 80])
            if chained:
                found.append((match.group(1), chained.group(1), match.start()))
        return found

    def callees(self, member_id: str) -> List[Tuple[str, int]]:
        """Every member a member's body calls by name, with the line of the call:
        bare calls to module functions and imported functions, chained calls
        through typed receivers, calls on constructed objects, and functions
        bound to local names (``threading.Thread(target=f)``) only when the
        caller asks for them through ``references``."""
        if member_id in self._callees:
            return self._callees[member_id]
        member = self.members[member_id]
        ex = self.by_path[member.path]
        imports = self.local_imports.get(member.path, {})
        found: List[Tuple[str, int]] = []

        def add(target: Optional[str], offset: int) -> None:
            if target and target != member_id:
                found.append((target, member.line + member.code.count("\n", 0, offset)))

        for call in self.calls_in(member.code):
            if not call.chain:
                local = self.module_functions.get((member.path, call.method))
                if not local and member.owner and f"{member.path}#{member.owner}.{call.method}" in self.members:
                    local = f"{member.path}#{member.owner}.{call.method}"
                if local:
                    add(local, call.offset)
                    continue
                record = imports.get(call.method)
                if record and record.member:
                    add(self.module_functions.get((record.module, record.member)), call.offset)
                continue
            types, module = self.receiver(member, call.chain)
            if module is not None:
                add(self.module_functions.get((module, call.method)), call.offset)
                continue
            for component, type_name in types:
                add(self.member_named(component, type_name, call.method, ex), call.offset)
            if not types and len(call.chain) == 1 and call.chain[0] not in (ex.pack.self_name,):
                # Guard 3: an untyped receiver lands on a method name only one store declares.
                candidates = sorted({self.type_members(c, t)[call.method] for (c, t) in self.store_types if call.method in self.type_members(c, t)})
                if len(candidates) == 1:
                    add(candidates[0], call.offset)
        for type_name, method, offset in self.constructed_calls(member):
            key = self.resolve_type_key(type_name, ex)
            if key:
                add(self.member_named(key[0], key[1], method, ex), offset)
        self._callees[member_id] = list(dict.fromkeys(found))
        return self._callees[member_id]

    def closure(self, start_ids: Sequence[str], max_hops: int = 4) -> Dict[str, List[str]]:
        """Every member reachable from the starts within ``max_hops`` calls, with
        the chain of member ids that reached it (starts map to themselves)."""
        chains: Dict[str, List[str]] = {s: [s] for s in start_ids if s in self.members}
        frontier = list(chains)
        for _ in range(max_hops):
            next_frontier: List[str] = []
            for member_id in frontier:
                for callee, _line in self.callees(member_id):
                    if callee not in chains:
                        chains[callee] = chains[member_id] + [callee]
                        next_frontier.append(callee)
            frontier = next_frontier
            if not frontier:
                break
        return chains

    def member_at(self, path: str, line: int) -> Optional[Member]:
        """The innermost declared function containing a line of a file."""
        best: Optional[Member] = None
        for member in self.members.values():
            if member.path == path and member.name and member.line <= line <= member.end_line and (best is None or member.line > best.line):
                best = member
        return best

    # ── Completeness: store calls ────────────────────────────────────────────

    def store_calls(self) -> Tuple[int, List[Dict[str, Any]]]:
        """Every call on a receiver typed as a store (or naming a store module):
        how many there are and the ones that land on no member the store declares."""
        seen = 0
        unparsed: List[Dict[str, Any]] = []
        for member in self.members.values():
            if member.synthetic:
                continue
            ex = self.by_path[member.path]
            for call in self.calls_in(member.code):
                if not call.chain:
                    continue
                types, module = self.receiver(member, call.chain)
                line = member.line + member.code.count("\n", 0, call.offset)
                if module is not None and module in self.store_modules:
                    seen += 1
                    if (module, call.method) not in self.module_functions:
                        unparsed.append({"path": member.path, "line": line, "excerpt": excerpt(ex.text, line), "receiver": module, "method": call.method})
                    continue
                store_hits = [key for key in types if key in self.store_types]
                if not store_hits:
                    continue
                seen += 1
                if not any(self.member_named(c, t, call.method, ex) or self.has_foreign_base(c, t) or call.method in self.assigned_fields(c, t) for c, t in store_hits):
                    unparsed.append({"path": member.path, "line": line, "excerpt": excerpt(ex.text, line), "receiver": ".".join(f"{c}:{t}" for c, t in store_hits), "method": call.method})
        unparsed.sort(key=lambda u: (u["path"], u["line"], u["method"]))
        return seen, unparsed
