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
  python3 wiki.py capture PATH ACTION SUMMARY [--trigger T] [--source SRC]
                              [--wiki NAME]

ACTION  ∈ create | update | archive | delete
TRIGGER ∈ ingest | query | lint | process-inbox | manual   (default: manual)

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
            lines.append(
                f"  {c.get('timestamp','')}  {c.get('action','?'):7} "
                f"{c.get('page','')}  +{stats.get('lines_added',0)}/-{stats.get('lines_removed',0)} "
                f"[{c.get('trigger','')}]  {c.get('summary','')}"
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


def cmd_capture(a):
    if wiki_changeset is None:
        sys.exit(
            "error: could not import wiki_changeset — ensure scripts/wiki_changeset.py "
            "is on the path (~/.hermes/scripts or a repo checkout)."
        )
    res = wiki_changeset.wiki_capture_changeset(
        page_path=a.path,
        action=a.action,
        summary=a.summary,
        trigger=a.trigger,
        source=a.source,
        wiki_path=_resolve(a),
    )
    if isinstance(res, dict) and res.get("error"):
        sys.exit(f"error: {res['error']}")
    cid = res.get("id") if isinstance(res, dict) else None
    print(f"captured changeset {cid or ''}: {a.action} {a.path}".rstrip())


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

    s = sub.add_parser("capture", help="record a changeset after writing a page")
    s.add_argument("path"); s.add_argument("action", choices=["create", "update", "archive", "delete"])
    s.add_argument("summary")
    s.add_argument("--trigger", default="manual",
                   choices=["ingest", "query", "lint", "process-inbox", "manual"])
    s.add_argument("--source", default="")
    s.set_defaults(func=cmd_capture)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
