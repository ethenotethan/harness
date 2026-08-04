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

    def test_events_are_newest_first(self, git_wiki):
        for name, ingested in [
            ("old", "2026-07-01T10:00:00Z"),
            ("new", "2026-07-03T10:00:00Z"),
        ]:
            _write(
                git_wiki, f"raw/{name}.md",
                f"---\ntitle: {name}\ningested: {ingested}\n---\nRaw.\n",
            )

        events = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"]
        assert [e["title"] for e in events] == ["new", "old"]

    def test_a_source_with_no_ingested_falls_back_to_mtime_and_says_so(self, git_wiki):
        # An event with no time can't be plotted at all, so the file's own mtime
        # is better than nothing — but it is NOT the event's time (a fresh clone
        # rewrites every mtime), so it must be flagged rather than presented as
        # precise. This is what the client draws as an estimated-time mark.
        _write(git_wiki, "raw/nostamp.md", "---\ntitle: No stamp\n---\nRaw.\n")

        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] != ""
        assert event["time_estimated"] is True

    def test_a_dated_source_is_not_marked_estimated(self, git_wiki):
        _write(
            git_wiki, "raw/dated.md",
            "---\ntitle: Dated\ningested: 2026-07-01T10:00:00Z\n---\nRaw.\n",
        )
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] == "2026-07-01T10:00:00Z"
        assert event["time_estimated"] is False

    @pytest.mark.parametrize(
        "written",
        [
            "2026-07-20T12:00:00Z",         # strict RFC3339
            "2026-07-20T12:00:00+00:00",    # explicit UTC offset
            "2026-07-20T12:00:00",          # bare datetime.isoformat()
            "2026-07-20T12:00:00.123456",   # ...with microseconds
            "2026-07-20 12:00:00",          # space separator
            "  2026-07-20T12:00:00Z  ",     # padded
        ],
    )
    def test_ingested_is_normalized_to_one_wire_format(self, git_wiki, written):
        # `ingested` is written by whatever ingested the source, sometimes by
        # hand, so it is not reliably strict RFC3339. Every one of these denotes
        # the same instant and must reach the client as the same string — a
        # client that has to guess the format will get it wrong, which is how
        # dated events ended up unplotted.
        _write(
            git_wiki, "raw/a.md",
            f"---\ntitle: A\ningested: {written}\n---\nRaw.\n",
        )
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] == "2026-07-20T12:00:00Z"
        assert event["time_estimated"] is False

    def test_a_non_utc_offset_is_converted_not_truncated(self, git_wiki):
        # 05:00-07:00 is 12:00Z. Dropping the offset would misplace the event by
        # seven hours and could push it out of the requested window.
        _write(
            git_wiki, "raw/a.md",
            "---\ntitle: A\ningested: 2026-07-20T05:00:00-07:00\n---\nRaw.\n",
        )
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] == "2026-07-20T12:00:00Z"

    def test_an_unparseable_ingested_falls_back_to_mtime_and_is_flagged(self, git_wiki):
        # Same treatment as a missing field: an unusable value tells us nothing
        # about when the event happened, so the mtime stands in and is marked
        # estimated. What must NOT happen is passing "whenever" through as a
        # timestamp, or inventing a precise time nobody recorded.
        _write(git_wiki, "raw/a.md", "---\ntitle: A\ningested: whenever\n---\nRaw.\n")
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] != "whenever"
        assert event["time_estimated"] is True
        # And it's a real, parseable instant rather than a passed-through string.
        assert wiki_api._parse_event_time(event["timestamp"]) is not None

    @pytest.mark.parametrize(
        "written",
        [
            "2026-07-20T12:00:00Z",
            "2026-07-20 12:00:00",      # a space sorts BELOW "T" lexically
            "2026-07-20",               # sorts below every stamp on its own day
            "2026-07-20T05:00:00-07:00",  # doesn't sort by real time at all
        ],
    )
    def test_the_window_keeps_events_inside_it_whatever_the_format(
        self, git_wiki, written
    ):
        # The bug this pins: bounds were compared as STRINGS, so an event that
        # was genuinely inside the window got dropped whenever its timestamp was
        # written in a shape that sorts oddly against the bound. The plot then
        # came up empty while the event sat right there in the feed.
        _write(
            git_wiki, "raw/a.md",
            f"---\ntitle: A\ningested: {written}\n---\nRaw.\n",
        )
        result = wiki_api.wiki_events(
            wiki_path=str(git_wiki),
            since="2026-07-20T00:00:00Z",
            until="2026-07-21T00:00:00Z",
        )
        assert [e["title"] for e in result["events"]] == ["A"]

    def test_the_window_still_excludes_events_outside_it(self, git_wiki):
        for name, ingested in [
            ("before", "2019-01-01T00:00:00Z"),
            ("inside", "2026-07-20T12:00:00Z"),
            ("after", "2031-01-01T00:00:00Z"),
        ]:
            _write(
                git_wiki, f"raw/{name}.md",
                f"---\ntitle: {name}\ningested: {ingested}\n---\nRaw.\n",
            )
        result = wiki_api.wiki_events(
            wiki_path=str(git_wiki),
            since="2026-07-20T00:00:00Z",
            until="2026-07-21T00:00:00Z",
        )
        assert [e["title"] for e in result["events"]] == ["inside"]

    def test_a_non_utc_bound_is_compared_as_an_instant(self, git_wiki):
        # until = 05:00-07:00 = 12:00Z, so a 13:00Z event is after it. Compared
        # as strings, "2026-07-20T13:00:00Z" > "2026-07-20T05:00:00-07:00" only
        # by luck of the digits — the point is that the instant decides.
        _write(
            git_wiki, "raw/late.md",
            "---\ntitle: late\ningested: 2026-07-20T13:00:00Z\n---\nRaw.\n",
        )
        _write(
            git_wiki, "raw/early.md",
            "---\ntitle: early\ningested: 2026-07-20T11:00:00Z\n---\nRaw.\n",
        )
        result = wiki_api.wiki_events(
            wiki_path=str(git_wiki), until="2026-07-20T05:00:00-07:00"
        )
        assert [e["title"] for e in result["events"]] == ["early"]

    def test_an_unparseable_bound_widens_rather_than_empties(self, git_wiki):
        # A malformed bound that silently matched nothing would look exactly
        # like an empty wiki. Treat it as absent instead.
        _write(
            git_wiki, "raw/a.md",
            "---\ntitle: A\ningested: 2026-07-20T12:00:00Z\n---\nRaw.\n",
        )
        result = wiki_api.wiki_events(wiki_path=str(git_wiki), since="garbage")
        assert [e["title"] for e in result["events"]] == ["A"]

    def test_an_estimated_time_still_participates_in_the_window(self, git_wiki):
        # A source with no usable `ingested` gets its mtime, and that estimate is
        # a real time, so the window applies to it like any other. The estimate
        # is the best available answer to "when"; excluding such events from
        # every window instead would make them appear in windows they have no
        # evidence of belonging to.
        _write(git_wiki, "raw/a.md", "---\ntitle: A\ningested: nonsense\n---\nRaw.\n")
        # The file was written just now, so a window over last year excludes it…
        past = wiki_api.wiki_events(
            wiki_path=str(git_wiki),
            since="2019-01-01T00:00:00Z",
            until="2019-12-31T00:00:00Z",
        )
        assert past["events"] == []
        # …and an unbounded query still finds it, flagged as estimated.
        allof = wiki_api.wiki_events(wiki_path=str(git_wiki))
        assert [e["title"] for e in allof["events"]] == ["A"]
        assert allof["events"][0]["time_estimated"] is True

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
