"""Wiki scanning API for the TUI gateway.

Provides filesystem-level wiki introspection for native clients that
render graph views or page detail. Supports a wiki name (e.g. "d-inference")
that resolves to a path via ~/.hermes/wikis.yaml, falling back to
$WIKI_PATH or ~/wiki.

Multi-wiki support via ~/.hermes/wikis.yaml registry.
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
        with open(registry_path) as f:
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
            with open(registry_path) as f:
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
    """Simple frontmatter parser — returns (metadata, body)."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    metadata = {}
    for line in parts[1].strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            # strip outer quotes
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            metadata[key] = val
    return metadata, parts[2]


def _extract_wikilinks(body: str) -> list[str]:
    """Extract [[wikilinks]] from markdown body."""
    pattern = r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]'
    matches = re.findall(pattern, body)
    return [m.strip().lower().replace(" ", "-") for m in matches]


def wiki_scan(wiki_path: Optional[str] = None) -> dict:
    """Scan wiki directory and return graph structure."""
    wiki = Path(wiki_path or _default_wiki_path())
    if not wiki.exists():
        return {"pages": [], "links": []}

    pages: list[dict] = []
    page_ids: set[str] = set()
    links: list[dict] = []
    subdirs = ["entities", "concepts", "comparisons", "queries", "raw"]

    # First pass: collect all pages
    for subdir in subdirs:
        dir_path = wiki / subdir
        if not dir_path.exists():
            continue
        for file in dir_path.iterdir():
            if file.suffix != ".md":
                continue
            try:
                content = file.read_text(encoding="utf-8")
            except Exception:
                continue
            fm, _ = _parse_frontmatter(content)
            slug = file.stem
            rel_path = f"{subdir}/{file.name}"

            # Parse tags (handles "[tag1, tag2]" or "tag1, tag2")
            raw_tags = fm.get("tags", "")
            tags: list[str] = []
            if raw_tags:
                cleaned = raw_tags.strip().strip("[]").replace("'", "").replace('"', "")
                tags = [t.strip() for t in cleaned.split(",") if t.strip()]

            pages.append(
                {
                    "id": slug,
                    "title": fm.get("title", slug),
                    "type": fm.get("type", "concept"),
                    "tags": tags,
                    "path": rel_path,
                    "created": fm.get("created", ""),
                    "updated": fm.get("updated", ""),
                    "confidence": fm.get("confidence", ""),
                    "contested": fm.get("contested", "").lower() == "true",
                }
            )
            page_ids.add(slug)

    # Second pass: extract wikilinks (only link to existing pages)
    for subdir in subdirs:
        dir_path = wiki / subdir
        if not dir_path.exists():
            continue
        for file in dir_path.iterdir():
            if file.suffix != ".md":
                continue
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
