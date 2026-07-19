"""Living-artifact store: upsert/merge/revisions/caps against a temp
HERMES_HOME, plus the agent tool's read-before-write surface."""

import json

import pytest


@pytest.fixture()
def artifact_home(tmp_path, monkeypatch):
    # get_hermes_home() reads HERMES_HOME live — no cache to reset.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


def test_set_get_roundtrip_and_revisions(artifact_home):
    from tui_gateway import artifact_store as store

    first = store.set_artifact(
        "clients", "table", "| name |\n| Acme |", title="Client List", updated_by="test"
    )
    assert first["rev"] == 1
    assert first["title"] == "Client List"

    second = store.set_artifact("clients", "table", "| name |\n| Acme |\n| Foo |")
    assert second["rev"] == 2
    assert second["title"] == "Client List"  # title survives an untitled update

    fetched = store.get_artifact("clients")
    assert fetched["content"].endswith("| Foo |")

    revisions = store.list_revisions("clients")
    assert [r["rev"] for r in revisions] == [2, 1]  # newest first
    assert all("content" not in r for r in revisions)

    old = store.get_revision("clients", 1)
    assert old["content"] == "| name |\n| Acme |"


def test_map_merge_unions_markers_by_label(artifact_home):
    from tui_gateway import artifact_store as store

    store.set_artifact(
        "bkk",
        "map",
        json.dumps({
            "title": "BKK",
            "markers": [
                {"lat": 13.72, "lon": 100.58, "label": "Ekkamai loft", "group": "shortlist"},
                {"lat": 13.73, "lon": 100.56, "label": "Thonglor 2BR", "group": "viewed"},
            ],
        }),
    )
    merged = store.set_artifact(
        "bkk",
        "map",
        json.dumps({
            "markers": [
                {"lat": 13.72, "lon": 100.58, "label": "Ekkamai loft", "group": "rejected"},
                {"lat": 13.74, "lon": 100.54, "label": "Ari studio", "group": "shortlist"},
            ],
        }),
    )
    markers = json.loads(merged["content"])["markers"]
    assert len(markers) == 3  # union, not replace
    ekkamai = next(m for m in markers if m["label"] == "Ekkamai loft")
    assert ekkamai["group"] == "rejected"  # incoming wins
    assert json.loads(merged["content"])["title"] == "BKK"  # carried over

    # replace=True skips the merge entirely.
    replaced = store.set_artifact(
        "bkk", "map", json.dumps({"markers": [{"lat": 1, "lon": 2, "label": "only"}]}),
        replace=True,
    )
    assert len(json.loads(replaced["content"])["markers"]) == 1


def test_non_map_kinds_replace_wholesale(artifact_home):
    from tui_gateway import artifact_store as store

    store.set_artifact("spend", "chart", '{"series": [1]}')
    updated = store.set_artifact("spend", "chart", '{"series": [1, 2]}')
    assert updated["content"] == '{"series": [1, 2]}'


def test_validation_and_caps(artifact_home):
    from tui_gateway import artifact_store as store

    with pytest.raises(ValueError):
        store.set_artifact("", "map", "{}")
    with pytest.raises(ValueError):
        store.set_artifact("bad id with spaces", "map", "{}")
    with pytest.raises(ValueError):
        store.set_artifact("ok", "", "{}")
    with pytest.raises(ValueError):
        store.set_artifact("big", "markdown", "x" * (store.MAX_CONTENT_BYTES + 1))

    # Revision cap: rev numbers keep increasing, list is bounded.
    for i in range(store.MAX_REVISIONS + 5):
        store.set_artifact("hot", "markdown", f"v{i}")
    revisions = store.list_revisions("hot")
    assert len(revisions) == store.MAX_REVISIONS
    assert revisions[0]["rev"] == store.MAX_REVISIONS + 5


def test_delete_removes_artifact_and_revisions(artifact_home):
    from tui_gateway import artifact_store as store

    store.set_artifact("gone", "markdown", "body")
    assert store.delete_artifact("gone") is True
    assert store.get_artifact("gone") is None
    assert store.list_revisions("gone") == []
    assert store.delete_artifact("gone") is False


def test_agent_tool_surface(artifact_home):
    from tools.artifact_tool import artifact_tool

    # set → get read-before-write loop
    result = artifact_tool(
        action="set", id="clients", kind="table",
        content="| name |\n| Acme |", title="Clients", session_id="s1",
    )
    assert result["success"] is True
    assert result["artifact"]["updated_by"] == "agent:s1"
    assert "content" not in result["artifact"]  # summaries keep tool results small

    fetched = artifact_tool(action="get", id="clients")
    assert fetched["success"] is True
    assert "Acme" in fetched["artifact"]["content"]

    listing = artifact_tool(action="list")
    assert listing["success"] is True
    assert listing["artifacts"][0]["id"] == "clients"

    revs = artifact_tool(action="revisions", id="clients")
    assert revs["success"] is True and len(revs["revisions"]) == 1

    # Bad kind is a tool error, not an exception.
    bad = artifact_tool(action="set", id="x", kind="hologram", content="{}")
    assert bad["success"] is False and "kind" in bad["error"]

    missing = artifact_tool(action="get", id="nope")
    assert missing["success"] is False
