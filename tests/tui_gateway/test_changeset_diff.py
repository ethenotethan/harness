"""Tests for wiki_changeset_diff — the timeline's git-style diff view."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import wiki_changeset  # noqa: E402


@pytest.fixture
def git_wiki(tmp_path, monkeypatch):
    """A git-initialized scratch wiki, with WIKI_PATH pointed at it."""
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=wiki, check=True)
    monkeypatch.setenv("WIKI_PATH", str(wiki))
    return wiki


def _write(wiki: Path, rel: str, text: str) -> None:
    (wiki / rel).write_text(text, encoding="utf-8")


class TestChangesetDiff:
    def test_update_diff_shows_added_line(self, git_wiki):
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nLine one.\n")
        wiki_changeset.wiki_capture_changeset("entities/x.md", "create", "initial")
        _write(git_wiki, "entities/x.md", "---\ntitle: X\n---\nLine one.\nLine two.\n")
        cs = wiki_changeset.wiki_capture_changeset("entities/x.md", "update", "add line")

        res = wiki_changeset.wiki_changeset_diff(cs["id"])
        assert "error" not in res
        assert "+Line two." in res["diff"]
        assert res["changeset"]["id"] == cs["id"]

    def test_create_diff_is_all_additions(self, git_wiki):
        _write(git_wiki, "entities/y.md", "---\ntitle: Y\n---\nBody.\n")
        cs = wiki_changeset.wiki_capture_changeset("entities/y.md", "create", "new page")

        res = wiki_changeset.wiki_changeset_diff(cs["id"])
        assert "error" not in res
        assert "+Body." in res["diff"]
        assert "new file mode" in res["diff"]

    def test_unknown_id(self, git_wiki):
        res = wiki_changeset.wiki_changeset_diff("2099-01-01T000000-001")
        assert "not found" in res["error"]

    def test_traversal_rejected(self, git_wiki):
        for bad in ("../../../etc/passwd", "a/b", "a\\b", ".."):
            res = wiki_changeset.wiki_changeset_diff(bad)
            assert "invalid changeset id" in res["error"], bad

    def test_no_git_commit_recorded(self, git_wiki, tmp_path, monkeypatch):
        # A wiki without git: capture records empty git_commit; diff must
        # return a structured error (with the changeset) rather than crash.
        bare = tmp_path / "bare-wiki"
        (bare / "entities").mkdir(parents=True)
        monkeypatch.setenv("WIKI_PATH", str(bare))
        _write(bare, "entities/z.md", "---\ntitle: Z\n---\nBody.\n")
        cs = wiki_changeset.wiki_capture_changeset("entities/z.md", "create", "no-git page")
        assert cs.get("git_commit", "") == ""

        res = wiki_changeset.wiki_changeset_diff(cs["id"])
        assert "no git commit" in res["error"]
        assert res["changeset"]["id"] == cs["id"]
