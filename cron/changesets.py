"""Recorded history of the cron dataflow graph's *configuration*.

``cron.graph`` only ever answers "what is the wiring right now", so there was no
way to ask when a job's schedule changed, who changed it, or what the graph
looked like before. A poller can approximate the first question by diffing
successive reads; it can never answer the other two. This module is the record
that can: an append-on-change log, written by the process that performs the
change, at the moment it performs it.

**What gets recorded, and what emphatically does not.** Every cron mutation in
this codebase funnels through ``_save_jobs_unlocked``, and most of those saves
are the scheduler's own bookkeeping — ``last_run_at``, ``next_run_at``,
preflight flags, the due-scan self-heal sweep. A row per save would be a log of
the tick loop with the occasional real change buried in it. So the log is gated
on a digest taken over the *configuration* form of the graph only
(:func:`configuration_digest`): identity, labels, schedules, enabled, whether a
job burns a model, where it delivers, and every dataflow edge. ``last_status``,
``state`` and service health are excluded — a container restarting is not a
change to the dataflow, and including liveness would mint a row per tick.

**Why this exact encoding.** The digest is byte-compatible with Portal's
``CronGraphDigest`` (``Sources/Portal/Models/CronGraphDigest.swift``): SHA-256
over sorted, length-prefixed rows, sorted by UTF-8 bytes so both languages agree
on the order. Two implementations of a content address that disagree are worse
than one, because each of them looks authoritative on its own screen. The
parity fixture in ``tests/cron/test_cron_changesets.py`` is asserted against the
identical fixture and hex in Portal's ``CronGraphDigestTests`` — change the
canonical form and both sides have to change together, or the test says so.

**Definitions, not only wiring.** The graph form names a job and says when it
runs and where it delivers; it says nothing about *what* the job is — its prompt,
model, provider, skills, script, working directory, toolsets, context sources,
inputs, outputs, side effects and source files. A prompt edit therefore moves no
graph digest. So every row also carries the *definition form* of every job
(:func:`definition_form`, the read-safe record minus runtime bookkeeping) under a
second digest, and the gate records a row when either commitment moved. A row
that changed one job lists the fields that changed with their before and after
values, and :func:`read_job_revisions` reads one job's history out of the log:
which revision created it, each field-level change since, who made it, and the
definition as it stood at each point. The graph digest keeps its meaning and its
Portal parity unchanged; the definition digest is a separate commitment that
Portal does not compute.

**One known asymmetry, on purpose.** ``cron.graph`` overlays live service nodes
(dashboards, APIs, Docker deps) onto the graph it serves; the jobs store knows
nothing about them and this log commits to the cron configuration alone. So a
digest recorded here does not equal the digest Portal computes over a live graph
whenever a service is running. The recorded diffs are still exactly right — both
sides of a comparison come from this log — but a client matching a recorded
digest against a live one will simply not find a match. That is the honest
outcome: the two digests are commitments to different things.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import subprocess
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# How many rows the log keeps. Matches Portal's observed-revision store so the
# two histories go blind at the same depth instead of one silently outliving the
# other and looking more complete than it is.
MAX_CHANGESETS = 200

CHANGESET_LOG_NAME = "changesets.jsonl"

# A one-line cache of the newest row's digest. Every save consults it, and only a
# mismatch pays for parsing the log (which carries a graph snapshot per row).
# Never authoritative: a stale or missing head costs one full read, never a
# wrong record — see _record_change.
CHANGESET_HEAD_NAME = "changesets.head"


# =============================================================================
# Store paths
# =============================================================================

def changeset_log_path() -> Path:
    """Path to the log for the *active* cron store context."""
    from cron.jobs import _current_cron_store

    return _current_cron_store().cron_dir / CHANGESET_LOG_NAME


def changeset_head_path() -> Path:
    from cron.jobs import _current_cron_store

    return _current_cron_store().cron_dir / CHANGESET_HEAD_NAME


# =============================================================================
# The canonical form (Portal parity — see the module docstring)
# =============================================================================

def _field_text(value: str) -> str:
    """Length-prefixed field: ``<utf8 byte count>:<value>``.

    Length-prefixed rather than separator-joined because node ids are
    ``scheme:value`` and job labels are ``folder/name`` — any plain separator
    already occurs inside the values it would separate. Joined on ``:``, the
    nodes ``(id "wiki:a", kind "artifact")`` and ``(id "wiki", kind
    "a:artifact")`` encode identically: two different graphs with one address,
    which for a content address is the one failure that matters.
    """
    return f"{len(value.encode('utf-8'))}:{value}"


def _field_optional(value: Optional[str]) -> str:
    """``None`` is distinct from ``""``.

    A job with no schedule and a job whose schedule was cleared to the empty
    string are different configurations, and collapsing them would hide the edit
    between them.
    """
    if value is None:
        return "-"
    return "+" + _field_text(value)


def _field_bool(value: bool) -> str:
    return "1" if value else "0"


def _row(tag: str, fields: Sequence[str]) -> str:
    return _field_text(tag) + "".join(fields)


def _text(value: Any, fallback: str = "") -> str:
    """A string field as the client reads it: non-strings fall back."""
    return value if isinstance(value, str) else fallback


def _optional_text(value: Any) -> Optional[str]:
    """An optional string field: absent, null, or non-string all mean absent."""
    return value if isinstance(value, str) else None


def _flag(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _node_row(node: Dict[str, Any]) -> Optional[str]:
    """One node's canonical row, or None for a node the client would drop.

    The defaults here are not ours to choose: they are exactly what Portal's
    ``CronGraph.decodeGatewayValue`` applies to the same payload (``type``
    falling back to ``kind``, ``label`` to ``id``, ``enabled`` to true,
    ``uses_llm`` to false, an id-less node dropped). The digest has to be taken
    over the graph *as the client will read it*, or the two sides hash different
    graphs from the same bytes.
    """
    node_id = _text(node.get("id"))
    kind = _optional_text(node.get("kind"))
    if not node_id or kind is None:
        return None
    return _row(
        "n",
        [
            _field_text(node_id),
            _field_text(kind),
            _field_text(_text(node.get("type"), kind)),
            _field_text(_text(node.get("label"), node_id)),
            _field_text(_text(node.get("description"))),
            _field_optional(_optional_text(node.get("schedule"))),
            _field_bool(_flag(node.get("enabled"), True)),
            _field_bool(_flag(node.get("uses_llm"), False)),
            _field_optional(_optional_text(node.get("deliver"))),
        ],
    )


def _edge_row(edge: Dict[str, Any]) -> Optional[str]:
    source = _optional_text(edge.get("source"))
    target = _optional_text(edge.get("target"))
    if source is None or target is None:
        return None
    return _row(
        "e",
        [
            _field_text(source),
            _field_text(target),
            _field_text(_text(edge.get("type"), "reads")),
        ],
    )


def configuration_form(graph: Dict[str, Any]) -> List[str]:
    """The rows the commitment is taken over, in canonical order.

    Sorted by UTF-8 bytes rather than by the language's native string order:
    Python compares code points and Swift compares canonically-equivalent
    graphemes, so for any non-ASCII label the two would disagree on row order
    and therefore on the digest — a divergence that would only ever show up on
    someone's emoji-named job.
    """
    rows: List[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        row = _node_row(node)
        if row is not None:
            rows.append(row)
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        row = _edge_row(edge)
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda text: text.encode("utf-8"))


def configuration_digest(graph: Dict[str, Any]) -> str:
    """Lowercase hex SHA-256 over the canonical form — 64 characters."""
    canonical = "".join(configuration_form(graph))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def configuration_graph(jobs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The graph the log commits to: the jobs' dataflow, no live overlay.

    Services are deliberately absent — see the module docstring's note on the
    asymmetry with ``cron.graph``. Jobs are normalized first because the graph's
    labels and schedules come from the read-safe shape
    (``schedule_display``/``name``), not from raw storage.
    """
    from cron.jobs import _normalize_job_record, build_cron_graph, load_jobs

    if jobs is None:
        jobs = load_jobs()
    records = [_normalize_job_record(job) for job in jobs if isinstance(job, dict)]
    return build_cron_graph(jobs=records)


