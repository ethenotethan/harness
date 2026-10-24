"""Usage rules over the call index: what a handler reaches, what a client spells.

``usage.injected_method`` — a route's handler calls a method on something handed
to it rather than declared beside it (a module-level store object, a field the
constructor was given, a module imported under a name) and the mechanism is in
that method's body or further down. The index follows the handler's calls by
name, up to four hops, to the store sites and external systems the visited
bodies carry; the endpoint gets an edge to each with the chain of members it
went through. Direct citations in the handler's own body stay attributed to the
handler's owner as before; this rule adds what is one or more calls away.

``usage.client_wrapper`` — a client type's member whose body spells a path as a
literal prefix and one of its own parameters (``f"/api/items{path}"``,
``` `/api/items${path}` ```) is a path wrapper: every sibling member that calls it
with a literal spells prefix + literal. The composed path is matched against
the service's routes like any other client path, and the type invokes the
endpoint it lands on.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .calls import CallIndex, Member, balanced
from .completeness import normalize_path, route_matches

INJECTED_METHOD = "usage.injected_method"
CLIENT_WRAPPER = "usage.client_wrapper"
IDENTIFIER_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)(?!\s*[\(\w$])")
MAX_HOPS = 4


def handler_member(index: CallIndex, ex: Any, line: int) -> Optional[Member]:
    """The member a route registration at ``line`` hands requests to: the
    decorated function (packs with ``handler_line``), else a synthetic member
    over the registration's own argument span, so inline handlers and handlers
    passed by name both start the walk."""
    if ex.pack.handler_line is not None:
        start = ex.pack.handler_line(ex.tree_root, ex.source, line)
        if start is not None:
            return index.member_at(ex.rel, start)
        return None
    offset = _line_offset(ex.code, line)
    open_paren = ex.code.find("(", offset, ex.code.find("\n", offset) if ex.code.find("\n", offset) >= 0 else len(ex.code))
    if open_paren < 0:
        return None
    end = balanced(ex.code, open_paren)
    synthetic = Member(f"{ex.rel}#@{line}", ex.rel, ex.component, ex.pack.name, "", "", line, line + ex.code.count("\n", open_paren, end),
                       ex.code[open_paren:end], ex.raw[open_paren:end], synthetic=True)
    enclosing = index.member_at(ex.rel, line)
    if enclosing is not None:
        synthetic.bindings = dict(enclosing.bindings)
        synthetic.param_types = dict(enclosing.param_types)
    index.members[synthetic.id] = synthetic
    return synthetic


def _line_offset(text: str, line: int) -> int:
    position = 0
    for _ in range(line - 1):
        position = text.find("\n", position) + 1
        if position <= 0:
            return len(text)
    return position


def named_handlers(index: CallIndex, member: Member) -> List[str]:
    """Functions a registration names without calling them (``app.get("/x", listItems)``)."""
    imports = index.local_imports.get(member.path, {})
    found: List[str] = []
    for match in IDENTIFIER_RE.finditer(member.code):
        name = match.group(1)
        target = index.module_functions.get((member.path, name))
        if target is None and name in imports and imports[name].member:
            target = index.module_functions.get((imports[name].module, imports[name].member))
        if target and target not in found:
            found.append(target)
    return found


def injected_method_edges(index: CallIndex, endpoints: Sequence[Tuple[str, Any, int]], sites: Dict[Tuple[str, int, str], str],
                          store_rules: frozenset, signature_rule: str) -> List[Dict[str, Any]]:
    """For each (endpoint id, extraction, registration line): the store sites and
    external systems its handler reaches one or more calls away, each with the
    chain of members and the line of the first hop in the handler."""
    found: List[Dict[str, Any]] = []
    for endpoint_id, ex, line in endpoints:
        start = handler_member(index, ex, line)
        if start is None:
            continue
        starts = [start.id] + named_handlers(index, start)
        chains = index.closure(starts, MAX_HOPS)
        first_hop_line = {callee: call_line for callee, call_line in index.callees(start.id)}
        for member_id, chain in sorted(chains.items()):
            if len(chain) < 2 and member_id == start.id:
                continue
            member = index.members[member_id]
            member_ex = index.by_path[member.path]
            via = [index.members[m].label for m in chain[1:]] if chain[0] == start.id else [index.members[m].label for m in chain]
            hops = len(via)
            hop_line = first_hop_line.get(chain[1] if len(chain) > 1 else chain[0], line)
            for citation in member_ex.citations:
                if not (member.line <= citation.line <= member.end_line):
                    continue
                if citation.rule in store_rules and (member.path, citation.line, citation.rule) in sites:
                    found.append({"source": endpoint_id, "target": sites[(member.path, citation.line, citation.rule)], "relation": "uses", "edge_class": "usage",
                                  "via": via, "hops": hops, "path": ex.rel, "line": hop_line})
                elif citation.rule == signature_rule:
                    found.append({"source": endpoint_id, "target": f"external:{citation.label}", "relation": "touches", "edge_class": "boundary",
                                  "via": via, "hops": hops, "path": ex.rel, "line": hop_line})
    return found


def client_wrapper_edges(index: CallIndex, routes: Dict[str, str], type_id: Callable[[str, str], str]) -> Tuple[List[Dict[str, Any]], List[Tuple[str, int, str]], List[Tuple[str, int, int]]]:
    """(edges, composed paths, wrapper spans): for every type with a wrapper
    member, the routes its sibling members reach through it, every composed path
    (for the client-paths invariant) whether or not a route answers it, and the
    (path, line, end line) of each wrapper body — its own prefix literal is not
    a client path."""
    edges: List[Dict[str, Any]] = []
    composed: List[Tuple[str, int, str]] = []
    spans: List[Tuple[str, int, int]] = []
    for (component, type_name), members in sorted(index.members_of_type.items()):
        if len(members) < 2:
            continue
        sample = index.members[next(iter(members.values()))]
        pack = index.by_path[sample.path].pack
        if pack.wrapper_prefix is None or pack.literal_calls is None:
            continue
        wrappers: Dict[str, str] = {}
        for name, member_id in members.items():
            member = index.members[member_id]
            prefix = pack.wrapper_prefix(member.raw, member.params)
            if prefix:
                wrappers[name] = prefix
                spans.append((member.path, member.line, member.end_line))
        if not wrappers:
            continue
        module_raw = index.by_path[sample.path].raw
        for name, member_id in sorted(members.items()):
            member = index.members[member_id]
            for wrapper_name, prefix in sorted(wrappers.items()):
                if wrapper_name == name:
                    continue
                for literal, offset in pack.literal_calls(member.raw, wrapper_name, module_raw):
                    path = normalize_path(prefix + literal)
                    if path is None:
                        continue
                    line = member.line + member.raw.count("\n", 0, offset)
                    composed.append((member.path, line, path))
                    for route_label, endpoint_id in sorted(routes.items()):
                        if route_matches(path, route_label):
                            edges.append({"source": type_id(component, type_name), "target": endpoint_id, "relation": "invokes", "edge_class": "interplay",
                                          "via": [f"{type_name}.{wrapper_name}"], "composed": path, "path": member.path, "line": line})
    return edges, composed, spans
