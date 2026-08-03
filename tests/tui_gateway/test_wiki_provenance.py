"""Tests for wiki provenance — the event→changeset→page edge.

Covers the three things that would fail silently: provenance normalization
(what "unknown" means and what it doesn't), the read-time migration that lets
an existing KB adopt this with no rewrite pass, and the wiki.events join.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import wiki_changeset  # noqa: E402

from tui_gateway import wiki_api  # noqa: E402


@pytest.fixture
def git_wiki(tmp_path, monkeypatch):
    """A git-initialized scratch wiki with raw/ present, WIKI_PATH pointed at it."""
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "raw").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=wiki, check=True)
    monkeypatch.setenv("WIKI_PATH", str(wiki))
    return wiki


def _write(wiki: Path, rel: str, text: str) -> None:
    target = wiki / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class TestNormalizeProvenance:
    def test_no_inputs_is_empty_not_a_guess(self):
        # Empty means unrecorded. The function never invents a source, because
        # "nothing caused this" and "nobody wrote down what caused this" are
        # indistinguishable here and guessing would make the log untrustworthy.
        assert wiki_changeset.normalize_provenance() == []
        assert wiki_changeset.normalize_provenance("", []) == []
        assert wiki_changeset.normalize_provenance("   ", ["  "]) == []

    def test_legacy_single_source_becomes_the_first_key(self):
        assert wiki_changeset.normalize_provenance("raw/a.md") == ["raw/a.md"]

    def test_multiple_events_keep_wire_order(self):
        # The whole reason for a list: a synthesis draws on several sources,
        # which the single `source` string could never express.
        keys = wiki_changeset.normalize_provenance(
            "raw/first.md", ["raw/second.md", "raw/third.md"]
        )
        assert keys == ["raw/first.md", "raw/second.md", "raw/third.md"]

    def test_duplicates_collapse(self):
        keys = wiki_changeset.normalize_provenance("raw/a.md", ["raw/a.md", "raw/b.md"])
        assert keys == ["raw/a.md", "raw/b.md"]

    def test_a_bare_string_where_a_list_was_expected_still_records(self):
        # The shape a shell or JSON-lite caller most easily produces. Accepting
        # it beats silently recording no provenance at all.
        assert wiki_changeset.normalize_provenance("", "raw/a.md") == ["raw/a.md"]

    def test_non_string_entries_are_skipped_not_stringified(self):
        keys = wiki_changeset.normalize_provenance("", ["raw/a.md", None, 7, {}])
        assert keys == ["raw/a.md"]


class TestCaptureRecordsProvenance:
    def test_capture_stores_declared_events(self, git_wiki):
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        cs = wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "synthesized",
            trigger="ingest",
            source_events=["raw/one.md", "raw/two.md"],
        )
        assert cs["source_event_keys"] == ["raw/one.md", "raw/two.md"]
        assert cs["trigger"] == "ingest"

    def test_capture_without_provenance_is_empty_not_absent(self, git_wiki):
        # Always present, so a reader never distinguishes "field missing
        # because old" from "field missing because unrecorded".
        _write(git_wiki, "entities/y.md", "---\ntitle: Y\n---\nBody.\n")
        cs = wiki_changeset.wiki_capture_changeset("entities/y.md", "create", "no source")
        assert cs["source_event_keys"] == []

    def test_query_round_trips_provenance(self, git_wiki):
        _write(git_wiki, "entities/z.md", "---\ntitle: Z\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/z.md", "create", "from a source", source_events=["raw/src.md"]
        )
        result = wiki_changeset.wiki_query_changesets()
        assert result["changesets"][0]["source_event_keys"] == ["raw/src.md"]


class TestReadTimeMigration:
    """A KB adopting this needs a newer gateway, not a migration script."""

    def test_a_pre_provenance_changeset_gains_keys_from_its_legacy_source(self, git_wiki):
        _write(git_wiki, "entities/old.md", "---\ntitle: Old\n---\nBody.\n")
        cs = wiki_changeset.wiki_capture_changeset(
            "entities/old.md", "create", "legacy", source="raw/legacy.md"
        )
        # Rewrite the stored file into the old shape — no source_event_keys.
        import json
        cs_file = git_wiki / "changesets" / f"{cs['id']}.json"
        stored = json.loads(cs_file.read_text(encoding="utf-8"))
        del stored["source_event_keys"]
        cs_file.write_text(json.dumps(stored), encoding="utf-8")

        result = wiki_changeset.wiki_query_changesets()
        assert result["changesets"][0]["source_event_keys"] == ["raw/legacy.md"]

    def test_a_changeset_with_neither_field_reads_as_unknown(self):
        assert wiki_changeset._with_provenance({"id": "x"})["source_event_keys"] == []

    def test_migration_never_overwrites_recorded_provenance(self):
        recorded = {"id": "x", "source": "raw/a.md", "source_event_keys": ["raw/b.md"]}
        assert wiki_changeset._with_provenance(recorded)["source_event_keys"] == ["raw/b.md"]


class TestWikiEvents:
    def test_events_join_the_changesets_they_caused(self, git_wiki):
        _write(
            git_wiki, "raw/article.md",
            "---\ntitle: Release notes\ntype: ingest\n"
            "source_url: https://example.invalid/x\n"
            "ingested: 2026-07-01T10:00:00Z\nsha256: abc123\n---\nRaw text.\n",
        )
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "from the article",
            trigger="ingest", source_events=["raw/article.md"],
        )

        result = wiki_api.wiki_events(wiki_path=str(git_wiki))
        assert result["total"] == 1
        event = result["events"][0]
        assert event["key"] == "raw/article.md"
        assert event["kind"] == "ingest"
        assert event["source_url"] == "https://example.invalid/x"
        assert event["sha256"] == "abc123"
        # The edge the client navigates: event → the changesets it caused.
        assert [c["page"] for c in event["changesets"]] == ["entities/x.md"]

    def test_an_event_that_caused_nothing_still_appears(self, git_wiki):
        # An ingested source nobody synthesized from is exactly the gap worth
        # seeing in the feed, so it must not be filtered out.
        _write(
            git_wiki, "raw/unused.md",
            "---\ntitle: Unused\ntype: ingest\ningested: 2026-07-02T10:00:00Z\n---\nRaw.\n",
        )
        result = wiki_api.wiki_events(wiki_path=str(git_wiki))
        assert result["events"][0]["changesets"] == []

    def test_events_are_newest_first_and_undated_ones_land_last(self, git_wiki):
        for name, ingested in [
            ("old", "2026-07-01T10:00:00Z"),
            ("new", "2026-07-03T10:00:00Z"),
            ("undated", ""),
        ]:
            stamp = f"ingested: {ingested}\n" if ingested else ""
            _write(git_wiki, f"raw/{name}.md", f"---\ntitle: {name}\n{stamp}---\nRaw.\n")

        events = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"]
        # An undated event must never be silently dated to now and jump the
        # feed; it sorts last.
        assert [e["title"] for e in events] == ["new", "old", "undated"]

    def test_kind_filter_matches_the_declared_kind(self, git_wiki):
        _write(git_wiki, "raw/a.md", "---\ntitle: A\nevent_kind: github_pr\n---\nRaw.\n")
        _write(git_wiki, "raw/b.md", "---\ntitle: B\ntype: ingest\n---\nRaw.\n")

        prs = wiki_api.wiki_events(wiki_path=str(git_wiki), kind="github_pr")
        assert [e["title"] for e in prs["events"]] == ["A"]
        # event_kind wins over type, so a raw page can be a wiki page of one
        # type and an event of another.
        assert prs["events"][0]["kind"] == "github_pr"

    def test_a_wiki_without_raw_has_an_empty_log_not_an_error(self, tmp_path):
        bare = tmp_path / "bare"
        (bare / "entities").mkdir(parents=True)
        result = wiki_api.wiki_events(wiki_path=str(bare))
        assert result == {"events": [], "total": 0, "limit": 200, "offset": 0}

    def test_pagination_reports_the_full_total(self, git_wiki):
        for i in range(5):
            _write(
                git_wiki, f"raw/s{i}.md",
                f"---\ntitle: S{i}\ningested: 2026-07-0{i + 1}T10:00:00Z\n---\nRaw.\n",
            )
        result = wiki_api.wiki_events(wiki_path=str(git_wiki), limit=2, offset=1)
        assert result["total"] == 5
        assert len(result["events"]) == 2
        assert [e["title"] for e in result["events"]] == ["S3", "S2"]


class TestScanForwardsSources:
    def test_page_level_sources_reach_the_client(self, git_wiki):
        # Parsed as a list key and written by the ingest skill, but previously
        # dropped from the payload — so page-level provenance was unreadable.
        _write(
            git_wiki, "entities/x.md",
            "---\ntitle: X\nsources:\n  - raw/a.md\n  - raw/b.md\n---\nBody.\n",
        )
        pages = wiki_api.wiki_scan(wiki_path=str(git_wiki))["pages"]
        page = next(p for p in pages if p["id"] == "x")
        assert page["sources"] == ["raw/a.md", "raw/b.md"]

    def test_a_page_without_sources_reports_an_empty_list(self, git_wiki):
        _write(git_wiki, "entities/y.md", "---\ntitle: Y\n---\nBody.\n")
        pages = wiki_api.wiki_scan(wiki_path=str(git_wiki))["pages"]
        page = next(p for p in pages if p["id"] == "y")
        assert page["sources"] == []


class TestUpdateThreadsProvenance:
    def test_update_records_the_trigger_it_was_given(self, git_wiki):
        # The bug: trigger was hardcoded "manual" here, so an automated ingest
        # and a hand edit in the desktop app were indistinguishable.
        wiki_api.wiki_update(
            "entities/x.md", "Body.\n",
            frontmatter={"title": "X"},
            trigger="ingest",
            source_events=["raw/src.md"],
            summary="ingested the release notes",
            wiki_path=str(git_wiki),
        )
        cs = wiki_changeset.wiki_query_changesets(wiki_path=str(git_wiki))["changesets"][0]
        assert cs["trigger"] == "ingest"
        assert cs["source_event_keys"] == ["raw/src.md"]
        assert cs["summary"] == "ingested the release notes"

    def test_update_defaults_to_manual_with_unknown_provenance(self, git_wiki):
        wiki_api.wiki_update(
            "entities/y.md", "Body.\n",
            frontmatter={"title": "Y"},
            wiki_path=str(git_wiki),
        )
        cs = wiki_changeset.wiki_query_changesets(wiki_path=str(git_wiki))["changesets"][0]
        assert cs["trigger"] == "manual"
        # A hand edit in the app genuinely has no ingestion event, and the
        # honest record of that is an empty list, not a fabricated source.
        assert cs["source_event_keys"] == []
