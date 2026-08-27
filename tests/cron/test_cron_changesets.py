"""Tests for the recorded cron configuration history (``cron/changesets.py``).

Three things are load-bearing here and each has its own class below:

1. **The digest is a shared commitment.** Portal computes the same digest over
   the same graph in Swift. If the two implementations disagree, each still looks
   authoritative on its own screen, which is worse than having only one. The
   fixture in ``TestPortalDigestParity`` is asserted byte-for-byte on both sides.
2. **The gate is the whole feature.** Every cron mutation funnels through one
   save function, and most saves are the scheduler's bookkeeping. A log that
   recorded them all would be a log of the tick loop.
3. **Attribution must not lie.** An unrecorded actor is honest; the wrong actor
   is not.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    return hermes_home


def _rows():
    from cron.changesets import _read_rows

    return _read_rows()


# =============================================================================
# The shared commitment
# =============================================================================

# One graph exercising every branch of the canonical form: a fully-populated cron
# node carrying runtime fields that must NOT count, a node with only the required
# keys (so every decode default applies), a service node with a description and
# no schedule, a node whose schedule is the empty string rather than absent, a
# multi-byte label, and an edge with no type.
PARITY_FIXTURE = {
    "nodes": [
        {
            "id": "indexing/sweep",
            "kind": "cron",
            "type": "cron",
            "label": "indexing/sweep",
            "schedule": "every 6h",
            "enabled": True,
            "uses_llm": True,
            "last_status": "ok",
            "deliver": "telegram",
            "state": "scheduled",
        },
        {"id": "wiki:x402", "kind": "artifact", "type": "wiki", "label": "x402"},
        {
            "id": "svc/dash",
            "kind": "service",
            "type": "service",
            "label": "dashboard ✻",
            "description": "graphs",
        },
        {
            "id": "https://exämple.com/feed",
            "kind": "source",
            "type": "https",
            "label": "//exämple.com/feed",
            "schedule": "",
        },
    ],
    "edges": [
        {"source": "indexing/sweep", "target": "wiki:x402", "type": "writes"},
        {"source": "https://exämple.com/feed", "target": "indexing/sweep"},
        {"source": "indexing/sweep", "target": "telegram:me", "type": "telegram"},
    ],
}

# The same fixture and the same constant are asserted in Portal's
# CronGraphDigestTests (`Tests/PortalTests/CronGraphDigestTests.swift`,
# `testGatewayFixtureDigestMatchesTheHarness`). Neither number may be edited
# alone: if a change to the canonical form is right, it is right in both
# languages, and this pair of tests is the only thing that says so out loud.
PARITY_DIGEST = "b020f2041953355870b525d6ca3d8ab834feaa41869d1b629d75b0abf1b06111"


class TestPortalDigestParity:
    def test_fixture_digest_matches_portal(self):
        from cron.changesets import configuration_digest

        assert configuration_digest(PARITY_FIXTURE) == PARITY_DIGEST

    def test_runtime_state_is_not_part_of_the_commitment(self):
        """The graph is re-read every 10 seconds for liveness.

        A commitment covering ``last_status`` / ``state`` / health would mint a
        revision per poll — a history of the poll loop rather than of anyone's
        changes.
        """
        from cron.changesets import configuration_digest

        moved = json.loads(json.dumps(PARITY_FIXTURE))
        moved["nodes"][0]["last_status"] = "error"
        moved["nodes"][0]["state"] = "error"
        moved["nodes"][2]["health"] = {"status": "up", "latency_ms": 41}

        assert configuration_digest(moved) == PARITY_DIGEST

    def test_gateway_ordering_cannot_move_the_digest(self):
        from cron.changesets import configuration_digest

        shuffled = {
            "nodes": list(reversed(PARITY_FIXTURE["nodes"])),
            "edges": list(reversed(PARITY_FIXTURE["edges"])),
        }
        assert configuration_digest(shuffled) == PARITY_DIGEST

    def test_absent_and_empty_schedule_are_different_configurations(self):
        """A job with no schedule and one whose schedule was cleared differ.

        Collapsing them would hide the edit between them.
        """
        from cron.changesets import configuration_digest

        absent = {"nodes": [{"id": "j", "kind": "cron"}], "edges": []}
        empty = {"nodes": [{"id": "j", "kind": "cron", "schedule": ""}], "edges": []}

        assert configuration_digest(absent) != configuration_digest(empty)

    def test_a_forged_field_boundary_cannot_collide(self):
        """Two graphs, one address, is the one failure a content address must not have.

        Joined on ``:`` — the separator already inside every node id — these two
        encode identically. Length-prefixed, they cannot.
        """
        from cron.changesets import configuration_digest

        left = {
            "nodes": [{"id": "wiki:a", "kind": "artifact", "type": "t", "label": "l"}],
            "edges": [],
        }
        right = {
            "nodes": [{"id": "wiki", "kind": "a:artifact", "type": "t", "label": "l"}],
            "edges": [],
        }
        assert configuration_digest(left) != configuration_digest(right)

    def test_client_decode_defaults_are_the_ones_hashed(self):
        """The digest is taken over the graph *as the client reads it*.

        ``type`` falls back to ``kind``, ``label`` to ``id``, ``enabled`` to
        true, ``uses_llm`` to false — Portal's decode applies exactly these, so
        hashing anything else would make the same bytes produce two digests.
        """
        from cron.changesets import configuration_digest

        sparse = {"nodes": [{"id": "j", "kind": "cron"}], "edges": []}
        spelled_out = {
            "nodes": [
                {
                    "id": "j",
                    "kind": "cron",
                    "type": "cron",
                    "label": "j",
                    "description": "",
                    "enabled": True,
                    "uses_llm": False,
                }
            ],
            "edges": [],
        }
        assert configuration_digest(sparse) == configuration_digest(spelled_out)

    def test_an_untyped_edge_reads(self):
        from cron.changesets import configuration_digest

        untyped = {"nodes": [], "edges": [{"source": "a", "target": "b"}]}
        spelled = {
            "nodes": [],
            "edges": [{"source": "a", "target": "b", "type": "reads"}],
        }
        assert configuration_digest(untyped) == configuration_digest(spelled)

    def test_rows_sort_by_utf8_bytes(self):
        """Not by the language's native string order.

        Python compares code points and Swift compares canonically-equivalent
        graphemes; on a non-ASCII label the two orders can differ, and a
        content address that depends on which language computed it is not one.
        """
        from cron.changesets import configuration_form

        graph = {
            "nodes": [
                {"id": "zebra", "kind": "cron"},
                {"id": "äpple", "kind": "cron"},
                {"id": "apple", "kind": "cron"},
            ],
            "edges": [],
        }
        rows = configuration_form(graph)
        as_bytes = [row.encode("utf-8") for row in rows]
        assert as_bytes == sorted(as_bytes)


# =============================================================================
# The gate
# =============================================================================

class TestRecordingGate:
    def test_first_row_on_a_populated_store_is_a_baseline(self, cron_env):
        """The configuration before recording began is genuinely unknown.

        So the log opens by stating what exists, unattributed, instead of
        crediting it to whoever happened to trigger the first write.
        """
        from cron.jobs import create_job

        create_job(prompt="collect", schedule="every 1h")

        rows = _rows()
        assert len(rows) == 1
        assert rows[0]["action"] == "baseline"
        assert rows[0]["parent_digest"] == ""
        assert rows[0]["actor"] == ""
        assert "baseline" in rows[0]["summary"]

    def test_runtime_only_saves_record_nothing(self, cron_env):
        """The failure this whole design exists to prevent.

        ``last_run_at`` / ``next_run_at`` / ``last_status`` move on every tick.
        Recording per save would produce a log of the scheduler's loop.
        """
        from cron.jobs import create_job, load_jobs, save_jobs

        create_job(prompt="collect", schedule="every 1h")
        before = len(_rows())

        for stamp in ("2026-08-26T10:00:00+00:00", "2026-08-26T11:00:00+00:00"):
            jobs = load_jobs()
            jobs[0]["last_run_at"] = stamp
            jobs[0]["next_run_at"] = stamp
            jobs[0]["last_status"] = "success"
            save_jobs(jobs)

        assert len(_rows()) == before

    def test_a_configuration_edit_records_exactly_one_row(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        baseline = _rows()[-1]

        update_job(job["id"], {"schedule": "every 6h"})

        rows = _rows()
        assert len(rows) == 2
        assert rows[1]["action"] == "update"
        assert rows[1]["job"] == job["id"]
        assert rows[1]["parent_digest"] == baseline["digest"]
        assert rows[1]["digest"] != baseline["digest"]

    def test_a_second_job_records_a_create_naming_it(self, cron_env):
        from cron.jobs import create_job

        create_job(prompt="collect", schedule="every 1h")
        second = create_job(prompt="report", schedule="every 6h")

        row = _rows()[-1]
        assert row["action"] == "create"
        assert row["job"] == second["id"]
        assert row["summary"].startswith("created ")

    def test_a_removal_records_a_delete(self, cron_env):
        from cron.jobs import create_job, remove_job

        create_job(prompt="collect", schedule="every 1h")
        doomed = create_job(prompt="report", schedule="every 6h")
        remove_job(doomed["id"])

        row = _rows()[-1]
        assert row["action"] == "delete"
        assert row["job"] == doomed["id"]

    def test_a_dataflow_only_edit_still_names_the_job(self, cron_env):
        """A changed ``inputs`` list leaves the job's own node row identical.

        The change lives entirely in its edges, so "which job changed" has to
        look at both — otherwise a rewiring reads as no change at all.
        """
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        update_job(job["id"], {"inputs": ["wiki:notes"]})

        row = _rows()[-1]
        assert row["action"] == "update"
        assert row["job"] == job["id"]

    def test_a_stale_head_cache_cannot_record_a_duplicate(self, cron_env):
        """The head file is a cache and never the authority.

        Losing it must cost one full read of the log, not a second row claiming
        a change that didn't happen.
        """
        from cron.changesets import changeset_head_path
        from cron.jobs import create_job, load_jobs, save_jobs

        create_job(prompt="collect", schedule="every 1h")
        before = len(_rows())
        changeset_head_path().unlink()

        save_jobs(load_jobs())

        assert len(_rows()) == before

    def test_the_log_is_trimmed_to_the_cap(self, cron_env, monkeypatch):
        import cron.changesets as changesets_mod
        from cron.jobs import create_job, update_job

        monkeypatch.setattr(changesets_mod, "MAX_CHANGESETS", 3)
        job = create_job(prompt="collect", schedule="every 1h")
        for hours in (2, 3, 4, 5, 6):
            update_job(job["id"], {"schedule": f"every {hours}h"})

        rows = _rows()
        assert len(rows) == 3
        # Trimming keeps the newest, and the oldest survivor's parent snapshot is
        # gone — which is exactly the case cron.changeset_diff must not paper
        # over (see TestReading.test_a_trimmed_parent_has_no_before).
        assert rows[-1]["summary"].endswith("collect")


# =============================================================================
# Attribution
# =============================================================================

class TestAttribution:
    def test_an_unattributed_change_records_no_actor(self, cron_env):
        """Empty is an admission of ignorance, not a claim that nobody did it."""
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        update_job(job["id"], {"schedule": "every 6h"})

        assert _rows()[-1]["actor"] == ""

    def test_a_bound_origin_records_actor_and_provenance(self, cron_env):
        from cron.changesets import use_changeset_origin
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        with use_changeset_origin("human", source_event_keys=["session/abc/turn-7"]):
            update_job(job["id"], {"schedule": "every 6h"})

        row = _rows()[-1]
        assert row["actor"] == "human"
        assert row["source_event_keys"] == ["session/abc/turn-7"]

    def test_blank_provenance_keys_are_dropped(self, cron_env):
        """A caller that recorded "no turns" and one that recorded nothing are
        indistinguishable, so neither may arrive as a list of blanks."""
        from cron.changesets import current_origin, use_changeset_origin

        with use_changeset_origin(
            "agent", source_event_keys=["  ", "", " session/abc/turn-7 "]
        ):
            assert current_origin().source_event_keys == ("session/abc/turn-7",)

    def test_the_session_fills_provenance_the_caller_could_not(self, cron_env, monkeypatch):
        """A boundary that knows the actor but not the turn still gets one.

        The ambient session context is where every other tool reads its routing
        from, so cron doesn't ask its callers to thread a turn id it can already
        see. With no session either, provenance stays empty — which is an
        admission of ignorance and not a claim that no cause exists.
        """
        import cron.changesets as changesets_mod
        from cron.changesets import current_origin, use_changeset_origin
        from cron.jobs import create_job, update_job

        monkeypatch.setattr(
            changesets_mod, "_session_turn_keys", lambda: ("session/live/turn-3",)
        )
        with use_changeset_origin("human"):
            assert current_origin().source_event_keys == ("session/live/turn-3",)

        monkeypatch.setattr(changesets_mod, "_session_turn_keys", lambda: ())
        job = create_job(prompt="collect", schedule="every 1h")
        with use_changeset_origin("human"):
            update_job(job["id"], {"schedule": "every 6h"})

        assert _rows()[-1]["source_event_keys"] == []

    def test_an_outer_origin_wins_over_a_deferential_inner_one(self, cron_env):
        """``cron.manage`` binds the human around the tool the model also calls.

        The tool's own claim defers (``if_unset=True``) precisely so a person
        clicking in a UI isn't recorded as an agent.
        """
        from cron.changesets import current_origin, use_changeset_origin

        with use_changeset_origin("human"):
            with use_changeset_origin("agent", if_unset=True):
                assert current_origin().actor == "human"

        with use_changeset_origin("agent", if_unset=True):
            assert current_origin().actor == "agent"

    def test_a_scheduler_note_reaches_the_summary(self, cron_env):
        """"The scheduler disabled this job" is what a history is opened to settle."""
        from cron.changesets import use_changeset_origin
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        with use_changeset_origin("scheduler", note="repeat limit reached"):
            update_job(job["id"], {"enabled": False})

        row = _rows()[-1]
        assert row["actor"] == "scheduler"
        assert row["summary"].endswith("repeat limit reached")

    def test_the_cli_records_a_person(self, cron_env):
        """The interactive ``/cron`` path binds the human before delegating."""
        from cron.changesets import current_origin, use_changeset_origin

        # What hermes_cli.cli_commands_mixin._cron_api does around the tool call.
        with use_changeset_origin("human"):
            assert current_origin().actor == "human"


# =============================================================================
# Reading
# =============================================================================

class TestReading:
    def test_page_envelope_is_newest_first_and_carries_no_snapshots(self, cron_env):
        from cron.changesets import read_changesets
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        update_job(job["id"], {"schedule": "every 6h"})

        page = read_changesets(limit=10)
        assert page["total"] == 2
        assert page["limit"] == 10
        assert page["offset"] == 0
        assert [row["action"] for row in page["changesets"]] == ["update", "baseline"]
        # The snapshots are the largest part of a row; a page of 50 would be
        # megabytes of graphs nobody asked for.
        assert all("graph" not in row for row in page["changesets"])

    def test_total_counts_matches_not_the_page(self, cron_env):
        from cron.changesets import read_changesets
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        for hours in (2, 3, 4):
            update_job(job["id"], {"schedule": f"every {hours}h"})

        page = read_changesets(limit=2, offset=1)
        assert page["total"] == 4
        assert len(page["changesets"]) == 2
        assert page["offset"] == 1

    def test_the_job_filter_restricts_to_one_history(self, cron_env):
        from cron.changesets import read_changesets
        from cron.jobs import create_job, update_job

        first = create_job(prompt="collect", schedule="every 1h")
        second = create_job(prompt="report", schedule="every 6h")
        update_job(first["id"], {"schedule": "every 2h"})

        page = read_changesets(job=second["id"])
        assert [row["job"] for row in page["changesets"]] == [second["id"]]
        assert page["total"] == 1

    def test_an_unjudgeable_timestamp_is_kept(self, cron_env):
        """A filter that silently drops what it can't read turns an unparseable
        timestamp into a missing change."""
        from cron.changesets import _in_window

        row = {"timestamp": "who knows"}
        assert _in_window(row, "2026-01-01T00:00:00+00:00", None) is True
        assert _in_window({"timestamp": "2026-08-26T10:00:00+00:00"}, "nonsense", None) is True

    def test_the_window_bounds_are_inclusive(self, cron_env):
        from cron.changesets import _in_window

        row = {"timestamp": "2026-08-26T10:00:00+00:00"}
        assert _in_window(row, "2026-08-26T10:00:00+00:00", "2026-08-26T10:00:00+00:00")
        assert not _in_window(row, "2026-08-26T10:00:01+00:00", None)
        assert not _in_window(row, None, "2026-08-26T09:59:59+00:00")

    def test_diff_serves_the_two_configurations(self, cron_env):
        from cron.changesets import read_changeset_diff, read_changesets
        from cron.jobs import create_job, get_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        update_job(job["id"], {"schedule": "every 6h"})

        newest = read_changesets(limit=1)["changesets"][0]
        payload = read_changeset_diff(newest["id"])

        assert set(payload) == {"before", "after"}

        def _schedule(graph):
            return {
                node["id"]: node.get("schedule")
                for node in graph["nodes"]
                if node["kind"] == "cron"
            }[job["id"]]

        # The snapshots carry the same display form the graph serves, so the two
        # sides are directly comparable and the edit is visible in them.
        assert _schedule(payload["after"]) == get_job(job["id"])["schedule_display"]
        assert _schedule(payload["before"]) != _schedule(payload["after"])

    def test_the_baseline_has_no_before(self, cron_env):
        """Omitted, not empty — and the baseline's own ``parent_digest`` is empty,
        which is the client's licence to read it against the empty graph."""
        from cron.changesets import read_changeset_diff
        from cron.jobs import create_job

        create_job(prompt="collect", schedule="every 1h")
        baseline = _rows()[0]

        payload = read_changeset_diff(baseline["id"])
        assert "before" not in payload
        assert baseline["parent_digest"] == ""

    def test_a_trimmed_parent_has_no_before(self, cron_env, monkeypatch):
        """The oldest surviving row keeps a non-empty ``parent_digest``.

        Sending an empty ``before`` for it would report a steady-state
        configuration as freshly built; sending none at all, with the parent
        digest still there, tells the client the comparison isn't available.
        """
        import cron.changesets as changesets_mod
        from cron.changesets import read_changeset_diff
        from cron.jobs import create_job, update_job

        monkeypatch.setattr(changesets_mod, "MAX_CHANGESETS", 2)
        job = create_job(prompt="collect", schedule="every 1h")
        for hours in (2, 3, 4):
            update_job(job["id"], {"schedule": f"every {hours}h"})

        oldest = _rows()[0]
        assert oldest["parent_digest"] != ""
        assert "before" not in read_changeset_diff(oldest["id"])

    def test_an_unknown_id_is_not_an_empty_diff(self, cron_env):
        from cron.changesets import read_changeset_diff
        from cron.jobs import create_job

        create_job(prompt="collect", schedule="every 1h")
        assert read_changeset_diff("nope") is None
        assert read_changeset_diff("") is None

    def test_a_torn_line_does_not_sink_the_history(self, cron_env):
        from cron.changesets import changeset_log_path
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        update_job(job["id"], {"schedule": "every 6h"})
        path = changeset_log_path()
        path.write_text(path.read_text(encoding="utf-8") + '{"id": "torn"\n', encoding="utf-8")

        assert len(_rows()) == 2

    def test_the_baseline_can_be_opened_before_any_change(self, cron_env):
        """Laid down at read time, it holds the true pre-change configuration —
        so the next edit records as an ordinary change with a real ``before``."""
        from cron.changesets import ensure_baseline, read_changeset_diff
        from cron.jobs import create_job, update_job

        job = create_job(prompt="collect", schedule="every 1h")
        # Drop the log to simulate a store that predates recording entirely.
        from cron.changesets import changeset_head_path, changeset_log_path

        changeset_log_path().unlink()
        changeset_head_path().unlink()

        assert ensure_baseline() is not None
        assert ensure_baseline() is None  # idempotent

        update_job(job["id"], {"schedule": "every 6h"})
        rows = _rows()
        assert [row["action"] for row in rows] == ["baseline", "update"]
        assert "before" in read_changeset_diff(rows[1]["id"])


# =============================================================================
# The store seam
# =============================================================================

class TestStoreIsolation:
    def test_the_log_lives_beside_the_jobs_it_describes(self, cron_env):
        from cron.changesets import changeset_log_path
        from cron.jobs import create_job

        create_job(prompt="collect", schedule="every 1h")
        path = changeset_log_path()
        assert path.parent == cron_env / "cron"
        assert path.exists()

    def test_recording_never_breaks_a_save(self, cron_env, monkeypatch):
        """A history that can fail a save would be a worse feature than none."""
        import cron.changesets as changesets_mod
        from cron.jobs import create_job, get_job

        def _explode(*_args, **_kwargs):
            raise RuntimeError("log volume full")

        monkeypatch.setattr(changesets_mod, "record_change", _explode)
        job = create_job(prompt="collect", schedule="every 1h")

        assert get_job(job["id"]) is not None
        assert _rows() == []
