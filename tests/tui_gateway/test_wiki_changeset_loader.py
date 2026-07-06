"""Regression: a stale ~/.hermes/scripts/wiki_changeset.py must not shadow
the repo-bundled module.

Deployed gateways carry an old copy of wiki_changeset.py under
~/.hermes/scripts. The previous sys.path bootstrap let that stale copy win,
so new symbols (wiki_changeset_diff) raised
"cannot import name 'wiki_changeset_diff' from 'wiki_changeset'" even though
the repo copy had them. The loader now imports by explicit file path and
skips candidates lacking the required symbol.
"""

import pytest

from tui_gateway import wiki_api


@pytest.fixture
def stale_user_copy(tmp_path, monkeypatch):
    """A fake $HOME whose ~/.hermes/scripts/wiki_changeset.py is outdated."""
    scripts = tmp_path / ".hermes" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "wiki_changeset.py").write_text(
        "def wiki_query_changesets(**kw):\n"
        "    return {'changesets': [], 'stale': True, 'total': 0, 'limit': 50, 'offset': 0}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return scripts


class TestLoaderPrefersRepoCopy:
    def test_new_symbol_resolves_despite_stale_shadow(self, stale_user_copy):
        module = wiki_api._load_wiki_changeset_module("wiki_changeset_diff")
        assert hasattr(module, "wiki_changeset_diff")

    def test_changesets_not_hijacked_by_stale_copy(self, stale_user_copy, tmp_path, monkeypatch):
        wiki = tmp_path / "wiki"
        (wiki / "entities").mkdir(parents=True)
        result = wiki_api.wiki_changesets(wiki_path=str(wiki))
        # The stale copy tags its result; the repo copy never does.
        assert "stale" not in result

    def test_missing_symbol_reports_paths_tried(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))  # no user copy at all
        with pytest.raises(ImportError) as exc:
            wiki_api._load_wiki_changeset_module("nonexistent_function_xyz")
        assert "nonexistent_function_xyz" in str(exc.value)
