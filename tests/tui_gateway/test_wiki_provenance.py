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


class TestRecordEvent:
    """The materialized event log — events captured at write-time, not derived.

    The identity is the key; first write creates, later writes are idempotent
    fill-forward (never clobbering a richer earlier value).
    """

    def test_record_creates_an_event(self, git_wiki):
        rec = wiki_changeset.wiki_record_event(
            "raw/snapshots/2026-08-18.json",
            kind="snapshot",
            source_url="https://example.invalid/s",
            sha256="abc123",
            ingested_at="2026-08-18T06:00:00Z",
            trigger="ingest",
        )
        assert rec["key"] == "raw/snapshots/2026-08-18.json"
        assert rec["kind"] == "snapshot"
        assert rec["source_url"] == "https://example.invalid/s"
        assert rec["sha256"] == "abc123"
        assert rec["ingested_at"] == "2026-08-18T06:00:00Z"

    def test_a_blank_key_is_refused(self, git_wiki):
        assert "error" in wiki_changeset.wiki_record_event("   ")

    def test_second_write_with_same_key_is_idempotent(self, git_wiki):
        wiki_changeset.wiki_record_event("raw/mpp/a.json", kind="mpp")
        again = wiki_changeset.wiki_record_event("raw/mpp/a.json", kind="mpp")
        events = wiki_changeset._load_events()
        # One event, not two — the key is the identity.
        assert list(events.keys()) == ["raw/mpp/a.json"]
        assert again["kind"] == "mpp"

    def test_later_write_fills_blanks_but_never_clobbers(self, git_wiki):
        # A capture records only the key + trigger; a later enriching call adds
        # the url it learned. But a value already recorded is never overwritten.
        wiki_changeset.wiki_record_event("raw/x.json", trigger="ingest")
        enriched = wiki_changeset.wiki_record_event(
            "raw/x.json", source_url="https://example.invalid/x", kind="snapshot"
        )
        assert enriched["source_url"] == "https://example.invalid/x"
        assert enriched["kind"] == "snapshot"
        assert enriched["trigger"] == "ingest"
        # A conflicting later kind must not clobber the recorded one.
        again = wiki_changeset.wiki_record_event("raw/x.json", kind="OTHER")
        assert again["kind"] == "snapshot"


