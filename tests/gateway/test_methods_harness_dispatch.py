"""End-to-end: Portal-facing RPCs dispatched through the REAL server path.

``tests/gateway/test_methods_harness_imports.py`` proves statically that every
name a split-module handler loads resolves in the namespace it is rebound
onto. These tests prove the same thing the way production does: build a
JSON-RPC request, hand it to ``tui_gateway.server.dispatch`` (the entry the
stdio/WS transports call), and assert an ``_ok`` frame came back rather than
``RPC error [5052]: name 'wiki_list' is not defined``.

Each test here corresponds to a handler the audit found calling a name that
only its own module's globals provided:

* ``wiki.list``            — module-level ``from tui_gateway.wiki_api import wiki_list``
* ``skills.get``           — module-level helpers ``_find_local_skill_md`` & co.
* ``learning.course.set``  — module-level helper ``_learning_changed``
"""

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tui_gateway import server
from tui_gateway import wiki_api


def _rpc(method: str, params: dict, rid: int = 7) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def test_wiki_list_dispatches_ok_through_real_server(monkeypatch):
    """The production bug: Portal's wiki picker showed "No wikis discovered"
    because ``wiki.list`` answered ``RPC error [5052]: name 'wiki_list' is not
    defined``. Stub the wiki API at its source module — the handler must reach
    it through a real import, not through a name that only existed in
    methods_harness.py's module scope."""
    calls = []

    def fake_wiki_list():
        calls.append(True)
        return {"wikis": []}

    monkeypatch.setattr(wiki_api, "wiki_list", fake_wiki_list)

    resp = server.dispatch(_rpc("wiki.list", {}))

    assert resp == {"jsonrpc": "2.0", "id": 7, "result": {"wikis": []}}, resp
    assert calls == [True]


def test_wiki_list_surfaces_wikis_from_registry(monkeypatch):
    monkeypatch.setattr(
        wiki_api, "wiki_list", lambda: {"wikis": [{"name": "notes", "path": "/tmp/notes"}]}
    )
    resp = server.dispatch(_rpc("wiki.list", {}, rid=8))
    assert "error" not in resp, resp
    assert resp["result"]["wikis"][0]["name"] == "notes"


def test_skills_get_dispatches_ok_through_real_server(hermes_home):
    """``skills.get`` called ``_find_local_skill_md`` / ``_parse_skill_frontmatter``
    / ``_skill_info_from_path`` — helpers defined at module level in
    methods_harness.py, hence absent from the rebound namespace."""
    skill_dir = hermes_home / "skills" / "general" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill\n---\n# Demo\n", encoding="utf-8"
    )

    resp = server.dispatch(_rpc("skills.get", {"skill_id": "demo"}))

    assert "error" not in resp, resp
    assert resp["result"]["skill"]["name"] == "demo"
    assert resp["result"]["file_path"] == "SKILL.md"
    assert resp["result"]["content"].startswith("---\nname: demo")


def test_learning_course_set_dispatches_ok_through_real_server(hermes_home, monkeypatch):
    """Every mutating ``learning.*`` handler calls ``_learning_changed`` — a helper
    defined at module level in methods_learning.py. register() now publishes it
    onto the server namespace, rebound so its own ``_emit`` is server.py's."""
    emitted = []
    monkeypatch.setattr(
        server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload))
    )

    resp = server.dispatch(
        _rpc("learning.course.set", {"title": "Intro", "summary": "s", "updated_by": "test"})
    )

    assert "error" not in resp, resp
    course = resp["result"]["course"]
    assert course["title"] == "Intro"
    assert emitted and emitted[0][0] == "learning.changed"
    assert emitted[0][2]["entity"] == "course"
    assert emitted[0][2]["id"] == course["id"]
