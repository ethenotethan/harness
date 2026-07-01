"""feed_publish tool — the agent-facing producer for the HermesNative news feed.

The gateway exposes ``feed.get`` / ``feed.sources`` (readers) that the native
client renders as a news feed, backed by ``tui_gateway/digest_store.py``. This
tool is the write path: it lets the agent — typically driven by the
``news-digest`` cron blueprint — push curated articles into that store so the
feed actually populates instead of always returning empty.

Articles are deduped against what was already stored for the same source, so a
recurring digest only lands genuinely new items.
"""

import json

from tools.registry import registry, tool_error


def feed_publish(source: str, articles: list) -> str:
    """Append articles to the news feed store.

    Args:
        source:   Feed source name (e.g. ``"ai-digest"``). Shown as a filter
                  tab in the client and used as the dedup key.
        articles: List of article dicts. Each may carry ``title``, ``url``,
                  ``summary``, ``tags`` (list), and ``image_url``.

    Returns:
        JSON string ``{"published": added, "total": N, "source": source}``.
    """
    if not source or not isinstance(source, str):
        return tool_error("source must be a non-empty string")
    if not isinstance(articles, list):
        return tool_error("articles must be a list of objects")
    # Tolerate a JSON string that some models emit instead of a real list.
    cleaned = []
    for a in articles:
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except (ValueError, TypeError):
                a = {"title": a}
        if isinstance(a, dict):
            cleaned.append(a)
    if not cleaned:
        return tool_error("no valid articles to publish")

    try:
        from tui_gateway.digest_store import append_digest, get_sources
    except Exception as exc:  # pragma: no cover - import shape varies by install
        return tool_error(f"feed store unavailable: {exc}")

    before = get_sources().get("sources", {}).get(source, 0)
    total = append_digest(source, cleaned)
    after = get_sources().get("sources", {}).get(source, 0)
    return json.dumps(
        {"published": max(0, after - before), "total": total, "source": source},
        ensure_ascii=False,
    )


def check_feed_requirements() -> bool:
    """The feed store is a local JSON file — always available."""
    return True


FEED_PUBLISH_SCHEMA = {
    "name": "feed_publish",
    "description": (
        "Publish curated articles to the user's news feed (the feed the "
        "HermesNative app shows). Use this to deliver a recurring topic digest "
        "as a browsable, deduped feed instead of a one-off chat message — it is "
        "the write side of the feed the client reads.\n\n"
        "Articles are deduped against what was already published for the same "
        "`source`, so re-running a digest only adds genuinely new items. Group "
        "related runs under a stable `source` name (e.g. 'ai-digest') so they "
        "share a filter tab and dedup history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "Stable feed source name, e.g. 'ai-digest'. Reused across "
                    "runs of the same digest for grouping and dedup."
                ),
            },
            "articles": {
                "type": "array",
                "description": "The articles to publish.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Headline."},
                        "url": {"type": "string", "description": "Link to the source."},
                        "summary": {
                            "type": "string",
                            "description": "One- or two-sentence summary (trimmed to 500 chars).",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional topic tags.",
                        },
                        "image_url": {
                            "type": "string",
                            "description": "Optional thumbnail image URL.",
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        "required": ["source", "articles"],
    },
}


registry.register(
    name="feed_publish",
    toolset="feed",
    schema=FEED_PUBLISH_SCHEMA,
    handler=lambda args, **kw: feed_publish(
        source=args.get("source", ""),
        articles=args.get("articles", []),
    ),
    check_fn=check_feed_requirements,
    emoji="📰",
)
