"""Service logs: declared and derived sinks, bounded tails, follow — by graph id.

Logs are a property of the RUNNING service, so the RPCs live under ``service.*``
and take the graph ids the cron dataflow graph uses: ``arch:<id>`` (an
architecture manifest), ``launchd:<label>``, ``docker:<12>``, ``nomad:<job>``,
``proc_<id>``. Sinks resolve per provider:

  arch:<id>        the manifest's declared sinks; when the manifest binds a launchd
                   runtime, its plist's StandardOutPath/StandardErrorPath are added as
                   ``launchd_stdout``/``launchd_stderr`` unless already declared
  launchd:<label>  the plist's paths as ``stdout``/``stderr``; if a manifest binds
                   that label, its declared sinks are unioned in
  docker/nomad/proc  not implemented yet (4042) — honest, never a fake empty list

Enforcement stays with the architecture manifest: a LOCAL service (a manifest
with a ``root``) must declare at least one resolvable sink or it is reported
non-conforming (``architecture_store.conformance_for``). GitHub-only manifests
are exempt (no runtime here).

Sinks (``logs: [{id, kind, path?, label?}]`` in the manifest):

  file            one log file
  directory       a folder of log files; the newest by mtime is the active file
  launchd_stdout  the bound launchd job's ``StandardOutPath`` (path may be omitted)
  launchd_stderr  the bound launchd job's ``StandardErrorPath`` (path may be omitted)

Reads are bounded: a tail scans at most ``TAIL_SCAN_BYTES`` from the end and
returns at most ``MAX_LINES`` lines; a cursor read returns only complete lines
appended since a byte offset. Following polls a sink from a daemon thread and
broadcasts ``service.log`` events with the new lines, coalesced, stopping after
``FOLLOW_IDLE_S`` without a client asking again or when told to stop. Only
declared or plist-derived paths are ever opened — never a caller-supplied one.
"""
from __future__ import annotations

import logging
import os
import plistlib
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SINK_KINDS = ("file", "directory", "launchd_stdout", "launchd_stderr")
LAUNCHD_KINDS = {"launchd_stdout": "StandardOutPath", "launchd_stderr": "StandardErrorPath"}
DEFAULT_LINES = 200
MAX_LINES = 2000
TAIL_SCAN_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
FOLLOW_INTERVAL_S = 1.0
FOLLOW_IDLE_S = 600.0
MAX_EVENT_LINES = 500
NO_CAPTURE_PROBLEM = "no log capture declared: add a logs sink to the manifest"
EVENT = "service.log"
UNSUPPORTED_PROVIDER_PROBLEM = "log capture is not implemented for this provider yet"

_LAUNCHD_DIRS: Tuple[str, ...] = ("~/Library/LaunchAgents", "/Library/LaunchAgents", "/Library/LaunchDaemons")


class LogError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Sink declaration ─────────────────────────────────────────────────────────


def launchd_plist_path(label: str, search_dirs: Optional[List[str]] = None) -> Optional[Path]:
    """The plist that defines a launchd label, or None: the user's agents first,
    then the system agents and daemons."""
    for directory in search_dirs if search_dirs is not None else list(_LAUNCHD_DIRS):
        candidate = Path(directory).expanduser() / f"{label}.plist"
        if candidate.is_file():
            return candidate
    return None


