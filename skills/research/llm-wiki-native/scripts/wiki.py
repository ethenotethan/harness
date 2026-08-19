#!/usr/bin/env python3
"""
wiki.py — thin CLI over the native Hermes wiki API.

Wraps tui_gateway/wiki_api.py and scripts/wiki_changeset.py so the agent can
drive the structured wiki (scan / page / taxonomy / changesets / expand-links)
and record changesets through the SAME code path the native app reads — instead
of re-implementing graph walks and changeset capture with raw filesystem tools.

This is what makes the native app's wiki views (graph + Timeline tab) reflect
the agent's work: every page write goes through `capture`, which appends to
`<wiki>/changesets/index.json`, which `wiki.changesets` serves.

Usage (through the `terminal` tool):
  python3 wiki.py scan        [--wiki NAME] [--json]
  python3 wiki.py page PATH   [--wiki NAME] [--json]
  python3 wiki.py taxonomy    [--wiki NAME]
  python3 wiki.py expand SLUG [--wiki NAME]
  python3 wiki.py changesets  [--wiki NAME] [--page PATH] [--action A]
                              [--trigger T] [--since ISO] [--until ISO]
                              [--limit N] [--offset N] [--json]
  python3 wiki.py events      [--wiki NAME] [--kind K] [--since ISO] [--until ISO]
                              [--limit N] [--offset N] [--json]
  python3 wiki.py capture PATH ACTION SUMMARY [--trigger T]
                              [--source-event RAW_PATH]... [--wiki NAME]

ACTION  ∈ create | update | archive | delete
TRIGGER — conventionally ingest | query | lint | process-inbox | manual
          (default: manual), but any kind the wiki declares via a
          `type: event-type` page is valid; the taxonomy is the wiki's.

Pass --source-event once per event that caused the change. Omitting it records
provenance as *unknown*, which is honest for a hand edit and a gap for an
ingest — `events` shows which sources have produced nothing.

The wiki is resolved by NAME via ~/.hermes/wikis.yaml, else $WIKI_PATH, else
~/wiki — identical to the gateway, so a name here means the same wiki there.
"""
import argparse
import json
import os
import sys


def _bootstrap_imports():
    """Make tui_gateway.wiki_api and scripts.wiki_changeset importable.

    The skill ships under ~/.hermes/skills/...; the helper modules live in the
    hermes-agent repo (and a copy under ~/.hermes/scripts). Probe both so the
    skill works whether run from a checkout or an installed Hermes.
    """
    candidates = []
    # Installed layout: ~/.hermes/scripts holds wiki_changeset.py
    candidates.append(os.path.join(os.path.expanduser("~"), ".hermes", "scripts"))
    # Repo layout: walk up looking for a dir containing tui_gateway/wiki_api.py
    here = os.path.dirname(os.path.abspath(__file__))
    node = here
    for _ in range(8):
        if os.path.exists(os.path.join(node, "tui_gateway", "wiki_api.py")):
            candidates.append(node)
            candidates.append(os.path.join(node, "scripts"))
            break
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent
    for d in candidates:
        if d and d not in sys.path and os.path.isdir(d):
            sys.path.insert(0, d)


_bootstrap_imports()

try:
    from tui_gateway import wiki_api
except Exception:  # pragma: no cover - import shape varies by install
    wiki_api = None
try:
    import wiki_changeset
except Exception:  # pragma: no cover
    wiki_changeset = None


def _need_api():
    if wiki_api is None:
        sys.exit(
            "error: could not import tui_gateway.wiki_api — run this from a "
            "hermes-agent checkout or an installed Hermes (~/.hermes/scripts)."
        )


def _print(obj, as_json):
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return
    print(_human(obj))


