"""What a Python process does for its life: the loops it runs, the collections it
keeps in memory, the tables it declares. The pack's process hooks; the core
(``process.py``) decides which loops are the process's ticks and which
collections it keeps, from the entry point's import closure."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .base import child_of_type, node_end_line, node_line, node_text, walk

TIMER_RE = re.compile(r"\b(?:time\.)?sleep\(\s*([^)\n]*)\)|\basyncio\.sleep\(\s*([^)\n]*)\)|\.wait\(\s*(?:timeout\s*=\s*)?([^)\n]*)\)")
READ_RE = re.compile(r"\.readline\(|\.recv(?:from|_into)?\(|\.accept\(|\.get\(\s*(?:block|timeout)\b|\bselect\.select\(|\.poll\(")
CALL_RE = re.compile(r"(?<![\w.])((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)\s*\(")
TASK_STARTER_PATTERNS = (
    r"\bThread\(\s*(?:[^()]*?,\s*)?target\s*=\s*([A-Za-z_][\w.]*)",      # threading.Thread(target=f)
    r"\bTimer\(\s*[^,()]+,\s*([A-Za-z_][\w.]*)",                          # threading.Timer(1.0, f)
    r"\b(?:create_task|ensure_future|run_coroutine_threadsafe)\(\s*([A-Za-z_][\w.]*)\(",  # asyncio.create_task(f())
    r"\brun_in_executor\(\s*[^,()]+,\s*([A-Za-z_][\w.]*)",                # loop.run_in_executor(None, f)
    r"\bProcess\(\s*(?:[^()]*?,\s*)?target\s*=\s*([A-Za-z_][\w.]*)",     # multiprocessing.Process(target=f)
)
COLLECTION_CALLS = {"dict", "list", "set", "frozenset", "deque", "defaultdict", "OrderedDict", "Counter", "ChainMap", "WeakValueDictionary", "WeakKeyDictionary", "WeakSet", "LRUCache", "TTLCache"}
MUTATORS = r"(?:append|add|pop|update|setdefault|clear|extend|remove|discard|popitem|insert|appendleft|popleft|extendleft|rotate|__setitem__|__delitem__)"
SQL_TABLE_RE = re.compile(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)[\"'`\]]?\s*\(", re.IGNORECASE)
SQL_COLUMN_REF_RE = re.compile(r"^\s*[\"'`\[]?(\w+)[\"'`\]]?\s+[^,]*?\bREFERENCES\s+[\"'`\[]?(\w+)[\"'`\]]?\s*\(\s*([^)]*?)\s*\)(?:[^,]*?\bON\s+DELETE\s+((?:SET\s+)?\w+))?", re.IGNORECASE | re.DOTALL)
SQL_TABLE_REF_RE = re.compile(r"\bFOREIGN\s+KEY\s*\(\s*([^)]*?)\s*\)\s*REFERENCES\s+[\"'`\[]?(\w+)[\"'`\]]?\s*\(\s*([^)]*?)\s*\)(?:[^,]*?\bON\s+DELETE\s+((?:SET\s+)?\w+))?", re.IGNORECASE | re.DOTALL)
ORM_FOREIGN_KEY_RE = re.compile(r"\bForeignKey\(\s*[\"']([\w.]+)[\"'](?:[^)]*?\bondelete\s*=\s*[\"'](\w+(?:\s+\w+)?)[\"'])?", re.IGNORECASE)


def _enclosing_functions(node: Any, source: bytes) -> Tuple[List[str], Optional[str]]:
    """Names of the functions enclosing a node, innermost first, and the class
    around them if any."""
    names: List[str] = []
    owner: Optional[str] = None
    parent = node.parent
    while parent is not None:
        if parent.type == "function_definition":
            name = child_of_type(parent, "identifier")
            names.append(node_text(name, source) if name is not None else "")
        elif parent.type == "class_definition" and owner is None:
            name = child_of_type(parent, "identifier")
            owner = node_text(name, source) if name is not None else None
        parent = parent.parent
    return names, owner


def _balanced(text: str, open_at: int) -> int:
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def _split_top_level(text: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def loops(root: Any, source: bytes, code: str) -> List[Dict[str, Any]]:
    """Every ``while`` whose body is paced — by a timer (``sleep``, ``wait``) or a
    blocking read — with its holders and the calls its body makes in source order."""
    found: List[Dict[str, Any]] = []
    for node in walk(root):
        if node.type != "while_statement":
            continue
        block = child_of_type(node, "block")
        if block is None:
            continue
        body = code.encode("utf-8")[block.start_byte:block.end_byte].decode("utf-8", errors="replace")  # mask() keeps byte positions
        timer = TIMER_RE.search(body)
        pacing = "timer" if timer else ("read" if READ_RE.search(body) else None)
        if pacing is None:
            continue
        condition = node.children[1] if len(node.children) > 1 else None
        holders, owner = _enclosing_functions(node, source)
        interval = next((g for g in (timer.groups() if timer else ()) if g), "") if timer else ""
        steps = []
        for match in CALL_RE.finditer(body):
            name = match.group(1)
            if name.split(".")[-1] in ("sleep", "wait", "print", "len", "isinstance", "str", "int", "float", "dict", "list", "set"):
                continue
            steps.append({"call": name, "line": node_line(block) + body.count("\n", 0, match.start())})
        condition_text = node_text(condition, source) if condition is not None else ""
        found.append({
            "line": node_line(node), "end_line": node_end_line(node), "condition": condition_text,
            # A condition that compares (`attempts < 3`, `time.monotonic() < deadline`) bounds the loop: a retry, not a schedule.
            "bounded": bool(re.search(r"[<>]=?|==|!=", condition_text)),
            "holders": holders, "owner": owner, "pacing": pacing, "interval": interval.strip(), "steps": steps,
        })
    return found


def _shape(value: Any, source: bytes) -> Optional[str]:
    """The mutable collection an initialiser constructs, or None for anything else."""
    if value is None:
        return None
    if value.type == "dictionary":
        return "dict"
    if value.type == "list":
        return "list"
    if value.type == "set":
        return "set"
    if value.type == "call":
        function = value.children[0] if value.children else None
        name = node_text(function, source).split(".")[-1] if function is not None else ""
        if name in COLLECTION_CALLS:
            return name
    return None


def _assignment_parts(node: Any) -> Tuple[Optional[Any], Optional[Any]]:
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    return left, right


def resident_state(root: Any, source: bytes, code: str) -> List[Dict[str, Any]]:
    """Module-level names and instance fields initialised to a mutable collection
    and written somewhere in the file: ``_SUBSCRIPTIONS: dict = {}`` with
    ``_SUBSCRIPTIONS[key] = …``; ``self.cache = {}`` with ``self.cache.pop(…)``.
    An initialised-once, never-written collection is a lookup constant."""
    found: List[Dict[str, Any]] = []
    for node in walk(root):
        if node.type != "assignment":
            continue
        left, right = _assignment_parts(node)
        shape = _shape(right, source)
        if left is None or shape is None:
            continue
        holders, owner = _enclosing_functions(node, source)
        if left.type == "identifier" and not holders and owner is None:
            name = node_text(left, source)
            writes = len(re.findall(r"(?<![\w.])" + re.escape(name) + r"\s*\[[^\]\n]*\]\s*=(?!=)", code))
            writes += len(re.findall(r"(?<![\w.])" + re.escape(name) + r"\." + MUTATORS + r"\s*\(", code))
            writes += len(re.findall(r"\bdel\s+" + re.escape(name) + r"\s*\[", code))
            if re.search(r"\bglobal\s+" + re.escape(name) + r"\b", code):
                writes += len(re.findall(r"^\s+" + re.escape(name) + r"\s*=(?!=)", code, re.MULTILINE))
            if writes:
                found.append({"name": name, "holder": "", "holder_kind": "module", "shape": shape, "writes": writes, "line": node_line(node)})
        elif left.type == "attribute" and owner is not None:
            receiver, attribute = left.children[0], left.children[-1]
            if node_text(receiver, source) != "self":
                continue
            name = node_text(attribute, source)
            class_node = node.parent
            while class_node is not None and class_node.type != "class_definition":
                class_node = class_node.parent
            body = code.encode("utf-8")[class_node.start_byte:class_node.end_byte].decode("utf-8", errors="replace") if class_node is not None else code
            writes = len(re.findall(r"\bself\." + re.escape(name) + r"\s*\[[^\]\n]*\]\s*=(?!=)", body))
            writes += len(re.findall(r"\bself\." + re.escape(name) + r"\." + MUTATORS + r"\s*\(", body))
            writes += len(re.findall(r"\bdel\s+self\." + re.escape(name) + r"\s*\[", body))
            if writes and not any(f["holder"] == owner and f["name"] == name for f in found):
                found.append({"name": name, "holder": owner, "holder_kind": "class", "shape": shape, "writes": writes, "line": node_line(node)})
    return found


def _sql_tables(contents: str, line: int) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for match in SQL_TABLE_RE.finditer(contents):
        open_at = match.end() - 1
        body = contents[open_at + 1:_balanced(contents, open_at) - 1]
        table = {"table": match.group(1), "line": line + contents.count("\n", 0, match.start()), "form": "sql", "columns": [], "references": []}
        for part in _split_top_level(body):
            column = SQL_COLUMN_REF_RE.match(part)
            if column and column.group(1).upper() not in ("FOREIGN", "PRIMARY", "UNIQUE", "CHECK", "CONSTRAINT"):
                table["references"].append({"column": column.group(1), "table": column.group(2), "target_column": column.group(3).strip(), "on_delete": (column.group(4) or "").upper() or None})
            head = re.match(r"\s*[\"'`\[]?(\w+)", part)
            if head and head.group(1).upper() not in ("FOREIGN", "PRIMARY", "UNIQUE", "CHECK", "CONSTRAINT"):
                table["columns"].append(head.group(1))
        for match_ in SQL_TABLE_REF_RE.finditer(body):
            table["references"].append({"column": ", ".join(c.strip().strip("\"'`[]") for c in match_.group(1).split(",")), "table": match_.group(2),
                                        "target_column": ", ".join(c.strip().strip("\"'`[]") for c in match_.group(3).split(",")), "on_delete": (match_.group(4) or "").upper() or None})
        found.append(table)
    return found


def schema(root: Any, source: bytes, code: str, strings: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
    """Tables and their foreign keys: ``CREATE TABLE`` statements inside string
    literals (column-level ``REFERENCES`` and table-level ``FOREIGN KEY``), and
    SQLAlchemy declarative classes (``__tablename__`` with ``ForeignKey("t.c")``
    columns)."""
    found: List[Dict[str, Any]] = []
    for line, contents in strings:
        if "CREATE" in contents.upper() and "TABLE" in contents.upper():
            found.extend(_sql_tables(contents, line))
    by_line: Dict[int, List[str]] = {}
    for line, contents in strings:
        by_line.setdefault(line, []).append(contents)
    for node in walk(root):
        if node.type != "class_definition":
            continue
        block = child_of_type(node, "block")
        if block is None:
            continue
        table_name: Optional[str] = None
        table_line = node_line(node)
        for child in walk(block):
            if child.type == "assignment":
                left, _right = _assignment_parts(child)
                if left is not None and node_text(left, source) == "__tablename__":
                    candidates = by_line.get(node_line(child), [])
                    table_name = candidates[0] if candidates else None
                    table_line = node_line(child)
                    break
        if not table_name:
            continue
        table = {"table": table_name, "line": table_line, "form": "orm", "columns": [], "references": []}
        class_raw = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        for match in ORM_FOREIGN_KEY_RE.finditer(class_raw):
            target_table, _, target_column = match.group(1).rpartition(".")
            head = re.search(r"(\w+)\s*(?::[^=\n]+)?=\s*[^\n]*$", class_raw[:match.start()].rsplit("\n", 1)[-1])
            table["references"].append({"column": head.group(1) if head else None, "table": target_table or match.group(1), "target_column": target_column,
                                        "on_delete": (match.group(2) or "").upper() or None})
        for match in re.finditer(r"^\s+(\w+)\s*(?::[^=\n]+)?=\s*(?:Column|mapped_column)\(", class_raw, re.MULTILINE):
            table["columns"].append(match.group(1))
        found.append(table)
    return found
