"""The read-only file browser: containment is the whole security contract.

The desktop can list two allowlisted roots (the repo checkout and ~/.hermes)
and read UTF-8 text files under them — and nothing else. These tests pin that
contract against a tmp tree: the walk prunes noise dirs and stays bounded,
reads return content + metadata, and every escape hatch (../ traversal, a
symlink pointing outside the root, binary blobs, oversize files) is refused
BEFORE any byte leaves the root.
"""

import os

import pytest

from tui_gateway.files_browse import (
    FileBrowseError,
    file_roots,
    list_tree,
    read_file,
    read_within,
)
import tui_gateway.files_browse as fb


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "indexing").mkdir()
    (tmp_path / "indexing" / "x402_snapshot.py").write_text(
        "print('snap')\n", encoding="utf-8"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Root\n", encoding="utf-8")
    # Noise that must never appear in the tree.
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    return tmp_path


def _make_root(tree):
    """Point the 'test' root at *tree* by monkeypatching file_roots callers."""
    return tree


def test_list_tree_lists_top_level(tree, monkeypatch):
    monkeypatch.setattr(fb, "file_roots", lambda: {"t": tree})
    res = list_tree("t")
    assert res["root"] == "t" and res["path"] == ""
    entries = res["entries"]
    by_path = {e["path"]: e for e in entries}
    assert "indexing" in by_path and by_path["indexing"]["type"] == "dir"
    assert "scripts" in by_path and by_path["scripts"]["has_children"] is True
    assert "README.md" in by_path and by_path["README.md"]["type"] == "file"
    assert by_path["README.md"]["size"] > 0
    # Dirs sort before files at this level.
    types = [e["type"] for e in entries]
    assert types == sorted(types, key=lambda t: t != "dir")


def test_list_tree_prunes_noise_dirs(tree, monkeypatch):
    monkeypatch.setattr(fb, "file_roots", lambda: {"t": tree})
    names = {e["name"] for e in list_tree("t")["entries"]}
    assert "__pycache__" not in names
    assert ".git" not in names
    assert "node_modules" not in names


def test_list_tree_descends_one_level(tree, monkeypatch):
    monkeypatch.setattr(fb, "file_roots", lambda: {"t": tree})
    res = list_tree("t", "indexing")
    assert res["path"] == "indexing"
    paths = {e["path"] for e in res["entries"]}
    assert os.path.join("indexing", "x402_snapshot.py") in paths


def test_list_tree_rejects_traversal(tree, monkeypatch):
    monkeypatch.setattr(fb, "file_roots", lambda: {"t": tree})
    with pytest.raises(FileBrowseError) as ei:
        list_tree("t", "../..")
    assert ei.value.code == 4020


def test_list_tree_missing_dir(tree, monkeypatch):
    monkeypatch.setattr(fb, "file_roots", lambda: {"t": tree})
    with pytest.raises(FileBrowseError) as ei:
        list_tree("t", "nope")
    assert ei.value.code == 4404


def test_list_tree_unknown_root():
    with pytest.raises(FileBrowseError) as ei:
        list_tree("bogus")
    assert ei.value.code == 4404


def test_read_within_returns_content_and_metadata(tree):
    payload = read_within(tree, os.path.join("indexing", "x402_snapshot.py"))
    assert payload["content"] == "print('snap')\n"
    assert payload["language"] == "py"
    assert payload["size"] == len(b"print('snap')\n")
    assert payload["read_only"] is False
    assert payload["path"] == os.path.join("indexing", "x402_snapshot.py")


def test_read_within_reports_read_only(tree):
    page = tree / "README.md"
    page.chmod(0o444)
    try:
        payload = read_within(tree, "README.md")
        # os.access(W_OK) is what we surface; skip if the test runs as a user
        # who can write regardless of mode (e.g. root in CI).
        if os.access(page, os.W_OK):
            pytest.skip("writable regardless of mode (privileged user)")
        assert payload["read_only"] is True
    finally:
        page.chmod(0o644)


def test_read_within_rejects_traversal(tree):
    with pytest.raises(FileBrowseError) as ei:
        read_within(tree, "../outside.txt")
    assert ei.value.code == 4020


def test_read_within_rejects_symlink_escape(tree, tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("nope\n", encoding="utf-8")
    link = tree / "link.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(FileBrowseError) as ei:
        read_within(tree, "link.txt")
    assert ei.value.code == 4020


def test_read_within_rejects_binary(tree):
    (tree / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(FileBrowseError) as ei:
        read_within(tree, "blob.bin")
    assert ei.value.code == 4015


def test_read_within_rejects_non_utf8(tree):
    (tree / "latin.txt").write_bytes(b"\xff\xfe not utf8")
    with pytest.raises(FileBrowseError) as ei:
        read_within(tree, "latin.txt")
    assert ei.value.code == 4015


def test_read_within_rejects_oversize(tree, monkeypatch):
    monkeypatch.setattr(fb, "_MAX_READ_BYTES", 4)
    (tree / "big.txt").write_text("more than four bytes", encoding="utf-8")
    with pytest.raises(FileBrowseError) as ei:
        read_within(tree, "big.txt")
    assert ei.value.code == 4013


def test_read_within_missing_file(tree):
    with pytest.raises(FileBrowseError) as ei:
        read_within(tree, "does/not/exist.py")
    assert ei.value.code == 4404


def test_read_within_requires_path(tree):
    with pytest.raises(FileBrowseError) as ei:
        read_within(tree, "")
    assert ei.value.code == 4001


def test_file_roots_includes_repo():
    roots = file_roots()
    assert "repo" in roots
    assert roots["repo"].is_dir()
    # files_browse.py lives under the repo root it advertises.
    assert (roots["repo"] / "tui_gateway" / "files_browse.py").is_file()


def test_read_file_unknown_root_rejected():
    with pytest.raises(FileBrowseError) as ei:
        read_file("nope", "whatever.txt")
    assert ei.value.code == 4404