# =============================================================================
# The definition form (what a job *is*, as distinct from how it is wired)
# =============================================================================

# Every field a person can set on a job, in the read-safe shape. Runtime
# bookkeeping (``last_run_at``, ``next_run_at``, ``last_status``, ``state``,
# ``paused_at``, preflight flags, ``created_at``) is deliberately absent: a change
# to those is the scheduler working, not the job changing.
DEFINITION_FIELDS: Tuple[str, ...] = (
    "name",
    "schedule",
    "enabled",
    "deliver",
    "repeat",
    "prompt",
    "model",
    "provider",
    "base_url",
    "skill",
    "skills",
    "script",
    "context_from",
    "enabled_toolsets",
    "workdir",
    "no_agent",
    "attach_to_session",
    "monitor_script",
    "monitor_url",
    "inputs",
    "outputs",
    "side_effects",
    "source_files",
)


def _plain(value: Any) -> Any:
    """A JSON-stable copy of a field value: lists of scalars kept as lists, dicts
    kept, anything exotic rendered as text so two writers agree on the bytes."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    return str(value)


def definition_form(job: Dict[str, Any]) -> Dict[str, Any]:
    """One job's definition, as a person would read it back: only the fields in
    :data:`DEFINITION_FIELDS` that are present, JSON-stable."""
    form: Dict[str, Any] = {}
    for field in DEFINITION_FIELDS:
        if field not in job:
            continue
        if field == "schedule" and isinstance(job.get("schedule_display"), str) and job["schedule_display"]:
            # The read-safe display form ("every 6h", a cron expression), the same
            # string the graph carries, rather than the parsed storage dict.
            form[field] = job["schedule_display"]
            continue
        if field == "repeat" and isinstance(job.get("repeat"), dict):
            # ``times`` is the configured limit; ``completed`` is scheduler
            # bookkeeping that advances after every run. Committing the whole
            # storage dict turns every successful tick into a fake definition
            # revision even though the user changed nothing.
            form[field] = {"times": _plain(job["repeat"].get("times"))}
            continue
        form[field] = _plain(job.get(field))
    return form


def configuration_definitions(jobs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """Every job's definition form by job id, from the normalized records."""
    from cron.jobs import _normalize_job_record, load_jobs

    if jobs is None:
        jobs = load_jobs()
    definitions: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        record = _normalize_job_record(job)
        job_id = _text(record.get("id"))
        if job_id:
            definitions[job_id] = definition_form(record)
    return definitions