class TestCaptureEmitsEvents:
    """The write path emits events, so one snapshot → many pages is one event."""

    def test_capture_materializes_an_event_per_source(self, git_wiki):
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "synthesized",
            trigger="ingest", source_events=["raw/one.json", "raw/two.json"],
        )
        events = wiki_changeset._load_events()
        assert set(events.keys()) == {"raw/one.json", "raw/two.json"}
        assert events["raw/one.json"]["trigger"] == "ingest"

    def test_one_snapshot_causing_many_pages_is_one_event(self, git_wiki):
        # 3 page writes all caused by the same snapshot must record ONE event,
        # not three — the upsert dedupes by key.
        for i in range(3):
            _write(git_wiki, f"entities/p{i}.md", f"---\ntitle: P{i}\n---\nB.\n")
            wiki_changeset.wiki_capture_changeset(
                f"entities/p{i}.md", "create", "from the snapshot",
                trigger="ingest", source_events=["raw/snapshots/day.json"],
            )
        events = wiki_changeset._load_events()
        assert list(events.keys()) == ["raw/snapshots/day.json"]

    def test_capture_with_no_source_emits_no_event(self, git_wiki):
        # A hand edit with no provenance records no event — the log stays a
        # record of ingestion, not of every keystroke.
        _write(git_wiki, "entities/y.md", "---\ntitle: Y\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset("entities/y.md", "create", "no source")
        assert wiki_changeset._load_events() == {}


class TestWikiEvents:
    """wiki.events reads the MATERIALIZED log and joins the changesets caused.

    These pin the emitted-event contract (not the old raw/ derivation) while
    preserving the invariants the derivation legitimately had: window/since/
    until as instants, RFC3339 normalization, newest-first sort, pagination,
    kind filter, and the caused-nothing view.
    """

    def test_events_join_the_changesets_they_caused(self, git_wiki):
        # An ingester records the event with its domain metadata (the write
        # path can't infer a url from an opaque key)…
        wiki_changeset.wiki_record_event(
            "raw/article.json", kind="ingest",
            source_url="https://example.invalid/x", sha256="abc123",
            ingested_at="2026-07-01T10:00:00Z",
        )
        # …and a capture declares it as the cause of a page write.
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "from the article",
            trigger="ingest", source_events=["raw/article.json"],
        )

        result = wiki_api.wiki_events(wiki_path=str(git_wiki))
        assert result["total"] == 1
        event = result["events"][0]
        assert event["key"] == "raw/article.json"
        assert event["kind"] == "ingest"
        assert event["source_url"] == "https://example.invalid/x"
        assert event["sha256"] == "abc123"
        # The edge the client navigates: event → the changesets it caused.
        assert [c["page"] for c in event["changesets"]] == ["entities/x.md"]

    def test_capture_trigger_classifies_event_when_kind_was_not_enriched(self, git_wiki):
        """The capture path knows the open event-kind wire value as `trigger`.

        Most wiki writers only call capture; they do not separately enrich the
        materialized event. The read contract must therefore expose that trigger
        as the event kind instead of rendering a classified source as blank.
        """
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "from Telegram",
            trigger="telegram", source_events=["raw/telegram/item.md"],
        )

        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]

        assert event["kind"] == "telegram"
        assert event["trigger"] == "telegram"

    def test_an_event_that_caused_nothing_still_appears(self, git_wiki):
        # An ingester fetched a source but hasn't synthesized from it yet. It
        # records the event directly (wiki_record_event is public/standalone),
        # and it must show in the feed as caused-nothing — that's the work
        # queue, not an error.
        wiki_changeset.wiki_record_event(
            "raw/unused.json", kind="ingest", ingested_at="2026-07-02T10:00:00Z"
        )
        result = wiki_api.wiki_events(wiki_path=str(git_wiki))
        assert result["total"] == 1
        assert result["events"][0]["changesets"] == []

    def test_events_are_newest_first(self, git_wiki):
        for name, ingested in [
            ("old", "2026-07-01T10:00:00Z"),
            ("new", "2026-07-03T10:00:00Z"),
        ]:
            wiki_changeset.wiki_record_event(f"raw/{name}.json", ingested_at=ingested)
        events = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"]
        assert [e["key"] for e in events] == ["raw/new.json", "raw/old.json"]

    def test_an_event_with_no_ingested_at_falls_back_to_recorded_at_and_says_so(
        self, git_wiki
    ):
        # An event captured with no source ingest time still has a real instant
        # to plot — when it was first materialized — but that is NOT the source's
        # own time, so it is flagged rather than presented as precise.
        wiki_changeset.wiki_record_event("raw/nostamp.json")
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] != ""
        assert event["time_estimated"] is True

    def test_a_dated_event_is_not_marked_estimated(self, git_wiki):
        wiki_changeset.wiki_record_event(
            "raw/dated.json", ingested_at="2026-07-01T10:00:00Z"
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
    def test_ingested_at_is_normalized_to_one_wire_format(self, git_wiki, written):
        # ingested_at is supplied by whatever recorded the event, sometimes by
        # hand, so it is not reliably strict RFC3339. Every one of these denotes
        # the same instant and must reach the client as the same string.
        wiki_changeset.wiki_record_event("raw/a.json", ingested_at=written)
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] == "2026-07-20T12:00:00Z"
        assert event["time_estimated"] is False

    def test_a_non_utc_offset_is_converted_not_truncated(self, git_wiki):
        # 05:00-07:00 is 12:00Z. Dropping the offset would misplace the event.
        wiki_changeset.wiki_record_event(
            "raw/a.json", ingested_at="2026-07-20T05:00:00-07:00"
        )
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] == "2026-07-20T12:00:00Z"

    def test_an_unparseable_ingested_at_falls_back_and_is_flagged(self, git_wiki):
        # An unusable value tells us nothing about when the event happened, so
        # recorded_at stands in and is marked estimated — never passed through.
        wiki_changeset.wiki_record_event("raw/a.json", ingested_at="whenever")
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] != "whenever"
        assert event["time_estimated"] is True
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
        # Bounds are compared as INSTANTS, so an event genuinely inside the
        # window is kept whatever shape its timestamp was written in.
        wiki_changeset.wiki_record_event("raw/a.json", ingested_at=written)
        result = wiki_api.wiki_events(
            wiki_path=str(git_wiki),
            since="2026-07-20T00:00:00Z",
            until="2026-07-21T00:00:00Z",
        )
        assert [e["key"] for e in result["events"]] == ["raw/a.json"]

    def test_the_window_still_excludes_events_outside_it(self, git_wiki):
        for name, ingested in [
            ("before", "2019-01-01T00:00:00Z"),
            ("inside", "2026-07-20T12:00:00Z"),
            ("after", "2031-01-01T00:00:00Z"),
        ]:
            wiki_changeset.wiki_record_event(f"raw/{name}.json", ingested_at=ingested)
        result = wiki_api.wiki_events(
            wiki_path=str(git_wiki),
            since="2026-07-20T00:00:00Z",
            until="2026-07-21T00:00:00Z",
        )
        assert [e["key"] for e in result["events"]] == ["raw/inside.json"]

    def test_a_non_utc_bound_is_compared_as_an_instant(self, git_wiki):
        # until = 05:00-07:00 = 12:00Z, so a 13:00Z event is after it.
        wiki_changeset.wiki_record_event(
            "raw/late.json", ingested_at="2026-07-20T13:00:00Z"
        )
        wiki_changeset.wiki_record_event(
            "raw/early.json", ingested_at="2026-07-20T11:00:00Z"
        )
        result = wiki_api.wiki_events(
            wiki_path=str(git_wiki), until="2026-07-20T05:00:00-07:00"
        )
        assert [e["key"] for e in result["events"]] == ["raw/early.json"]

    def test_an_unparseable_bound_widens_rather_than_empties(self, git_wiki):
        wiki_changeset.wiki_record_event(
            "raw/a.json", ingested_at="2026-07-20T12:00:00Z"
        )
        result = wiki_api.wiki_events(wiki_path=str(git_wiki), since="garbage")
        assert [e["key"] for e in result["events"]] == ["raw/a.json"]

    def test_an_estimated_time_still_participates_in_the_window(self, git_wiki):
        # An event with no usable ingested_at gets recorded_at (now), a real
        # time the window applies to like any other.
        wiki_changeset.wiki_record_event("raw/a.json", ingested_at="nonsense")
        past = wiki_api.wiki_events(
            wiki_path=str(git_wiki),
            since="2019-01-01T00:00:00Z",
            until="2019-12-31T00:00:00Z",
        )
        assert past["events"] == []
        allof = wiki_api.wiki_events(wiki_path=str(git_wiki))
        assert [e["key"] for e in allof["events"]] == ["raw/a.json"]
        assert allof["events"][0]["time_estimated"] is True

    def test_kind_filter_matches_the_declared_kind(self, git_wiki):
        wiki_changeset.wiki_record_event("raw/a.json", kind="github_pr")
        wiki_changeset.wiki_record_event("raw/b.json", kind="ingest")
        prs = wiki_api.wiki_events(wiki_path=str(git_wiki), kind="github_pr")
        assert [e["key"] for e in prs["events"]] == ["raw/a.json"]
        assert prs["events"][0]["kind"] == "github_pr"

    def test_a_wiki_without_events_has_an_empty_log_not_an_error(self, tmp_path):
        bare = tmp_path / "bare"
        (bare / "entities").mkdir(parents=True)
        result = wiki_api.wiki_events(wiki_path=str(bare))
        assert result == {"events": [], "total": 0, "limit": 200, "offset": 0}

    def test_pagination_reports_the_full_total(self, git_wiki):
        for i in range(5):
            wiki_changeset.wiki_record_event(
                f"raw/s{i}.json", ingested_at=f"2026-07-0{i + 1}T10:00:00Z"
            )
        result = wiki_api.wiki_events(wiki_path=str(git_wiki), limit=2, offset=1)
        assert result["total"] == 5
        assert len(result["events"]) == 2
        assert [e["key"] for e in result["events"]] == ["raw/s3.json", "raw/s2.json"]


