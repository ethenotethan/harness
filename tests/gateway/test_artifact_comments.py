"""Comments a reader pins to a living artifact: the store, the gateway RPCs
and the agent tool's read and resolve path."""

import json

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def report(hermes_home):
    from tui_gateway.artifact_store import set_artifact

    return set_artifact("report", "html", "<h1>Q3</h1><table></table>", title="Q3 report")


ANCHOR = {"selector": "body > table", "x": 0.25, "y": 0.5, "text": "Revenue  by\nregion"}


def test_a_comment_keeps_its_anchor_and_the_revision_it_was_written_on(report):
    from tui_gateway.artifact_comments import add_comment, list_comments

    comment = add_comment("report", "  Totals look stale.  ", author="app:1A2B", anchor=ANCHOR)

    assert comment["body"] == "Totals look stale."
    assert comment["rev"] == report["rev"]
    assert comment["anchor"] == {"selector": "body > table", "x": 0.25, "y": 0.5, "text": "Revenue by region"}
    assert comment["resolved"] is False
    assert list_comments("report") == [comment]


def test_commenting_leaves_the_artifact_and_its_history_untouched(report):
    from tui_gateway.artifact_comments import add_comment
    from tui_gateway.artifact_store import get_artifact, list_revisions

    add_comment("report", "Check this.")

    assert get_artifact("report") == report
    assert len(list_revisions("report")) == 1


def test_comments_survive_the_document_being_replaced(report):
    from tui_gateway.artifact_comments import add_comment, list_comments
    from tui_gateway.artifact_store import set_artifact

    comment = add_comment("report", "Check this.", anchor=ANCHOR)
    set_artifact("report", "html", "<h1>Q4</h1>", replace=True)

    assert list_comments("report") == [comment]


def test_resolving_hides_a_comment_from_the_open_list_and_reopening_restores_it(report):
    from tui_gateway.artifact_comments import add_comment, list_comments, open_comment_count, resolve_comment

    comment = add_comment("report", "Check this.")
    resolved = resolve_comment("report", comment["id"], resolved_by="agent:s1")

    assert resolved["resolved"] is True and resolved["resolved_by"] == "agent:s1"
    assert list_comments("report", include_resolved=False) == []
    assert open_comment_count("report") == 0

    reopened = resolve_comment("report", comment["id"], resolved=False)
    assert reopened["resolved"] is False and "resolved_by" not in reopened
    assert open_comment_count("report") == 1


def test_deleting_the_artifact_deletes_its_comments(report):
    from tui_gateway.artifact_comments import add_comment, list_comments
    from tui_gateway.artifact_store import delete_artifact, set_artifact

    add_comment("report", "Check this.")
    delete_artifact("report")
    set_artifact("report", "html", "<p>new</p>")

    assert list_comments("report") == []


@pytest.mark.parametrize("body, anchor, message", [
    ("", None, "body is required"),
    ("x" * 4001, None, "body exceeds"),
    ("bad\x00byte", None, "control characters"),
    ("ok", {"x": 1.5}, "between 0 and 1"),
    ("ok", {"selector": "a", "script": "x"}, "unknown fields"),
    ("ok", {"selector": "s" * 513}, "anchor.selector exceeds"),
    ("ok", "body > p", "anchor must be an object"),
])
def test_invalid_comments_are_refused(report, body, anchor, message):
    from tui_gateway.artifact_comments import add_comment

    with pytest.raises(ValueError, match=message):
        add_comment("report", body, anchor=anchor)


def test_an_unknown_artifact_or_comment_is_reported_not_created(report):
    from tui_gateway.artifact_comments import add_comment, delete_comment, resolve_comment

    assert add_comment("missing", "hello") is None
    assert resolve_comment("report", "c-nope") is None
    assert delete_comment("report", "c-nope") is False


@pytest.mark.parametrize("artifact_id", ["../index", "a/b", ""])
def test_an_id_that_is_not_an_artifact_id_never_reaches_the_filesystem(hermes_home, artifact_id):
    from tui_gateway.artifact_comments import delete_comment, list_comments

    with pytest.raises(ValueError, match="invalid artifact id"):
        list_comments(artifact_id)
    with pytest.raises(ValueError, match="invalid artifact id"):
        delete_comment(artifact_id, "c-1")


