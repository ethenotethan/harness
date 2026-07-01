"""Regression tests for the news-feed producer path.

Guards against the failure diagnosed in the HermesNative feed: the reader RPCs
(``feed.get`` / ``feed.sources``) shipped without any code calling
``append_digest``, so the feed was permanently empty. These tests assert the
producer -> reader round-trip end to end, including the ``feed_publish`` tool
and dedup, so a future squash/rebase can't silently drop the writer again.
"""

import json

import pytest

from tui_gateway import digest_store


@pytest.fixture
def feed_file(tmp_path, monkeypatch):
    """Point the digest store at an isolated feed.json for each test.

    FEED_DIR / FEED_FILE are computed at import time, so patch the module
    globals directly rather than relying on HERMES_HOME import ordering.
    """
    d = tmp_path / "digests"
    monkeypatch.setattr(digest_store, "FEED_DIR", d)
    monkeypatch.setattr(digest_store, "FEED_FILE", d / "feed.json")
    return d / "feed.json"


class TestProducerReaderRoundTrip:
    def test_append_then_get_feed(self, feed_file):
        # The bug: readers existed but nothing populated the store.
        assert digest_store.get_feed()["articles"] == []

        digest_store.append_digest(
            "ai-digest",
            [
                {"title": "Model X released", "url": "https://ex/1", "summary": "s1"},
                {"title": "Chip Y benchmarks", "url": "https://ex/2", "summary": "s2"},
            ],
        )

        feed = digest_store.get_feed()
        assert feed["total"] == 2
        titles = {a["title"] for a in feed["articles"]}
        assert titles == {"Model X released", "Chip Y benchmarks"}
        # Producer-supplied fields survive the round-trip.
        first = feed["articles"][0]
        assert first["source"] == "ai-digest"
        assert first["url"] in {"https://ex/1", "https://ex/2"}
        assert "id" in first and "ts" in first

    def test_append_populates_sources(self, feed_file):
        digest_store.append_digest("ai-digest", [{"title": "a"}])
        digest_store.append_digest("markets", [{"title": "b"}, {"title": "c"}])

        sources = digest_store.get_sources()
        assert sources["total"] == 3
        assert sources["sources"] == {"ai-digest": 1, "markets": 2}

    def test_dedup_same_source(self, feed_file):
        digest_store.append_digest("ai-digest", [{"title": "dupe", "url": "u"}])
        digest_store.append_digest("ai-digest", [{"title": "dupe", "url": "u"}])
        # Re-publishing the same item must not create a second entry.
        assert digest_store.get_feed()["total"] == 1

    def test_source_filter_and_pagination(self, feed_file):
        digest_store.append_digest("ai-digest", [{"title": f"a{i}"} for i in range(3)])
        digest_store.append_digest("markets", [{"title": "m0"}])

        only_ai = digest_store.get_feed(sources=["ai-digest"])
        assert only_ai["total"] == 3
        assert all(a["source"] == "ai-digest" for a in only_ai["articles"])

        page = digest_store.get_feed(limit=2, offset=0)
        assert len(page["articles"]) == 2
        assert page["has_more"] is True


class TestFeedPublishTool:
    def test_tool_publishes_and_reports(self, feed_file):
        from tools.feed_tool import feed_publish

        out = json.loads(
            feed_publish("ai-digest", [{"title": "t1", "url": "u1"}])
        )
        assert out["source"] == "ai-digest"
        assert out["published"] == 1
        assert out["total"] == 1
        assert digest_store.get_feed()["total"] == 1

    def test_tool_rejects_bad_input(self, feed_file):
        from tools.feed_tool import feed_publish

        assert "error" in json.loads(feed_publish("", [{"title": "x"}]))
        assert "error" in json.loads(feed_publish("src", "not-a-list"))
        # An articles list with no usable objects is an error, not a silent no-op.
        assert "error" in json.loads(feed_publish("src", []))

    def test_tool_coerces_json_string_articles(self, feed_file):
        from tools.feed_tool import feed_publish

        # Some models emit each article as a JSON string rather than an object.
        out = json.loads(
            feed_publish("ai-digest", ['{"title": "coerced", "url": "u"}'])
        )
        assert out["published"] == 1
        assert digest_store.get_feed()["articles"][0]["title"] == "coerced"


class TestFeedToolRegistered:
    def test_feed_publish_is_registered(self):
        from tools.registry import registry

        # discover_builtin_tools imports every tools/*.py at startup; ensure the
        # module self-registered under the expected name + toolset.
        from tools import feed_tool  # noqa: F401  (import side effect: register)

        tool = registry._tools.get("feed_publish")
        assert tool is not None, "feed_publish tool must be registered"
        assert tool.toolset == "feed"