class TestBackfillEvents:
    """Backfill materializes one event per distinct source key, idempotently."""

    def test_backfill_materializes_events_for_existing_changesets(self, git_wiki):
        # Simulate a wiki whose changesets carry source keys but whose event
        # log was never populated (the pre-refactor state): capture, then wipe
        # the event store the capture emitted.
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "s", trigger="ingest",
            source_events=["raw/one.json", "raw/two.json"],
        )
        (git_wiki / "changesets" / "events.json").unlink()

        res = wiki_changeset.wiki_backfill_events(wiki_path=str(git_wiki))
        assert res["distinct_keys"] == 2
        assert res["created"] == 2
        assert set(wiki_changeset._load_events(str(git_wiki)).keys()) == {
            "raw/one.json", "raw/two.json"
        }

    def test_backfill_is_idempotent_on_rerun(self, git_wiki):
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "s", trigger="ingest",
            source_events=["raw/one.json"],
        )
        first = wiki_changeset.wiki_backfill_events(wiki_path=str(git_wiki))
        second = wiki_changeset.wiki_backfill_events(wiki_path=str(git_wiki))
        # The events already existed (emitted at capture), so re-running never
        # creates duplicates.
        assert second["created"] == 0
        assert second["already_present"] == first["distinct_keys"]
        assert list(wiki_changeset._load_events(str(git_wiki)).keys()) == ["raw/one.json"]

    def test_backfill_dates_an_event_by_its_earliest_caused_changeset(self, git_wiki):
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nBody.\n")
        wiki_changeset.wiki_capture_changeset(
            "entities/x.md", "create", "s", trigger="ingest",
            source_events=["raw/one.json"],
        )
        (git_wiki / "changesets" / "events.json").unlink()
        wiki_changeset.wiki_backfill_events(wiki_path=str(git_wiki))
        # The backfilled event carries a real timestamp (the changeset's), so
        # it's plottable rather than dumped at "now".
        event = wiki_api.wiki_events(wiki_path=str(git_wiki))["events"][0]
        assert event["timestamp"] != ""


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