def _human(obj) -> str:
    """Compact human-readable rendering for the common shapes."""
    if isinstance(obj, dict) and "pages" in obj and "links" in obj:
        lines = [f"{len(obj['pages'])} pages, {len(obj['links'])} links"]
        for p in obj["pages"]:
            lines.append(f"  [{p.get('type','?'):11}] {p.get('path','')}  — {p.get('title','')}")
        return "\n".join(lines)
    if isinstance(obj, dict) and "changesets" in obj:
        lines = [f"{obj.get('total', len(obj['changesets']))} changesets "
                 f"(showing {len(obj['changesets'])}, offset {obj.get('offset', 0)})"]
        for c in obj["changesets"]:
            stats = c.get("diff_stats", {}) or {}
            keys = c.get("source_event_keys") or []
            # Surface unknown provenance rather than leaving a blank: the point
            # of the field is that its absence is visible.
            provenance = f" ← {', '.join(keys)}" if keys else " ← unknown"
            lines.append(
                f"  {c.get('timestamp','')}  {c.get('action','?'):7} "
                f"{c.get('page','')}  +{stats.get('lines_added',0)}/-{stats.get('lines_removed',0)} "
                f"[{c.get('trigger','')}]  {c.get('summary','')}{provenance}"
            )
        return "\n".join(lines)
    if isinstance(obj, dict) and "events" in obj:
        lines = [f"{obj.get('total', len(obj['events']))} events "
                 f"(showing {len(obj['events'])}, offset {obj.get('offset', 0)})"]
        for e in obj["events"]:
            caused = e.get("changesets") or []
            effect = f"→ {len(caused)} change(s)" if caused else "→ nothing yet"
            lines.append(
                f"  {e.get('timestamp','') or '(undated)':20}  [{e.get('kind','') or '?'}]  "
                f"{e.get('key','')}  {effect}"
            )
        return "\n".join(lines)
    return json.dumps(obj, indent=2, ensure_ascii=False)


def cmd_scan(a):
    _need_api()
    _print(wiki_api.wiki_scan(wiki_path=_resolve(a)), a.json)


def cmd_page(a):
    _need_api()
    res = wiki_api.wiki_page(a.path, wiki_path=_resolve(a))
    if res is None:
        sys.exit(f"error: page not found or outside wiki: {a.path}")
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"# {res['frontmatter'].get('title', a.path)}  ({a.path})")
        print(res["body"].strip())


def cmd_taxonomy(a):
    _need_api()
    paths = wiki_api.wiki_flatten_taxonomy(wiki_path=_resolve(a))
    if not paths:
        print("(no taxonomy.yaml — taxonomy is optional)")
        return
    print("\n".join(paths))


def cmd_expand(a):
    _need_api()
    _print(wiki_api.wiki_expand_links(a.slug, wiki_path=_resolve(a)), True)


def cmd_changesets(a):
    _need_api()
    res = wiki_api.wiki_changesets(
        wiki_path=_resolve(a),
        page=a.page,
        action=a.action,
        trigger=a.trigger,
        limit=a.limit,
        offset=a.offset,
        since=a.since,
        until=a.until,
    )
    _print(res, a.json)


def cmd_events(a):
    _need_api()
    res = wiki_api.wiki_events(
        wiki_path=_resolve(a),
        kind=a.kind,
        limit=a.limit,
        offset=a.offset,
        since=a.since,
        until=a.until,
    )
    _print(res, a.json)


def cmd_capture(a):
    if wiki_changeset is None:
        sys.exit(
            "error: could not import wiki_changeset — ensure scripts/wiki_changeset.py "
            "is on the path (~/.hermes/scripts or a repo checkout)."
        )
    wiki_path = _resolve(a)
    res = wiki_changeset.wiki_capture_changeset(
        page_path=a.path,
        action=a.action,
        summary=a.summary,
        trigger=a.trigger,
        source=a.source,
        source_events=getattr(a, "source_events", None),
        wiki_path=wiki_path,
    )
    if isinstance(res, dict) and res.get("error"):
        sys.exit(f"error: {res['error']}")
    cid = res.get("id") if isinstance(res, dict) else None
    keys = res.get("source_event_keys") or [] if isinstance(res, dict) else []
    # Domain metadata the write path can't infer (an opaque `article:<hash>`
    # key carries no url on its own). The ingester supplies it here and we fill
    # it onto each event record the capture just materialized — idempotent, so
    # it enriches without clobbering anything an earlier call recorded.
    event_kind = getattr(a, "event_kind", None)
    event_url = getattr(a, "event_url", None)
    if (event_kind or event_url) and hasattr(wiki_changeset, "wiki_record_event"):
        for key in keys:
            wiki_changeset.wiki_record_event(
                key, kind=event_kind, source_url=event_url,
                trigger=a.trigger, wiki_path=wiki_path,
            )
    # Say when provenance is missing. A silent capture is how a KB accumulates
    # unknowns nobody notices until the whole log is untrustworthy.
    provenance = f" ← {', '.join(keys)}" if keys else "  [provenance: unknown]"
    print(f"captured changeset {cid or ''}: {a.action} {a.path}{provenance}".rstrip())