def definition_digest(definitions: Dict[str, Dict[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of every definition, keyed by job id."""
    payload = json.dumps(definitions, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_recorded_definitions(definitions: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade stored definition snapshots to the current canonical shape.

    The first definition-history release stored ``repeat.completed`` alongside
    the configured limit. Removing that scheduler-owned counter must not create
    a synthetic revision when an existing log is opened by a newer runtime.
    """
    canonical: Dict[str, Any] = {}
    for job_id, definition in definitions.items():
        if not isinstance(definition, dict):
            canonical[job_id] = definition
            continue
        normalized = dict(definition)
        repeat = normalized.get("repeat")
        if isinstance(repeat, dict):
            normalized["repeat"] = {"times": _plain(repeat.get("times"))}
        canonical[job_id] = normalized
    return canonical


def definition_changes(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Field-level differences between two definition forms of one job, in field
    order. A missing side (created or deleted job) diffs against an empty form."""
    before = before or {}
    after = after or {}
    changes: List[Dict[str, Any]] = []
    for field in DEFINITION_FIELDS:
        if field not in before and field not in after:
            continue
        if before.get(field) != after.get(field):
            changes.append({"field": field, "before": before.get(field), "after": after.get(field)})
    return changes


# =============================================================================
# Who made the change (actor + provenance)
# =============================================================================
@dataclass(frozen=True)
class ChangesetOrigin:
    """Attribution for whatever configuration change happens in this context.

    ``actor`` is the vocabulary the client renders: ``human``, ``agent``,
    ``scheduler``, or ``""`` for "nobody recorded one". An empty actor is an
    admission of ignorance, never a claim that nobody did it — which is why an
    unrecognized value is passed through verbatim rather than folded into the
    empty string.
    """

    actor: str = ""
    source_event_keys: Tuple[str, ...] = ()
    note: str = ""


_origin: ContextVar[Optional[ChangesetOrigin]] = ContextVar(
    "cron_changeset_origin",
    default=None,
)


@contextlib.contextmanager
def use_changeset_origin(
    actor: str,
    *,
    source_event_keys: Optional[Sequence[str]] = None,
    note: str = "",
    if_unset: bool = False,
):
    """Attribute configuration changes made inside this block.

    Bound at the boundary that *knows* who is acting — the gateway method a UI
    called, the tool the model called, the scheduler branch that disables a
    finished one-shot — and not one layer deeper, because the layers below are
    shared by all three.

    ``if_unset=True`` yields to an already-bound origin. The tool entry point
    uses it: ``cron.manage`` binds ``human`` before delegating to the same tool
    function the model calls, and the outer, more specific claim must win.
    """
    if if_unset and _origin.get() is not None:
        yield
        return
    keys = tuple(
        str(key).strip()
        for key in (source_event_keys or ())
        if str(key).strip()
    )
    token = _origin.set(
        ChangesetOrigin(actor=str(actor or "").strip(), source_event_keys=keys, note=note)
    )
    try:
        yield
    finally:
        _origin.reset(token)


def _session_turn_keys() -> Tuple[str, ...]:
    """Provenance from the ambient session context, when there is one.

    The key is opaque to the client, which joins and displays it without parsing
    meaning out of it, so the shape only has to be stable and recognizable:
    ``session/<session id>[/turn-<message id>]``.
    """
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return ()
    try:
        session_id = (get_session_env("HERMES_SESSION_ID", "") or "").strip()
        if not session_id:
            return ()
        message_id = (get_session_env("HERMES_SESSION_MESSAGE_ID", "") or "").strip()
        if message_id:
            return (f"session/{session_id}/turn-{message_id}",)
        return (f"session/{session_id}",)
    except Exception:
        return ()


def current_origin() -> ChangesetOrigin:
    """The origin to record: what the boundary claimed, plus what the session knows.

    A boundary usually knows *who* is acting and not *which turn* — the turn is
    already in the ambient session context that every other tool reads its
    routing from, so cron doesn't ask its callers to thread it through by hand.
    Keys passed explicitly win; the session only fills a gap, and when there is
    no session either, provenance stays empty.
    """
    bound = _origin.get()
    if bound is not None:
        if bound.source_event_keys:
            return bound
        return ChangesetOrigin(
            actor=bound.actor,
            source_event_keys=_session_turn_keys(),
            note=bound.note,
        )
    return ChangesetOrigin(source_event_keys=_session_turn_keys())


# =============================================================================
# Reading and writing the log
# =============================================================================

def _read_rows() -> List[Dict[str, Any]]:
    """Every stored row, oldest first.

    A line that won't parse is skipped rather than fatal: this is a log, and one
    torn tail line must not make the whole history unreadable.
    """
    try:
        text = changeset_log_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            logger.debug("cron changeset log: skipping unparseable line")
            continue
        if isinstance(row, dict) and row.get("id"):
            rows.append(row)
    return rows


def _write_head_digests(graph_digest: str, definitions_digest: str) -> None:
    """Refresh the disposable cache of the current canonical commitments."""
    from cron.jobs import ensure_dirs
    from utils import atomic_write_text

    ensure_dirs()
    try:
        atomic_write_text(
            changeset_head_path(),
            f"{graph_digest}\n{definitions_digest}\n",
            preserve_mode=True,
            create_mode=0o600,
        )
    except OSError:
        # The head is a cache; losing it costs a full read next time.
        logger.debug("cron changeset head not written", exc_info=True)


def _write_rows(rows: List[Dict[str, Any]]) -> None:
    """Rewrite the log atomically, keeping the newest ``MAX_CHANGESETS`` rows."""
    from cron.jobs import ensure_dirs
    from utils import atomic_write_text

    ensure_dirs()
    kept = rows[-MAX_CHANGESETS:]
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in kept
    )
    atomic_write_text(
        changeset_log_path(),
        payload,
        preserve_mode=True,
        create_mode=0o600,
    )
    head = kept[-1].get("digest", "") if kept else ""
    head_definitions = kept[-1].get("definition_digest", "") if kept else ""
    _write_head_digests(head, head_definitions)


def _head_digests() -> Tuple[Optional[str], Optional[str]]:
    """The current canonical (graph digest, definition digest) per the cache.

    Either is None when it can't be trusted. A cache written before definitions
    were recorded has only the first line, so its definition digest is None and
    the fast path does not fire until the log is read once. During a compatible
    encoding migration, this cache may be newer than the last row's stored
    representation; the append-only log remains untouched.
    """
    try:
        lines = changeset_head_path().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None
    graph = lines[0].strip() if lines else ""
    definitions = lines[1].strip() if len(lines) > 1 else ""
    return (graph or None), (definitions or None)


def _head_digest() -> Optional[str]:
    """The newest row's graph digest per the cache, or None when it can't be trusted."""
    return _head_digests()[0]
# =============================================================================
# Recording
# =============================================================================

def _cron_nodes(graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        node["id"]: node
        for node in (graph.get("nodes") or [])
        if isinstance(node, dict) and node.get("kind") == "cron" and node.get("id")
    }


def _edges_by_node(graph: Dict[str, Any]) -> Dict[str, set]:
    """Each node id → the canonical rows of the edges touching it.

    A job whose ``inputs`` changed has a byte-identical node row — the change
    lives entirely in its edges — so "which job changed" has to look at both or
    it would report a dataflow edit as no edit at all.
    """
    touching: Dict[str, set] = {}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        row = _edge_row(edge)
        if row is None:
            continue
        for endpoint in (edge.get("source"), edge.get("target")):
            if isinstance(endpoint, str) and endpoint:
                touching.setdefault(endpoint, set()).add(row)
    return touching


def _describe(
    before: Dict[str, Any],
    after: Dict[str, Any],
    before_definitions: Optional[Dict[str, Dict[str, Any]]] = None,
    after_definitions: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[str, str, str, List[Dict[str, Any]]]:
    """Infer ``(action, job, summary, changes)`` from the two configurations.

    ``changes`` lists the field-level definition differences of the jobs that
    changed (each entry carries its ``job``), so a row says *what* moved and not
    only that something did. When the previous row predates definition
    recording, ``before_definitions`` is None and no field diff is claimed.
    Inferred rather than passed down from the 17 mutation call sites, so the
    single hook covers every one of them — including the CLI and any future
    mutator — instead of covering the ones somebody remembered to annotate.
    ``action`` uses the client's recognized vocabulary where the change is about
    one job and falls back to ``update`` otherwise, which reads as "something
    changed" rather than as nothing.
    """
    before_nodes, after_nodes = _cron_nodes(before), _cron_nodes(after)
    before_edges, after_edges = _edges_by_node(before), _edges_by_node(after)
    defs_known = before_definitions is not None and after_definitions is not None
    before_defs = before_definitions or {}
    after_defs = after_definitions or {}
    known_before = set(before_nodes) | (set(before_defs) if defs_known else set())
    known_after = set(after_nodes) | (set(after_defs) if defs_known else set())

    added = sorted(known_after - known_before)
    removed = sorted(known_before - known_after)
    changed = sorted(
        job_id
        for job_id in known_before & known_after
        if (job_id in before_nodes and job_id in after_nodes
            and (_node_row(before_nodes[job_id]) != _node_row(after_nodes[job_id])
                 or before_edges.get(job_id, set()) != after_edges.get(job_id, set())))
        or (defs_known and before_defs.get(job_id) != after_defs.get(job_id))
    )
    changes: List[Dict[str, Any]] = []
    if defs_known:
        for job_id in changed + added + removed:
            for change in definition_changes(before_defs.get(job_id), after_defs.get(job_id)):
                changes.append({"job": job_id, **change})

    def label(job_id: str, nodes: Dict[str, Dict[str, Any]]) -> str:
        node_label = nodes.get(job_id, {}).get("label")
        if node_label:
            return _text(node_label)
        for defs in (after_defs, before_defs):
            if job_id in defs and defs[job_id].get("name"):
                return _text(defs[job_id]["name"])
        return job_id

    if len(added) == 1 and not removed and not changed:
        return "create", added[0], f"created {label(added[0], after_nodes)}", changes
    if len(removed) == 1 and not added and not changed:
        return "delete", removed[0], f"deleted {label(removed[0], before_nodes)}", changes
    if len(changed) == 1 and not added and not removed:
        fields = sorted({change["field"] for change in changes})
        detail = f" ({', '.join(fields)})" if fields else ""
        return "update", changed[0], f"updated {label(changed[0], after_nodes)}{detail}", changes
    counts = [
        f"{len(added)} added" if added else "",
        f"{len(removed)} removed" if removed else "",
        f"{len(changed)} updated" if changed else "",
    ]
    detail = ", ".join(part for part in counts if part)
    if not detail:
        # The digest moved but no cron node did: a dataflow-only change on a
        # resource node, or a service the graph no longer carries. Say that
        # instead of naming a job.
        return "update", "", "configuration changed", changes
    return "update", "", f"configuration changed ({detail})", changes
def _timestamp() -> str:
    """ISO 8601 with an offset and second precision.

    Second precision on purpose: a changeset marks a human-scale action, and the
    plain internet-date-time parsers on the reading side accept this form
    unambiguously, where a six-digit fractional part is accepted by some and not
    others — a timestamp that fails to parse degrades to "no time recorded",
    which is a worse trade than losing microseconds nobody wanted.
    """
    from hermes_time import now as _hermes_now

    return _hermes_now().replace(microsecond=0).isoformat()


def _git_commit() -> str:
    """Short git hash when the store lives in a repo, else ``""``.

    Job definitions kept in a dotfiles-style repo make a changeset joinable to a
    commit; the ordinary ``~/.hermes`` store is not a repo, and the ancestry
    check means that common case never spawns a subprocess.
    """
    from cron.jobs import _current_cron_store

    cron_dir = _current_cron_store().cron_dir
    for candidate in (cron_dir, *cron_dir.parents):
        try:
            if (candidate / ".git").exists():
                break
        except OSError:
            return ""
    else:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(cron_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _new_row(
    *,
    action: str,
    job: str,
    digest: str,
    parent_digest: str,
    summary: str,
    graph: Dict[str, Any],
    origin: ChangesetOrigin,
    definitions: Optional[Dict[str, Dict[str, Any]]] = None,
    changes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    definitions = definitions if definitions is not None else {}
    return {
        "id": uuid.uuid4().hex,
        "timestamp": _timestamp(),
        "action": action,
        "job": job,
        "digest": digest,
        "parent_digest": parent_digest,
        "actor": origin.actor,
        "summary": summary,
        "source_event_keys": list(origin.source_event_keys),
        "git_commit": _git_commit(),
        # The configuration at this revision, so a diff is a read of two rows
        # rather than a replay of the whole log.
        "graph": graph,
        # Every job's definition at this revision, under its own commitment, and
        # the field-level changes this row made against the previous one.
        "definitions": definitions,
        "definition_digest": definition_digest(definitions),
        "changes": list(changes or []),
    }
def _baseline_row(
    graph: Dict[str, Any],
    digest: str,
    definitions: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The first row in a log that starts on a store which already has jobs.

    The configuration before recording began is genuinely unknown — nobody wrote
    it down — so the log opens by stating what exists rather than by attributing
    it to whoever happened to trigger the first write. Actor is empty for the
    same reason: the person who edited one job did not create the other twelve.

    :func:`ensure_baseline` exists so this row can be laid down at read time,
    before any change, in which case it holds the true pre-change configuration
    and the next edit records as an ordinary change. When the first write beats
    the first read, that write's content is folded into this baseline instead of
    appearing as its own row.
    """
    return _new_row(
        action="baseline",
        job="",
        digest=digest,
        parent_digest="",
        summary="baseline — the configuration when recording began",
        graph=graph,
        origin=ChangesetOrigin(),
        definitions=definitions if definitions is not None else configuration_definitions(),
    )
def record_change(jobs: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Append a row iff the configuration digest moved. Returns it, or None.

    Called from ``_save_jobs_unlocked`` while the jobs lock is held, so writes
    to the log are serialized by the same lock that serializes the store.
    """
    graph = configuration_graph(jobs)
    digest = configuration_digest(graph)
    definitions = configuration_definitions(jobs)
    defs_digest = definition_digest(definitions)
    # Fast path: the cached head already says both commitments are the newest
    # row's, and no row will be written, so the log itself is never parsed.
    if _head_digests() == (digest, defs_digest):
        return None
    rows = _read_rows()
    if rows and rows[-1].get("digest") == digest:
        recorded_definitions = rows[-1].get("definitions")
        canonical_recorded = (
            _canonical_recorded_definitions(recorded_definitions)
            if isinstance(recorded_definitions, dict)
            else None
        )
        if canonical_recorded is not None and definition_digest(canonical_recorded) == defs_digest:
            # The head was stale or missing, or this is the one-time migration
            # from the first definition encoding that included repeat.completed.
            # Preserve the append-only row and refresh only the disposable head
            # cache so later runtime saves stay on the fast path instead of
            # reparsing the log forever.
            if recorded_definitions != canonical_recorded:
                _write_head_digests(digest, defs_digest)
            return None
        if rows[-1].get("definition_digest") == defs_digest:
            # A definitions-free legacy row can still have a matching explicit
            # commitment; retain the old stale-head behavior.
            return None
    if not rows:
        row = _baseline_row(graph, digest, definitions)
    else:
        previous = rows[-1]
        before = previous.get("graph") or {"nodes": [], "edges": []}
        previous_definitions = previous.get("definitions")
        if not isinstance(previous_definitions, dict):
            # The previous row predates definition recording: the graph may not
            # have moved at all, and no field diff can honestly be claimed.
            previous_definitions = None
        else:
            previous_definitions = _canonical_recorded_definitions(previous_definitions)
        action, job, summary, changes = _describe(before, graph, previous_definitions, definitions)
        if previous_definitions is None and previous.get("digest") == digest:
            action, job, summary = "update", "", "definitions recorded — the history of job definitions begins here"
        origin = current_origin()
        if origin.note:
            summary = f"{summary} — {origin.note}" if summary else origin.note
        row = _new_row(
            action=action,
            job=job,
            digest=digest,
            parent_digest=_text(previous.get("digest")),
            summary=summary,
            graph=graph,
            origin=origin,
            definitions=definitions,
            changes=changes,
        )
    rows.append(row)
    _write_rows(rows)
    return row


def ensure_baseline() -> Optional[Dict[str, Any]]:
    """Open the log on the current configuration if it has no rows yet.

    Called from the read path so the baseline captures the configuration as it
    stands *before* anyone changes it. Takes the jobs lock (re-entrantly, so it
    is safe from a caller that already holds it) because it loads the store.
    """
    if _head_digest():
        return None
    if _read_rows():
        return None

    from cron.jobs import _jobs_lock

    with _jobs_lock():
        if _read_rows():
            return None
        graph = configuration_graph()
        row = _baseline_row(graph, configuration_digest(graph), configuration_definitions())
        _write_rows([row])
        return row


# =============================================================================
# Reading (the cron.changesets / cron.changeset_diff surface)
# =============================================================================

_WIRE_KEYS = (
    "id",
    "timestamp",
    "action",
    "job",
    "digest",
    "parent_digest",
    "actor",
    "summary",
    "source_event_keys",
    "git_commit",
    "definition_digest",
)
def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    """A row as the client reads it — without the graph snapshot.

    The snapshots are what make a diff cheap, and they are also by far the
    largest part of a row; a page of 50 would be megabytes of graphs nobody
    asked for. ``cron.changeset_diff`` serves them one pair at a time. The page
    does carry which definition fields a row changed, per job, so a list can say
    "prompt, model" without fetching the values.
    """
    public = {key: row.get(key) for key in _WIRE_KEYS if key in row}
    changes = row.get("changes")
    if isinstance(changes, list):
        by_job: Dict[str, List[str]] = {}
        for change in changes:
            if isinstance(change, dict) and change.get("field"):
                fields = by_job.setdefault(_text(change.get("job")), [])
                if change["field"] not in fields:
                    fields.append(change["field"])
        public["changed_fields"] = by_job
    return public
def _instant(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _in_window(row: Dict[str, Any], since: Optional[str], until: Optional[str]) -> bool:
    """Whether a row falls inside the requested window.

    A row or bound this can't parse is *kept*: a filter that silently drops what
    it can't judge turns an unreadable timestamp into a missing change, and a
    missing change is the one thing a history must not have.
    """
    stamp = _instant(row.get("timestamp"))
    if stamp is None:
        return True
    for bound, keep_after in ((since, True), (until, False)):
        if not bound:
            continue
        edge = _instant(bound)
        if edge is None:
            continue
        if stamp.tzinfo is None or edge.tzinfo is None:
            stamp_cmp = stamp.replace(tzinfo=None)
            edge_cmp = edge.replace(tzinfo=None)
        else:
            stamp_cmp, edge_cmp = stamp, edge
        if keep_after and stamp_cmp < edge_cmp:
            return False
        if not keep_after and stamp_cmp > edge_cmp:
            return False
    return True


def read_changesets(
    *,
    limit: int = 50,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
    job: Optional[str] = None,
) -> Dict[str, Any]:
    """One page of recorded changes, newest first.

    ``total`` counts the rows matching the filters, not the rows returned, so a
    client can tell a short page from the end of the history.
    """
    rows = list(reversed(_read_rows()))
    wanted = (job or "").strip()
    matches = [
        row
        for row in rows
        if (not wanted or _text(row.get("job")) == wanted)
        and _in_window(row, since, until)
    ]
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    window = matches[offset : offset + limit]
    return {
        "changesets": [_public(row) for row in window],
        "total": len(matches),
        "limit": limit,
        "offset": offset,
    }


def read_changeset_diff(changeset_id: str) -> Optional[Dict[str, Any]]:
    """The configurations on either side of one recorded change.

    Graphs, not sentences. The client derives its statements from these with the
    same code it uses on its own observed log; a second dialect of "what
    changed", produced here, would drift from that one and leave a reader with
    two accounts and no way to choose.

    ``before`` is omitted when the previous revision's snapshot is not in the
    log — the row is the baseline, or its parent has been trimmed away. Omitting
    it is not the same as sending an empty graph, and the client's rule turns on
    exactly that: a missing ``before`` with a non-empty ``parent_digest`` means
    the comparison isn't available, where an empty one would report a
    steady-state configuration as freshly built.
    """
    wanted = (changeset_id or "").strip()
    if not wanted:
        return None
    rows = _read_rows()
    for index, row in enumerate(rows):
        if row.get("id") != wanted:
            continue
        payload: Dict[str, Any] = {"after": row.get("graph") or {"nodes": [], "edges": []}}
        if isinstance(row.get("definitions"), dict):
            payload["definitions_after"] = row["definitions"]
        if isinstance(row.get("changes"), list):
            payload["changes"] = row["changes"]
        if index > 0:
            before = rows[index - 1].get("graph")
            if isinstance(before, dict):
                payload["before"] = before
            before_definitions = rows[index - 1].get("definitions")
            if isinstance(before_definitions, dict):
                payload["definitions_before"] = before_definitions
        return payload
    return None


def read_job_revisions(job_id: str, *, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """One job's definition history, newest first: every recorded row at which the
    job appeared, disappeared or changed a field, each with the definition as it
    stood and the field-level changes against the revision before it.

    Derived from the same rows as everything else here, so it cannot disagree
    with the configuration history; a row that changed several jobs at once
    appears in each of their histories with only its own fields. A row that
    predates definition recording can only say the job was present.
    """
    wanted = (job_id or "").strip()
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    if not wanted:
        return {"job_id": wanted, "revisions": [], "total": 0, "limit": limit, "offset": offset}
    revisions: List[Dict[str, Any]] = []
    previous_definition: Optional[Dict[str, Any]] = None
    previous_known = False
    for row in _read_rows():
        definitions = row.get("definitions")
        known = isinstance(definitions, dict)
        if not known:
            # Only the graph can say whether the job existed at this row.
            if wanted in _cron_nodes(row.get("graph") or {}) and previous_definition is None and not previous_known:
                revisions.append(_revision_entry(row, action=_text(row.get("action"), "update"), definition=None, changes=[], known=False))
            previous_definition = None
            previous_known = False
            continue
        assert isinstance(definitions, dict)
        definitions = _canonical_recorded_definitions(definitions)
        definition = definitions.get(wanted)
        present = definition is not None
        was_present = previous_definition is not None
        if present and not was_present:
            action = "baseline" if row.get("action") == "baseline" or not previous_known else "create"
            changes = definition_changes(None, definition) if previous_known else []
            revisions.append(_revision_entry(row, action=action, definition=definition, changes=changes, known=True))
        elif present and was_present and definition != previous_definition:
            revisions.append(_revision_entry(row, action="update", definition=definition,
                                             changes=definition_changes(previous_definition, definition), known=True))
        elif was_present and not present:
            revisions.append(_revision_entry(row, action="delete", definition=None,
                                             changes=definition_changes(previous_definition, None), known=True))
        previous_definition = definition
        previous_known = True
    revisions.reverse()
    return {
        "job_id": wanted,
        "revisions": revisions[offset : offset + limit],
        "total": len(revisions),
        "limit": limit,
        "offset": offset,
    }


def _revision_entry(row: Dict[str, Any], *, action: str, definition: Optional[Dict[str, Any]],
                    changes: List[Dict[str, Any]], known: bool) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "timestamp": row.get("timestamp"),
        "action": action,
        "actor": row.get("actor"),
        "summary": row.get("summary"),
        "source_event_keys": list(row.get("source_event_keys") or []),
        "git_commit": row.get("git_commit"),
        "digest": row.get("digest"),
        "definition_digest": row.get("definition_digest"),
        "definition": definition,
        "changes": changes,
        "definition_recorded": known,
    }
