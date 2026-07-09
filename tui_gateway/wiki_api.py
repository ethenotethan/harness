"""
Wiki scanning API for the TUI gateway.

Provides filesystem-level wiki introspection for native clients that
render graph views or page detail. Supports a wiki name (e.g. "d-inference")
that resolves to a path via ~/.hermes/wikis.yaml, falling back to
$WIKI_PATH or ~/wiki.

Multi-wiki support via ~/.hermes/wikis.yaml registry.

v2 (2026-06-12): Adds hierarchical taxonomy (tag_path), taxonomy tree serving,
and integration link expansion for project management systems.
"""
import os
import re
from pathlib import Path
from typing import Optional
import yaml


def _load_wiki_registry() -> dict:
    """Load ~/.hermes/wikis.yaml, returning {name: path} dict.
    Returns empty dict if file doesn't exist or is unparseable.
    """
    registry_path = Path(os.path.expanduser("~/.hermes/wikis.yaml"))
    if not registry_path.exists():
        return {}
    try:
        with open(registry_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    wikis = data.get("wikis", {})
    if not isinstance(wikis, dict):
        return {}
    resolved = {}
    for name, path in wikis.items():
        if isinstance(path, str):
            resolved[str(name)] = os.path.expanduser(path)
    return resolved


def resolve_wiki(name: Optional[str] = None) -> str:
    """Resolve a wiki name to a filesystem path.

    Resolution order:
    1. If name matches a key in ~/.hermes/wikis.yaml -> use that path
    2. If name looks like a path (~ or / prefix) -> expand and use directly
    3. If name is None/empty -> use registry's 'default' key
    4. Fall back to $WIKI_PATH env var
    5. Final fallback: ~/wiki
    """
    registry = _load_wiki_registry()

    if name:
        # Try registry name match
        if name in registry:
            return registry[name]
        # Try raw path
        if name.startswith("~") or name.startswith("/"):
            return os.path.expanduser(name)

    # No name or name not found - use default
    if registry:
        # Read raw YAML to get the default key
        registry_path = Path(os.path.expanduser("~/.hermes/wikis.yaml"))
        try:
            with open(registry_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            default_name = data.get("default")
            if default_name and default_name in registry:
                return registry[default_name]
        except Exception:
            pass

    # Fallbacks
    env = os.environ.get("WIKI_PATH", "")
    if env:
        return env
    return os.path.expanduser("~/wiki")


def wiki_list() -> dict:
    """Return list of available wikis from ~/.hermes/wikis.yaml."""
    registry = _load_wiki_registry()
    wikis = []
    for name, path in registry.items():
        wikis.append({"name": name, "path": path})
    return {"wikis": wikis}


def _default_wiki_path() -> str:
    return resolve_wiki(None)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter — returns (metadata, body).
    
    Handles both simple key:value and multi-line YAML list fields
    (tag_path, integration_links, sources).
    """
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    metadata = {}
    current_key = None
    current_list = None
    for line in parts[1].split("\n"):
        # Check for indented list item (YAML list)
        if line.startswith("  - ") and current_key:
            if current_list is None:
                current_list = []
            value = line.strip()[2:].strip().strip('"').strip("'")
            current_list.append(value)
            continue
        
        # Flush previous key's list
        if current_key and current_list is not None:
            metadata[current_key] = current_list
            current_key = None
            current_list = None
        
        line = line.strip()
        if not line:
            current_key = None
            current_list = None
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            # strip outer quotes
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if val:  # scalar value
                metadata[key] = val
            else:  # starts a list on next lines
                current_key = key
                current_list = None
    
    # Flush final key's list
    if current_key and current_list is not None:
        metadata[current_key] = current_list
    
    return metadata, parts[2]


def _extract_wikilinks(body: str) -> list[str]:
    """Extract [[wikilinks]] from markdown body."""
    pattern = r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]'
    matches = re.findall(pattern, body)
    return [m.strip().lower().replace(" ", "-") for m in matches]


#: Content subdirectories scanned for wiki pages. Root-level *.md files
#: (index.md, log.md, ...) are scanned too — see _iter_page_files.
WIKI_SUBDIRS = ["entities", "concepts", "comparisons", "queries", "raw",
                "projects", "goals", "life", "issues"]


def _iter_page_files(wiki: Path):
    """Yield (subdir, file) for every wiki page markdown file.

    Covers the content subdirectories plus root-level pages (subdir "" —
    e.g. index.md, log.md), which previously never appeared in wiki.scan
    and were therefore invisible in graph clients.
    """
    for subdir in [""] + WIKI_SUBDIRS:
        dir_path = wiki / subdir if subdir else wiki
        if not dir_path.exists():
            continue
        for file in sorted(dir_path.iterdir()):
            if file.suffix != ".md" or not file.is_file():
                continue
            yield subdir, file


def wiki_scan(wiki_path: Optional[str] = None) -> dict:
    """Scan wiki directory and return graph structure."""
    wiki = Path(wiki_path or _default_wiki_path())
    if not wiki.exists():
        return {"pages": [], "links": []}

    pages: list[dict] = []
    page_ids: set[str] = set()
    links: list[dict] = []

    # First pass: collect all pages
    for subdir, file in _iter_page_files(wiki):
        try:
            content = file.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _ = _parse_frontmatter(content)
        slug = file.stem
        rel_path = f"{subdir}/{file.name}" if subdir else file.name

        # Parse tags (handles "[tag1, tag2]" or "tag1, tag2")
        raw_tags = fm.get("tags", "")
        tags: list[str] = []
        if raw_tags:
            cleaned = raw_tags.strip().strip("[]").replace("'", "").replace('"', "")
            tags = [t.strip() for t in cleaned.split(",") if t.strip()]

        # Root-level pages (index/log) are meta pages unless frontmatter
        # says otherwise; subdir pages keep the old "concept" default.
        default_type = "meta" if not subdir else "concept"

        pages.append(
            {
                "id": slug,
                "title": fm.get("title", slug),
                "type": fm.get("type", default_type),
                "tags": tags,
                "tag_path": fm.get("tag_path", []) if isinstance(fm.get("tag_path"), list) else [],
                "integration_links": fm.get("integration_links", []) if isinstance(fm.get("integration_links"), list) else [],
                "path": rel_path,
                "created": fm.get("created", ""),
                "updated": fm.get("updated", ""),
                "confidence": fm.get("confidence", ""),
                "contested": fm.get("contested", "").lower() == "true",
            }
        )
        page_ids.add(slug)

    # Second pass: extract wikilinks (only link to existing pages)
    for _subdir, file in _iter_page_files(wiki):
        try:
            content = file.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _parse_frontmatter(content)
        slug = file.stem
        for target in _extract_wikilinks(body):
            if target in page_ids:
                links.append({"source": slug, "target": target, "type": "wikilink"})

    return {"pages": pages, "links": links}


def wiki_page(path: str, wiki_path: Optional[str] = None) -> Optional[dict]:
    """Read a single wiki page by relative path (e.g. 'entities/dflash-mlx.md')."""
    wiki = Path(wiki_path or _default_wiki_path())
    target = wiki / path
    # Security: refuse to escape the wiki directory
    try:
        target = target.resolve()
        wiki = wiki.resolve()
    except Exception:
        return None
    if not str(target).startswith(str(wiki)):
        return None
    if not target.exists() or target.suffix != ".md":
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = _parse_frontmatter(content)
    return {"frontmatter": fm, "body": body, "path": path}


def wiki_taxonomy(wiki_path: Optional[str] = None) -> Optional[dict]:
    """Load and return the hierarchical taxonomy tree from taxonomy.yaml.
    
    Returns the full taxonomy dict with categories and nested children,
    or None if taxonomy.yaml doesn't exist."""
    wiki = Path(wiki_path or _default_wiki_path())
    taxonomy_path = wiki / "taxonomy.yaml"
    if not taxonomy_path.exists():
        return None
    try:
        with open(taxonomy_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return None


def wiki_flatten_taxonomy(wiki_path: Optional[str] = None) -> list[str]:
    """Return a flat list of all valid taxonomy paths from taxonomy.yaml."""
    tree = wiki_taxonomy(wiki_path)
    if not tree:
        return []
    
    def _flatten(categories, prefix=""):
        paths = []
        for name, node in categories.items():
            if not isinstance(node, dict):
                continue
            path = f"{prefix}{name}" if prefix else name
            paths.append(path)
            if "children" in node and isinstance(node["children"], dict):
                paths.extend(_flatten(node["children"], f"{path}/"))
        return paths
    
    return sorted(_flatten(tree.get("categories", {})))


def _load_wiki_changeset_module(required_attr: str):
    """Load the wiki_changeset helper, preferring the repo-bundled copy.

    The previous sys.path approach let a STALE deployed copy in
    ~/.hermes/scripts shadow the repo's updated module (and once cached in
    sys.modules it kept winning) — surfacing to clients as
    "cannot import name 'wiki_changeset_diff' from 'wiki_changeset'".

    Load by explicit file path via importlib instead: repo copy first, user
    copy as fallback — and only accept a copy that actually has the symbol
    the caller needs, so version skew degrades to the next candidate rather
    than a confusing ImportError from the wrong file.
    """
    import importlib.util
    import os as _os

    repo_copy = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "scripts", "wiki_changeset.py",
    )
    user_copy = _os.path.join(
        _os.path.expanduser("~"), ".hermes", "scripts", "wiki_changeset.py"
    )

    tried = []
    for path in (repo_copy, user_copy):
        if not _os.path.exists(path):
            continue
        tried.append(path)
        try:
            spec = importlib.util.spec_from_file_location("_hermes_wiki_changeset", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            continue
        if hasattr(module, required_attr):
            return module
    raise ImportError(
        f"no wiki_changeset module providing {required_attr!r} found "
        f"(tried: {tried or [repo_copy, user_copy]}) — "
        "the gateway install may predate this feature"
    )


def wiki_changesets(
    wiki_path: Optional[str] = None,
    page: Optional[str] = None,
    action: Optional[str] = None,
    trigger: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Query wiki changesets (timeline view).

    Args:
        wiki_path: Wiki root path override
        page: Filter by page path
        action: Filter by action ('create', 'update', 'archive', 'delete')
        trigger: Filter by trigger ('ingest', 'query', 'lint', 'process-inbox')
        limit: Max results (default 50)
        offset: Pagination offset
        since: ISO timestamp filter (after)
        until: ISO timestamp filter (before)

    Returns:
        {"changesets": [...], "total": N, "limit": L, "offset": O}
    """
    module = _load_wiki_changeset_module("wiki_query_changesets")
    return module.wiki_query_changesets(
        wiki_path=wiki_path,
        page=page,
        action=action,
        trigger=trigger,
        limit=limit,
        offset=offset,
        since=since,
        until=until,
    )


def wiki_changeset_diff(changeset_id: str, wiki_path: Optional[str] = None) -> dict:
    """Return the unified git diff for one changeset (timeline detail view).

    Args:
        changeset_id: Changeset id from wiki.changesets (e.g. '2026-06-28T140819-001')
        wiki_path: Wiki root path override

    Returns:
        {"diff": "<unified diff>", "changeset": {...}} or {"error": ...}
    """
    module = _load_wiki_changeset_module("wiki_changeset_diff")
    return module.wiki_changeset_diff(changeset_id, wiki_path=wiki_path)


def wiki_expand_links(page_slug: str, wiki_path: Optional[str] = None) -> dict:
    """Expand integration_links for a wiki page into live status.
    
    Currently resolves GitHub and Linear links. Returns a dict
    mapping each link to a status object. Other link types return
    a 'pending' status with the original value.
    
    Example return:
        {"github:hermes-agent#456": {"status": "merged", "title": "Fix wiki...", "url": "..."}}
    """
    wiki = Path(wiki_path or _default_wiki_path())

    # Find the page by slug (root-level pages like index/log included)
    for subdir in [""] + WIKI_SUBDIRS:
        file_path = (wiki / subdir if subdir else wiki) / f"{page_slug}.md"
        if file_path.exists():
            break
    else:
        return {"error": f"Page '{page_slug}' not found"}
    
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return {"error": f"Could not read '{page_slug}'"}
    
    fm, _ = _parse_frontmatter(content)
    links = fm.get("integration_links", [])
    if not isinstance(links, list):
        return {}
    
    result = {}
    for link in links:
        if not isinstance(link, str) or ":" not in link:
            continue
        prefix, rest = link.split(":", 1)
        prefix = prefix.lower()
        
        if prefix == "github":
            # Parse org/repo#num
            if "#" in rest:
                repo_path, num = rest.rsplit("#", 1)
                result[link] = {
                    "type": "github",
                    "repo": repo_path,
                    "number": num,
                    "url": f"https://github.com/{repo_path}/pull/{num}",
                    "status": "unknown",
                    "title": f"{repo_path}#{num}",
                }
            else:
                result[link] = {"type": "github", "repo": rest, "status": "unknown", "title": rest}
        elif prefix == "linear":
            result[link] = {
                "type": "linear",
                "issue_id": rest,
                "url": f"https://linear.app/issue/{rest}",
                "status": "unknown",
                "title": rest,
            }
        elif prefix == "notion":
            result[link] = {"type": "notion", "url": rest, "status": "unknown", "title": "Notion page"}
        elif prefix == "obsidian":
            result[link] = {"type": "obsidian", "note": rest, "status": "unknown", "title": rest}
        elif prefix == "slack":
            result[link] = {"type": "slack", "channel_msg": rest, "status": "unknown", "title": rest}
        else:
            result[link] = {"type": prefix, "raw": rest, "status": "unknown", "title": f"{prefix}:{rest}"}
    
    return result