def launchd_log_path(label: str, key: str, search_dirs: Optional[List[str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(path, problem)``: the plist's log path for ``key`` (StandardOutPath /
    StandardErrorPath) or why it could not be derived."""
    plist = launchd_plist_path(label, search_dirs)
    if plist is None:
        return None, f"no launchd plist found for {label!r}"
    try:
        with open(plist, "rb") as handle:
            doc = plistlib.load(handle)
    except Exception as exc:  # malformed plist: report, never raise into the graph
        return None, f"launchd plist {plist} unreadable: {exc}"
    value = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None, f"launchd plist {plist.name} declares no {key}"
    return str(Path(value.strip()).expanduser()), None


def normalize_log_sinks(value: Any, runtime: Optional[Dict[str, Any]],
                        launchd_dirs: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Validate ``logs`` from a manifest. Raises ValueError for a malformed
    declaration (unknown kind, empty/duplicate id, relative path, launchd kind
    without a launchd binding). A launchd sink whose plist cannot be read keeps
    ``path: None`` and a ``problem`` — the manifest stays valid and the service
    is reported non-conforming rather than disappearing."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("logs must be a list of sinks {id, kind, path?, label?}")
    sinks: List[Dict[str, Any]] = []
    seen: set = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"logs[{index}] must be an object")
        sink_id = raw.get("id")
        if not isinstance(sink_id, str) or not sink_id.strip():
            raise ValueError(f"logs[{index}].id must be a non-empty string")
        sink_id = sink_id.strip()
        if sink_id in seen:
            raise ValueError(f"logs: duplicate sink id {sink_id!r}")
        seen.add(sink_id)
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in SINK_KINDS:
            raise ValueError(f"logs[{sink_id}].kind must be one of {', '.join(SINK_KINDS)}")
        label = raw.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError(f"logs[{sink_id}].label must be a string")
        path = raw.get("path")
        problem: Optional[str] = None
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f"logs[{sink_id}].path must be a non-empty string when present")
            path = str(Path(path.strip()).expanduser())
            if not os.path.isabs(path):
                raise ValueError(f"logs[{sink_id}].path must be absolute (got {raw.get('path')!r})")
        if kind in LAUNCHD_KINDS:
            if not runtime or runtime.get("provider") != "launchd":
                raise ValueError(f"logs[{sink_id}]: kind {kind} needs a runtime binding with provider launchd")
            if path is None:
                path, problem = launchd_log_path(str(runtime["id"]), LAUNCHD_KINDS[kind], launchd_dirs)
        elif path is None:
            raise ValueError(f"logs[{sink_id}]: kind {kind} needs a path")
        sink: Dict[str, Any] = {"id": sink_id, "kind": kind, "label": (label or sink_id).strip(), "path": path}
        if problem:
            sink["problem"] = problem
        sinks.append(sink)
    return sinks


def capture_problem(manifest: Dict[str, Any]) -> Optional[str]:
    """Why a LOCAL manifest fails log-capture enforcement, or None when it passes:
    at least one declared sink must resolve to a path. GitHub-only manifests
    are exempt (logs are a runtime property, and the runtime is not here)."""
    if not manifest.get("root"):
        return None
    sinks = manifest.get("logs") or []
    if not sinks:
        return NO_CAPTURE_PROBLEM
    if not any(sink.get("path") for sink in sinks):
        problems = "; ".join(str(sink.get("problem") or f"{sink['id']}: no path") for sink in sinks)
        return f"no log sink resolves: {problems}"
    return None


# ── Sink resolution ──────────────────────────────────────────────────────────


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def active_file(sink: Dict[str, Any]) -> Optional[Path]:
    """The file a sink reads: the declared file, or the newest regular file in a
    directory sink (by mtime, then name for determinism). None when unresolved."""
    path = sink.get("path")
    if not path:
        return None
    target = Path(path)
    if sink.get("kind") != "directory":
        return target
    if not target.is_dir():
        return None
    candidates = [p for p in target.iterdir() if p.is_file() and not p.name.startswith(".")]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return candidates[-1]


def resolve_sink(sink: Dict[str, Any]) -> Dict[str, Any]:
    """The sink as clients see it: declaration plus whether the active file
    exists, its size and mtime. Fail-open per sink: a missing file is
    ``exists: false``, never an error."""
    resolved = {"id": sink["id"], "kind": sink["kind"], "label": sink.get("label") or sink["id"],
                "path": sink.get("path"), "exists": False, "size_bytes": 0, "modified_at": None}
    if sink.get("problem"):
        resolved["problem"] = sink["problem"]
    try:
        target = active_file(sink)
        if target is not None and target.is_file():
            stat = target.stat()
            resolved.update({"path": str(target), "exists": True, "size_bytes": int(stat.st_size), "modified_at": _iso(stat.st_mtime)})
        elif sink.get("kind") == "directory" and sink.get("path") and Path(sink["path"]).is_dir():
            resolved["problem"] = "directory has no log files yet"
    except OSError as exc:
        resolved["problem"] = f"{type(exc).__name__}: {exc}"
    return resolved


def resolve_sinks(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [resolve_sink(sink) for sink in manifest.get("logs") or []]


def select_sink(sinks: List[Dict[str, Any]], sink_id: Optional[str], service: str) -> Dict[str, Any]:
    """The sink to read: by id, or the first one. Raises LogError 4040 when the
    id is unknown or the service has no sinks at all."""
    if not sinks:
        raise LogError(4040, f"{service}: {NO_CAPTURE_PROBLEM}")
    if sink_id is None:
        return sinks[0]
    for sink in sinks:
        if sink["id"] == sink_id:
            return sink
    raise LogError(4040, f"unknown log sink {sink_id!r}; declared: {', '.join(s['id'] for s in sinks)}")


# ── Resolution by graph id ───────────────────────────────────────────────────


def launchd_sinks(label: str, launchd_dirs: Optional[List[str]] = None,
                  ids: Tuple[str, str] = ("stdout", "stderr")) -> List[Dict[str, Any]]:
    """The sinks a launchd label's plist names (StandardOutPath → ids[0],
    StandardErrorPath → ids[1]). Only paths the plist actually declares."""
    sinks: List[Dict[str, Any]] = []
    for sink_id, (kind, key) in zip(ids, LAUNCHD_KINDS.items()):
        path, _problem = launchd_log_path(label, key, launchd_dirs)
        if path:
            sinks.append({"id": sink_id, "kind": kind, "label": f"launchd {key}", "path": path})
    return sinks


def _union(primary: List[Dict[str, Any]], extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``primary`` plus every ``extra`` sink whose id and (kind, path) are new."""
    seen_ids = {s["id"] for s in primary}
    seen_paths = {(s["kind"], s.get("path")) for s in primary}
    merged = list(primary)
    for sink in extra:
        if sink["id"] in seen_ids or (sink["kind"], sink.get("path")) in seen_paths:
            continue
        merged.append(sink)
        seen_ids.add(sink["id"])
        seen_paths.add((sink["kind"], sink.get("path")))
    return merged


def service_sinks(graph_id: str, home: Optional[str] = None, launchd_dirs: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Every sink a graph service id resolves to, per provider (see module doc).
    LogError 4030 for an unknown ``arch:`` service, 4042 for a provider without
    log capture, 4001 for an id that is not a graph service id."""
    from tui_gateway import architecture_store as store

    if graph_id.startswith(store.ID_PREFIX):
        manifest = store.load_manifest(graph_id, home)
        if manifest is None:
            raise LogError(4030, f"unknown service: {graph_id}")
        sinks = list(manifest.get("logs") or [])
        runtime = manifest.get("runtime") or {}
        if runtime.get("provider") == "launchd":
            sinks = _union(sinks, launchd_sinks(str(runtime["id"]), launchd_dirs, ids=("launchd_stdout", "launchd_stderr")))
        return sinks
    if graph_id.startswith("launchd:"):
        label = graph_id[len("launchd:"):]
        if not label:
            raise LogError(4001, "launchd service id needs a label")
        sinks = launchd_sinks(label, launchd_dirs)
        for manifest in store.list_manifests(home):
            runtime = manifest.get("runtime") or {}
            if runtime.get("graph_id") == graph_id:
                sinks = _union(sinks, list(manifest.get("logs") or []))
        return sinks
    if graph_id.startswith(("docker:", "nomad:", "proc_")):
        raise LogError(4042, f"{graph_id}: {UNSUPPORTED_PROVIDER_PROBLEM}")
    raise LogError(4001, f"{graph_id!r} is not a graph service id (arch:, launchd:, docker:, nomad:, proc_)")


# ── Bounded reads ────────────────────────────────────────────────────────────


def _decode(chunk: bytes) -> str:
    return chunk.decode("utf-8", errors="replace")


def _split_lines(data: bytes) -> List[str]:
    return [_decode(line[:MAX_LINE_BYTES]) for line in data.split(b"\n")]


def read_tail(path: Path, lines: int) -> Dict[str, Any]:
    """The last ``lines`` complete lines of ``path`` (scanning at most
    TAIL_SCAN_BYTES from the end) and the cursor to continue from: the file's
    byte length, so the next read sees only what is appended afterwards."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - TAIL_SCAN_BYTES)
        handle.seek(start)
        data = handle.read(size - start)
    # A partial trailing line (no newline yet) is not returned; it is re-read
    # once complete via the cursor, which stays at the end of the last full line.
    complete_end = data.rfind(b"\n")
    if complete_end == -1:
        return {"lines": [], "cursor": str(start), "truncated": False, "scanned_from": start}
    body = data[:complete_end]
    all_lines = _split_lines(body)
    if start > 0 and all_lines:
        all_lines = all_lines[1:]  # the first piece is the tail of a line we did not fully scan
    truncated = len(all_lines) > lines or start > 0
    return {"lines": all_lines[-lines:], "cursor": str(start + complete_end + 1), "truncated": truncated, "scanned_from": start}


def read_since(path: Path, cursor: int, lines: int) -> Dict[str, Any]:
    """Every complete line appended after byte ``cursor`` (bounded by ``lines``,
    ``truncated`` when more remain). A file shorter than the cursor was rotated
    or truncated: restart from the tail and say so."""
    size = path.stat().st_size
    if size < cursor:
        result = read_tail(path, lines)
        result["rotated"] = True
        return result
    if size == cursor:
        return {"lines": [], "cursor": str(cursor), "truncated": False}
    budget = min(size - cursor, TAIL_SCAN_BYTES)
    with open(path, "rb") as handle:
        handle.seek(cursor)
        data = handle.read(budget)
    complete_end = data.rfind(b"\n")
    if complete_end == -1:
        return {"lines": [], "cursor": str(cursor), "truncated": False}
    body = data[:complete_end]
    all_lines = _split_lines(body)
    if len(all_lines) > lines:
        kept = all_lines[:lines]
        consumed = sum(len(line.encode("utf-8")) + 1 for line in kept)
        # Recompute from bytes to stay exact for lines that decoded with replacements.
        consumed = len(b"\n".join(body.split(b"\n")[:lines])) + 1
        return {"lines": kept, "cursor": str(cursor + consumed), "truncated": True}
    return {"lines": all_lines, "cursor": str(cursor + complete_end + 1), "truncated": (size - cursor) > budget}


def parse_lines(value: Any) -> int:
    if value is None:
        return DEFAULT_LINES
    if isinstance(value, bool) or not isinstance(value, int):
        raise LogError(4001, "lines must be an integer")
    if value < 1 or value > MAX_LINES:
        raise LogError(4001, f"lines must be between 1 and {MAX_LINES}")
    return value


def parse_cursor(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise LogError(4001, "cursor must be the string returned by a previous read")
    try:
        cursor = int(value)
    except ValueError as exc:
        raise LogError(4001, "cursor must be the string returned by a previous read") from exc
    if cursor < 0:
        raise LogError(4001, "cursor must not be negative")
    return cursor


def read_logs(sinks: List[Dict[str, Any]], service: str, sink_id: Optional[str], lines: int, cursor: Optional[int]) -> Dict[str, Any]:
    """``service.logs``: a bounded tail or the lines since a cursor, from one of
    the service's sinks only. LogError 4040 unknown sink, 4041 sink not present."""
    sink = select_sink(sinks, sink_id, service)
    target = active_file(sink)
    if target is None or not target.is_file():
        raise LogError(4041, f"log sink {sink['id']!r} has no file yet" + (f" ({sink['problem']})" if sink.get("problem") else ""))
    try:
        result = read_tail(target, lines) if cursor is None else read_since(target, cursor, lines)
    except OSError as exc:
        raise LogError(4041, f"log sink {sink['id']!r} unreadable: {exc}") from exc
    result.pop("scanned_from", None)
    resolved = resolve_sink(sink)
    resolved["path"] = str(target)
    result.update({"service": service, "sink": resolved, "sinks": [resolve_sink(s) for s in sinks], "encoding": "utf-8-replace"})
    return result


# ── Follow ───────────────────────────────────────────────────────────────────


class Follower:
    """One followed sink: polls for appended lines and broadcasts them."""

    def __init__(self, service: str, sink: Dict[str, Any], cursor: int, broadcast: Callable[[str, Dict[str, Any]], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.service = service
        self.sink = sink
        self.cursor = cursor
        self.broadcast = broadcast
        self.clock = clock
        self.started_at = clock()
        self.last_activity = self.started_at
        self.stop_requested = threading.Event()
        self.thread: Optional[threading.Thread] = None

    @property
    def key(self) -> Tuple[str, str]:
        return (self.service, self.sink["id"])

    def touch(self) -> None:
        """A client asked again: the idle clock restarts."""
        self.last_activity = self.clock()

    def idle_for(self) -> float:
        return self.clock() - self.last_activity

    def poll_once(self) -> Optional[Dict[str, Any]]:
        """Read what was appended since the cursor; broadcast and return the
        payload when there are new lines, else None. Never raises."""
        target = active_file(self.sink)
        if target is None or not target.is_file():
            return None
        try:
            result = read_since(target, self.cursor, MAX_EVENT_LINES)
        except OSError:
            logger.debug("log follow read failed for %s", self.key, exc_info=True)
            return None
        self.cursor = int(result["cursor"])
        if not result["lines"] and not result.get("rotated"):
            return None
        payload: Dict[str, Any] = {"service": self.service, "sink": self.sink["id"], "lines": result["lines"], "cursor": result["cursor"]}
        if result.get("rotated"):
            payload["rotated"] = True
        self.broadcast(EVENT, payload)
        return payload

    def stop(self, reason: str) -> None:
        if self.stop_requested.is_set():
            return
        self.stop_requested.set()
        self.broadcast(EVENT, {"service": self.service, "sink": self.sink["id"], "lines": [], "cursor": str(self.cursor), "stopped": reason})

    def run(self, interval: float = FOLLOW_INTERVAL_S, sleep: Callable[[float], None] = time.sleep) -> None:
        """The polling loop: until stopped or idle for FOLLOW_IDLE_S."""
        while not self.stop_requested.is_set():
            if self.idle_for() >= FOLLOW_IDLE_S:
                _forget(self)
                self.stop("idle-timeout")
                return
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - poll_once already guards
                logger.debug("log follow poll failed for %s", self.key, exc_info=True)
            sleep(interval)


_followers: Dict[Tuple[str, str], Follower] = {}
_followers_lock = threading.Lock()


def _forget(follower: Follower) -> None:
    with _followers_lock:
        if _followers.get(follower.key) is follower:
            del _followers[follower.key]


def start_follow(sinks: List[Dict[str, Any]], service: str, sink_id: Optional[str],
                 broadcast: Callable[[str, Dict[str, Any]], None], cursor: Optional[int] = None,
                 spawn: bool = True, clock: Callable[[], float] = time.monotonic) -> Follower:
    """Follow one of the service's sinks from its current end (or ``cursor``).
    One follower per (service, sink): asking again refreshes the idle clock and
    returns the existing follower. ``spawn=False`` creates without a thread
    (tests drive ``poll_once`` themselves)."""
    sink = select_sink(sinks, sink_id, service)
    target = active_file(sink)
    if target is None or not target.is_file():
        raise LogError(4041, f"log sink {sink['id']!r} has no file yet; nothing to follow")
    with _followers_lock:
        existing = _followers.get((service, sink["id"]))
        if existing is not None and not existing.stop_requested.is_set():
            existing.touch()
            return existing
        start_cursor = cursor if cursor is not None else target.stat().st_size
        follower = Follower(service, sink, start_cursor, broadcast, clock)
        _followers[follower.key] = follower
    if spawn:
        thread = threading.Thread(target=follower.run, name=f"arch-logs:{service}:{sink['id']}", daemon=True)
        follower.thread = thread
        thread.start()
    return follower


def stop_follow(service: str, sink_id: str) -> bool:
    """Stop following; True when a follower existed."""
    with _followers_lock:
        follower = _followers.pop((service, sink_id), None)
    if follower is None:
        return False
    follower.stop("stopped")
    return True


def following(service: str, sink_id: str) -> Optional[Follower]:
    with _followers_lock:
        follower = _followers.get((service, sink_id))
    return follower if follower is not None and not follower.stop_requested.is_set() else None


def stop_all(reason: str = "shutdown") -> None:
    with _followers_lock:
        active = list(_followers.values())
        _followers.clear()
    for follower in active:
        follower.stop(reason)
