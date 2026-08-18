"""The wiki watcher: programmatic changeset capture for out-of-band writes.

The feed used to depend on the agent being PROMPTED to run the capture CLI
after editing pages with file tools — no enforcement on insertion, so pages
written by terminal sessions/humans/scripts never reached the desktop's wiki
timeline. These tests pin the sweeper's contract: baseline captures nothing,
out-of-band writes capture exactly once, already-captured writes dedup by
content hash, and half-written files wait for quiescence.
"""

import json
import time
from pathlib import Path

import pytest

from tui_gateway.wiki_watch import _QUIESCENT_NS, sweep_once


class CaptureSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": f"cs-{len(self.calls)}", "page": kwargs["page_path"], **kwargs}


def _settled_ns() -> int:
    """A 'now' far enough in the future that every file is quiescent."""
    return time.time_ns() + 2 * _QUIESCENT_NS


@pytest.fixture()
def wiki(tmp_path: Path) -> Path:
    (tmp_path / "entities").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "changesets").mkdir()
    (tmp_path / "entities" / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    return tmp_path


def test_baseline_sweep_captures_nothing(wiki: Path):
    spy = CaptureSpy()
    snapshot, captured = sweep_once(None, wiki, spy, now_ns=_settled_ns())
    assert captured == []
    assert spy.calls == []
    assert "entities/alpha.md" in snapshot


def test_out_of_band_write_is_captured_once(wiki: Path):
    spy = CaptureSpy()
    snapshot, _ = sweep_once(None, wiki, spy, now_ns=_settled_ns())

    (wiki / "entities" / "beta.md").write_text("# Beta\n", encoding="utf-8")
    (wiki / "entities" / "alpha.md").write_text("# Alpha v2\n", encoding="utf-8")

    snapshot, captured = sweep_once(snapshot, wiki, spy, now_ns=_settled_ns())
    actions = {c["page_path"]: c["action"] for c in spy.calls}
    assert actions == {"entities/beta.md": "create", "entities/alpha.md": "update"}
    assert all(c["trigger"] == "auto" for c in spy.calls)
    assert len(captured) == 2

    # Steady state: nothing new, nothing captured.
    _, captured = sweep_once(snapshot, wiki, spy, now_ns=_settled_ns())
    assert captured == []
    assert len(spy.calls) == 2


def test_already_captured_write_dedups_by_hash(wiki: Path):
    """A write that came WITH its own changeset (wiki.update / capture CLI)
    must not be recorded twice."""
    spy = CaptureSpy()
    snapshot, _ = sweep_once(None, wiki, spy, now_ns=_settled_ns())

    page = wiki / "entities" / "alpha.md"
    page.write_text("# Alpha captured elsewhere\n", encoding="utf-8")

    import hashlib
    sha = hashlib.sha256(page.read_bytes()).hexdigest()
    (wiki / "changesets" / "cs-external.json").write_text(
        json.dumps({"id": "cs-external", "page": "entities/alpha.md", "after_sha256": sha}),
        encoding="utf-8",
    )
    (wiki / "changesets" / "index.json").write_text(
        json.dumps([{"id": "cs-external", "page": "entities/alpha.md"}]),
        encoding="utf-8",
    )

    _, captured = sweep_once(snapshot, wiki, spy, now_ns=_settled_ns())
    assert captured == []
    assert spy.calls == []


def test_half_written_file_waits_for_quiescence(wiki: Path):
    spy = CaptureSpy()
    snapshot, _ = sweep_once(None, wiki, spy, now_ns=_settled_ns())

    (wiki / "entities" / "fresh.md").write_text("# partial", encoding="utf-8")

    # Sweep with 'now' ~equal to the write time: inside the quiescence window.
    snapshot, captured = sweep_once(snapshot, wiki, spy, now_ns=time.time_ns())
    assert captured == []
    assert spy.calls == []
    assert "entities/fresh.md" not in snapshot, "must be re-examined next sweep"

    # Once quiescent, it captures.
    snapshot, captured = sweep_once(snapshot, wiki, spy, now_ns=_settled_ns())
    assert [c["page_path"] for c in spy.calls] == ["entities/fresh.md"]
    assert "entities/fresh.md" in snapshot


def test_raw_and_changesets_dirs_are_not_pages(wiki: Path):
    spy = CaptureSpy()
    snapshot, _ = sweep_once(None, wiki, spy, now_ns=_settled_ns())

    (wiki / "raw" / "event.md").write_text("source\n", encoding="utf-8")
    (wiki / "changesets" / "junk.md").write_text("x\n", encoding="utf-8")

    _, captured = sweep_once(snapshot, wiki, spy, now_ns=_settled_ns())
    assert captured == []
    assert spy.calls == []


def test_deleted_page_leaves_snapshot_without_capture(wiki: Path):
    spy = CaptureSpy()
    snapshot, _ = sweep_once(None, wiki, spy, now_ns=_settled_ns())
    (wiki / "entities" / "alpha.md").unlink()
    snapshot, captured = sweep_once(snapshot, wiki, spy, now_ns=_settled_ns())
    assert captured == []
    assert "entities/alpha.md" not in snapshot
