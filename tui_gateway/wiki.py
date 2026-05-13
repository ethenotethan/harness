"""Wiki scanner for the Karpathy compendium pattern.

Scans a markdown wiki directory (default ``~/wiki``) and returns a graph
of pages + wikilink edges suitable for rendering in HermesNative or the TUI.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# Front matter delimiter: exactly three hyphens on their own line.
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)

_WIKI_SUBDIRS = ("entities", "concepts", "comparisons", "queries")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and remaining body from markdown text.

    Returns ``({}, text)`` when no frontmatter is found or YAML is invalid.
    """
    m = _FM_RE.match(text)
    if not m or yaml is None:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            return {}, text
        return fm, text[m.end() :]
    except Exception:
        return {}, text


def scan(wiki_path: str | None = None) -> dict[str, Any]:
    """Walk *wiki_path* and return pages + links.

    Returns a dict with ``pages`` (list of page metadata) and ``links``
    (list of directed wikilink edges).
    """
    root = Path(wiki_path or os.path.expanduser("~/wiki")).expanduser().resolve()
    if not root.is_dir():
        return {"pages": [], "links": []}

    pages: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    for subdir_name in _WIKI_SUBDIRS:
        subdir = root / subdir_name
        if not subdir.is_dir():
            continue
        for file in sorted(subdir.glob("*.md")):
            try:
                text = file.read_text(encoding="utf-8")
            except Exception:
                continue
            fm, body = _parse_frontmatter(text)
            page_id = file.stem
            rel_path = str(file.relative_to(root))

            pages.append(
                {
                    "id": page_id,
                    "title": fm.get("title") or page_id,
                    "type": fm.get("type") or "page",
                    "tags": fm.get("tags") or [],
                    "path": rel_path,
                }
            )

            for match in _WIKILINK_RE.finditer(body):
                target = match.group(1).strip()
                if target:
                    links.append(
                        {
                            "source": page_id,
                            "target": target,
                            "type": "wikilink",
                        }
                    )

    return {"pages": pages, "links": links}


def page(rel_path: str, wiki_path: str | None = None) -> dict[str, Any] | None:
    """Read a single wiki page by relative path.

    Returns ``None`` if the file does not exist, is outside the wiki root,
    or cannot be read.
    """
    root = Path(wiki_path or os.path.expanduser("~/wiki")).expanduser().resolve()
    target = (root / rel_path).resolve()
    # Path-traversal guard: resolved target must still be inside root.
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = _parse_frontmatter(text)
    return {
        "id": target.stem,
        "frontmatter": fm,
        "body": body,
        "path": rel_path,
    }
