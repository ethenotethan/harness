"""What the map is short: completeness invariants the compiler proves or fails.

Every pass reads a subset of a wider shape — a route with a literal path out of
every route registration, a client call whose path a declared route answers, a
call on a store that lands on a member the store declares, a package import a
declared external system claims. The remainder is counted here, per site, and
turned into invariants with ``status: holds`` or ``status: violated`` and the
unparsed sites attached, so a silently short map is visible in the model
instead of in a reviewer's head. Nothing here fails the build: the ratchet
(``tools/architecture_ratchet.py``) refuses a change that grows the remainder.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .calls import CallIndex, excerpt

PARAM_SEGMENT_RE = re.compile(r"^(?::\w+|\{[^}]*\}|<[^>]*>|\$\{[^}]*\}|\*.*|%s|%d)$")
URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]*(/.*)?$", re.IGNORECASE)

ROUTES_LITERAL = "routes-declared-literally"
STORE_CALLS = "store-calls-resolve"
CLIENT_PATHS = "client-paths-resolve"
VENDORS = "vendors-declared"


def normalize_path(literal: str) -> Optional[str]:
    """The path a string literal spells: a path-only literal as is, a URL's path,
    nothing for anything else. Query strings are dropped."""
    text = literal.strip()
    match = URL_RE.match(text)
    if match:
        text = match.group(1) or "/"
    if not text.startswith("/"):
        return None
    return text.split("?", 1)[0].rstrip("/") or "/"


def segments(path: str) -> List[str]:
    return [s for s in path.split("/") if s]


def route_matches(client_path: str, route: str) -> bool:
    """Segment-wise equality with either side's parameter segments as wildcards
    (``:id``, ``{id}``, ``<int:id>``, ``${id}``, a trailing ``*``)."""
    left, right = segments(client_path), segments(route)
    if right and right[-1].startswith("*"):
        return left[:len(right) - 1] == right[:-1] or all(a == b or PARAM_SEGMENT_RE.match(a) or PARAM_SEGMENT_RE.match(b) for a, b in zip(left[:len(right) - 1], right[:-1]))
    if len(left) != len(right):
        return False
    return all(a == b or bool(PARAM_SEGMENT_RE.match(a)) or bool(PARAM_SEGMENT_RE.match(b)) for a, b in zip(left, right))


def invariant(identifier: str, kind: str, why: str, seen: int, unparsed: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": identifier, "kind": kind, "status": "holds" if not unparsed else "violated", "checked": seen, "why": why,
            "unparsed": sorted(unparsed, key=lambda u: (u["path"], u["line"], u.get("excerpt", "")))}


def routes_declared_literally(extractions: Sequence[Any], rules_by_id: Dict[str, Any]) -> Dict[str, Any]:
    seen = 0
    unparsed: List[Dict[str, Any]] = []
    for ex in extractions:
        for rule_id, lines in ex.probes.items():
            rule = rules_by_id.get(rule_id)
            if rule is None or rule.produces != "endpoint":
                continue
            cited = {c.line for c in ex.citations if c.rule == rule_id}
            seen += len(lines)
            unparsed.extend({"path": ex.rel, "line": line, "excerpt": excerpt(ex.text, line), "rule": rule_id} for line in lines if line not in cited)
    return invariant(ROUTES_LITERAL, "routes_declared_literally",
                     "Every route registration the packs recognise carries a literal path, so every endpoint of the service is on the map; a registration whose path is computed is listed as unparsed.",
                     seen, unparsed)


def store_calls_resolve(index: CallIndex) -> Dict[str, Any]:
    seen, unparsed = index.store_calls()
    return invariant(STORE_CALLS, "store_calls_resolve",
                     "Every call on a receiver typed as a store, or naming a store module, lands on a member that store declares; a call to a member the store does not have is a new idiom or a typo the map would otherwise be silently short of.",
                     seen, unparsed)


def client_paths_resolve(extractions: Sequence[Any], rules_by_id: Dict[str, Any], routes: Sequence[str],
                         composed: Sequence[Tuple[str, int, str]] = ()) -> Dict[str, Any]:
    """Client call sites whose literal path falls under a prefix the service's own
    routes answer: each must match a declared route. ``composed`` adds the paths
    a client spells through a wrapper member (path, line, composed path)."""
    first_segments = {segments(route)[0] for route in routes if segments(route)}
    sites: List[Tuple[str, int, str]] = []
    for ex in extractions:
        client_lines = {c.line for c in ex.citations if (rules_by_id.get(c.rule) is not None and rules_by_id[c.rule].sub_kind == "http_client")}
        for line, contents in ex.strings:
            if line in client_lines:
                path = normalize_path(contents)
                if path is not None:
                    sites.append((ex.rel, line, path))
    sites.extend(composed)
    seen = 0
    unparsed: List[Dict[str, Any]] = []
    texts = {ex.rel: ex.text for ex in extractions}
    for path, line, client_path in sites:
        parts = segments(client_path)
        if not parts or parts[0] not in first_segments:
            continue
        seen += 1
        if not any(route_matches(client_path, route) for route in routes):
            unparsed.append({"path": path, "line": line, "excerpt": excerpt(texts.get(path, ""), line), "client_path": client_path})
    return invariant(CLIENT_PATHS, "client_paths_resolve",
                     "Every path a client in this service spells under a prefix the service's own routes answer matches a declared route; a path no route answers is a dead call or a route the map is short.",
                     seen, unparsed)


def vendors_declared(extractions: Sequence[Any], absorbing: Dict[str, str]) -> Dict[str, Any]:
    seen = 0
    unparsed: List[Dict[str, Any]] = []
    for ex in extractions:
        for root_name, line in ex.external_roots:
            seen += 1
            if root_name not in absorbing:
                unparsed.append({"path": ex.rel, "line": line, "excerpt": excerpt(ex.text, line), "package": root_name})
    return invariant(VENDORS, "vendors_declared",
                     "Every external package the service imports is claimed by a declared external system (a signature naming the package), so the boundary is described by a person rather than inferred from import lists; an unclaimed package is listed with the import that pulls it in.",
                     seen, unparsed)


def summary(invariants: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    return {i["id"]: {"seen": i["checked"], "parsed": i["checked"] - len(i["unparsed"]), "unparsed": len(i["unparsed"])}
            for i in invariants if "unparsed" in i}