def cmd_backfill_events(a):
    if wiki_changeset is None or not hasattr(wiki_changeset, "wiki_backfill_events"):
        sys.exit(
            "error: wiki_changeset.wiki_backfill_events unavailable — the bundled "
            "wiki_changeset.py may predate the materialized event log."
        )
    res = wiki_changeset.wiki_backfill_events(wiki_path=_resolve(a))
    if getattr(a, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(
            f"backfill-events: scanned {res.get('scanned_changesets', 0)} changesets, "
            f"{res.get('distinct_keys', 0)} distinct source keys "
            f"({res.get('created', 0)} created, {res.get('already_present', 0)} already present)"
        )


def _resolve(a):
    """Resolve --wiki NAME to a path via the gateway's own resolver when present."""
    name = getattr(a, "wiki", None)
    if not name:
        return None
    if wiki_api is not None:
        return wiki_api.resolve_wiki(name)
    if name.startswith("~") or name.startswith("/"):
        return os.path.expanduser(name)
    return name


def main(argv=None):
    p = argparse.ArgumentParser(description="CLI over the native Hermes wiki API")
    p.add_argument("--wiki", help="wiki name (wikis.yaml) or path; default resolves like the gateway")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="graph structure: pages + links")
    s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_scan)

    s = sub.add_parser("page", help="read one page by relative path")
    s.add_argument("path"); s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_page)

    s = sub.add_parser("taxonomy", help="flat list of valid taxonomy paths")
    s.set_defaults(func=cmd_taxonomy)

    s = sub.add_parser("expand", help="expand a page's integration_links")
    s.add_argument("slug"); s.set_defaults(func=cmd_expand)

    s = sub.add_parser("changesets", help="query the edit timeline")
    s.add_argument("--page"); s.add_argument("--action"); s.add_argument("--trigger")
    s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--limit", type=int, default=50); s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_changesets)

    s = sub.add_parser("events", help="ingestion event log — what caused wiki updates")
    s.add_argument("--kind"); s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--limit", type=int, default=200); s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_events)

    s = sub.add_parser("capture", help="record a changeset after writing a page")
    s.add_argument("path"); s.add_argument("action", choices=["create", "update", "archive", "delete"])
    s.add_argument("summary")
    # Deliberately not `choices=[...]`: what an event kind IS belongs to the
    # wiki (a `type: event-type` page), not to this parser. A closed list here
    # would mean adding an ingestion source requires editing this file.
    s.add_argument("--trigger", default="manual",
                   help="event kind — conventionally ingest | query | lint | "
                        "process-inbox | manual, but any kind a wiki declares")
    s.add_argument("--source", default="", help="legacy single source path")
    s.add_argument("--source-event", action="append", default=[], dest="source_events",
                   metavar="RAW_PATH",
                   help="event that caused this change (repeatable) — "
                        "e.g. --source-event raw/articles/src.md")
    # Domain metadata the write path can't infer from an opaque event key.
    # Threaded onto the materialized event record for each --source-event.
    s.add_argument("--event-kind", default=None,
                   help="kind for the caused event(s), e.g. github_pr | arxiv | snapshot")
    s.add_argument("--event-url", default=None,
                   help="source URL for the caused event(s)")
    s.set_defaults(func=cmd_capture)

    s = sub.add_parser("backfill-events",
                       help="materialize an event record per source key across all changesets (idempotent)")
    s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_backfill_events)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