# ── gateway RPCs, through the real dispatch path ───────────────────────────


def _call(method: str, params: dict) -> dict:
    from tui_gateway import server

    return server.dispatch({"jsonrpc": "2.0", "id": 7, "method": method, "params": params})


@pytest.fixture
def events(monkeypatch):
    from tui_gateway import server

    seen = []
    monkeypatch.setattr(server, "_emit", lambda name, sid, payload: seen.append((name, payload)))
    return seen


def test_the_rpcs_add_list_resolve_and_delete_a_comment_and_announce_each_change(report, events):
    added = _call("artifact.comment.add", {
        "artifact_id": "report", "body": "Totals look stale.", "anchor": ANCHOR, "author": "app:1A2B",
    })
    comment = added["result"]["comment"]
    assert comment["anchor"]["selector"] == "body > table"

    listed = _call("artifact.comment.list", {"artifact_id": "report"})
    assert [c["id"] for c in listed["result"]["comments"]] == [comment["id"]]

    resolved = _call("artifact.comment.resolve", {"artifact_id": "report", "comment_id": comment["id"]})
    assert resolved["result"]["comment"]["resolved"] is True
    open_only = _call("artifact.comment.list", {"artifact_id": "report", "include_resolved": False})
    assert open_only["result"]["comments"] == []

    deleted = _call("artifact.comment.delete", {"artifact_id": "report", "comment_id": comment["id"]})
    assert deleted["result"] == {"deleted": comment["id"]}

    assert events == [
        ("artifact.comment.changed", {"artifact_id": "report", "comment_id": comment["id"], "change": "added", "open": 1}),
        ("artifact.comment.changed", {"artifact_id": "report", "comment_id": comment["id"], "change": "resolved", "open": 0}),
        ("artifact.comment.changed", {"artifact_id": "report", "comment_id": comment["id"], "change": "deleted", "open": 0}),
    ]


def test_the_rpcs_answer_not_found_and_invalid_input_with_their_codes(report, events):
    assert _call("artifact.comment.add", {"artifact_id": "missing", "body": "x"})["error"]["code"] == 4004
    assert _call("artifact.comment.list", {"artifact_id": "missing"})["error"]["code"] == 4004
    assert _call("artifact.comment.add", {"artifact_id": "report", "body": ""})["error"]["code"] == 4001
    assert _call("artifact.comment.resolve", {"artifact_id": "report", "comment_id": "c-nope"})["error"]["code"] == 4004
    assert _call("artifact.comment.add", {"artifact_id": "report", "body": "x", "anchor": {"left": 1}})["error"]["code"] == 4000
    assert events == []


def test_the_capability_is_advertised():
    names = _call("gateway.capabilities", {})["result"]
    assert "artifact.comment" in json.dumps(names)


# ── the agent's side ───────────────────────────────────────────────────────


def _tool(**kwargs) -> dict:
    from tools.artifact_tool import artifact_tool

    return json.loads(artifact_tool(**kwargs))


def test_the_agent_sees_open_comments_when_it_reads_the_artifact_and_can_resolve_them(report, events):
    from tui_gateway.artifact_comments import add_comment, list_comments

    assert "open_comments" not in _tool(action="get", id="report")
    comment = add_comment("report", "Totals look stale.", anchor=ANCHOR)

    assert _tool(action="get", id="report")["open_comments"] == 1
    assert _tool(action="comments", id="report")["comments"] == [comment]

    result = _tool(action="resolve_comment", id="report", comment_id=comment["id"], session_id="s1")
    assert result["comment"]["resolved_by"] == "agent:s1"
    assert _tool(action="comments", id="report")["comments"] == []
    assert list_comments("report")[0]["resolved"] is True
    assert events[-1][1]["change"] == "resolved"


def test_the_agent_is_told_when_a_comment_does_not_exist(report):
    assert _tool(action="resolve_comment", id="report", comment_id="c-nope")["success"] is False
    assert _tool(action="comments", id="missing")["success"] is False
