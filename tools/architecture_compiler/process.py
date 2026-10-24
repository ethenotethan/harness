"""What a process does for its life, from its entry point: the loops it runs,
the collections it keeps, the tables it declares.

The entry point's runtime import closure (every file reachable through local
imports) is the process. In it:

``launch.loop`` — a paced ``while`` at the entry's own level (module level, or in
a function the entry file's top-level code calls) is a tick: a periodic task
named for its holder, with the interval expression and the calls its body makes
in source order. Source order is the only claim; what the loop *means* is the
model's to write.

``launch.starter`` — a function handed to a thread, timer or task anywhere in
the closure whose body holds a paced loop, or a loop-holding function a module
calls at its top level, is a tick too: work that runs for the process's life
once started. A loop inside a closure handed out per job (not a task target,
not called at module level) is not a starter.

``state.resident`` — a collection a module or class in the closure initialises
and writes is state the process keeps; a class field counts only where the
class is constructed outside its own body. The entry point keeps it, and so
does every tick whose call closure writes it (the chain on the edge).

``schema.table`` / ``schema.references`` — tables declared in the tree and the
foreign keys between them, with columns and delete rule.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .calls import CallIndex, Member

MAX_HOPS = 4


def runtime_closure(index: CallIndex, entry_path: str) -> List[str]:
    """Every file the entry file loads through local imports, the entry first."""
    seen = [entry_path]
    frontier = [entry_path]
    while frontier:
        next_frontier: List[str] = []
        for path in frontier:
            for record in index.local_imports.get(path, {}).values():
                if record.module not in seen:
                    seen.append(record.module)
                    next_frontier.append(record.module)
        frontier = next_frontier
    return seen


def _task_targets(index: CallIndex, path: str) -> List[Tuple[str, int, Member]]:
    """(target name, line, member it resolves to) for every task starter in a file."""
    ex = index.by_path[path]
    found: List[Tuple[str, int, Member]] = []
    for pattern in ex.pack.task_starter_patterns:
        for match in re.finditer(pattern, ex.code):
            line = ex.code.count("\n", 0, match.start()) + 1
            name = match.group(1).split(".")[-1]
            starter = index.member_at(path, line)
            candidates = [f"{path}#{starter.label}.{name}" if starter else "", f"{path}#{name}"]
            if starter and starter.owner:
                candidates.insert(0, f"{path}#{starter.owner}.{name}")
            target = next((index.members[c] for c in candidates if c in index.members), None)
            if target is not None:
                found.append((name, line, target))
    return found


def ticks_for_entry(index: CallIndex, entry_path: str, closure: Sequence[str]) -> List[Dict[str, Any]]:
    """The ticks a process runs: its own paced loops and the starters in its closure."""
    found: List[Dict[str, Any]] = []
    seen_loops: Set[Tuple[str, int]] = set()

    def add(path: str, loop: Dict[str, Any], holder: Optional[Member], sub_kind: str, started_at: Optional[Tuple[str, int]]) -> None:
        if (path, loop["line"]) in seen_loops:
            return
        seen_loops.add((path, loop["line"]))
        found.append({"path": path, "line": loop["line"], "end_line": loop["end_line"], "holder": holder, "label": "module loop" if holder is None else f"{holder.label} loop",
                      "sub_kind": sub_kind, "interval": loop["interval"], "pacing": loop["pacing"], "condition": loop["condition"], "steps": loop["steps"], "started_at": started_at})

    def paced_loops(path: str) -> List[Dict[str, Any]]:
        """The file's unbounded paced loops, outermost only: a paced loop nested in
        another is the same tick."""
        ex = index.by_path[path]
        loops = [l for l in (ex.pack.loops(ex.tree_root, ex.source, ex.code) if ex.pack.loops else []) if not l["bounded"]]
        return [l for l in loops if not any(o is not l and o["line"] <= l["line"] and l["end_line"] <= o["end_line"] for o in loops)]

    top = index.members.get(f"{entry_path}#")
    top_callees = {callee for callee, _ in index.callees(top.id)} if top else set()
    for loop in paced_loops(entry_path):
        holder = index.member_at(entry_path, loop["line"])
        if holder is None or (not holder.nested and not holder.owner and holder.id in top_callees):
            add(entry_path, loop, holder, "loop", None)
    for path in closure:
        by_holder: Dict[str, List[Dict[str, Any]]] = {}
        for loop in paced_loops(path):
            holder = index.member_at(path, loop["line"])
            if holder is not None:
                by_holder.setdefault(holder.id, []).append(loop)
        if not by_holder:
            continue
        for _name, line, target in _task_targets(index, path):
            for loop in by_holder.get(target.id, []):
                add(path, loop, target, "starter", (path, line))
        # A loop-holding function the module calls at its top level runs when the module loads —
        # unless the module is an entry point itself, whose top level is another process's.
        module_top = index.members.get(f"{path}#")
        if module_top is not None and path != entry_path and not index.by_path[path].entrypoints:
            for callee, line in index.callees(module_top.id):
                for loop in by_holder.get(callee, []):
                    add(path, loop, index.members[callee], "starter", (path, line))
    found.sort(key=lambda t: (t["path"], t["line"]))
    return found


def resident_state_in(index: CallIndex, closure: Sequence[str]) -> List[Dict[str, Any]]:
    """The collections modules and constructed classes in the closure keep."""
    found: List[Dict[str, Any]] = []
    for path in closure:
        ex = index.by_path[path]
        if not ex.pack.resident_state:
            continue
        for state in ex.pack.resident_state(ex.tree_root, ex.source, ex.code):
            if state["holder_kind"] == "class" and not _constructed_outside(index, ex.component, state["holder"], closure):
                continue
            found.append({**state, "path": path, "component": ex.component})
    return found


def _constructed_outside(index: CallIndex, component: str, type_name: str, closure: Sequence[str]) -> bool:
    location = index.type_lines.get((component, type_name))
    for path in closure:
        ex = index.by_path[path]
        if not ex.pack.construct_pattern:
            continue
        for match in re.finditer(ex.pack.construct_pattern, ex.code):
            if match.group(1) != type_name:
                continue
            line = ex.code.count("\n", 0, match.start()) + 1
            if location is None or path != location[0] or not (location[1] <= line <= location[2]):
                return True
    return False


def writers_of(index: CallIndex, state: Dict[str, Any], tick: Dict[str, Any]) -> Optional[List[str]]:
    """The chain of members from a tick's holder to the first one that writes the
    state, or None when nothing in the tick's reach does."""
    holder = tick["holder"]
    if holder is None:
        return None
    if state["holder_kind"] == "module":
        pattern = re.compile(r"(?<![\w$.])" + re.escape(state["name"]) + r"(?:\s*\[[^\]\n]*\]\s*=(?!=)|\.(?:append|add|pop|update|setdefault|clear|extend|remove|discard|popitem|insert|appendleft|popleft|set|delete|push|splice)\s*\()|\bdel\s+" + re.escape(state["name"]) + r"\s*\[")
    else:
        self_name = index.by_path[state["path"]].pack.self_name or "self"
        pattern = re.compile(r"\b" + re.escape(self_name) + r"\." + re.escape(state["name"]) + r"(?:\s*\[[^\]\n]*\]\s*=(?!=)|\.(?:append|add|pop|update|setdefault|clear|extend|remove|discard|popitem|insert|appendleft|popleft|set|delete|push|splice)\s*\()")
    for member_id, chain in sorted(index.closure([holder.id], MAX_HOPS).items(), key=lambda kv: (len(kv[1]), kv[0])):
        member = index.members[member_id]
        if state["holder_kind"] == "module" and member.path != state["path"]:
            continue
        if state["holder_kind"] == "class" and member.owner.split(".")[0] != state["holder"]:
            continue
        if pattern.search(member.code):
            return [index.members[m].label for m in chain]
    return None


def tables_in(index: CallIndex) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for ex in index.extractions:
        if not ex.pack.schema:
            continue
        for table in ex.pack.schema(ex.tree_root, ex.source, ex.code, ex.strings):
            found.append({**table, "path": ex.rel, "component": ex.component})
    return found

