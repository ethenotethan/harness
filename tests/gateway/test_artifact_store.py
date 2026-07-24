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
    import json as _json

    from tools.artifact_tool import artifact_tool as _raw_tool

    def artifact_tool(**kwargs):
        # The registry contract requires tools to return STRINGS — pin the
        # type here (a raw dict is rejected as tool_result_contract at
        # dispatch, which broke every artifact call in production).
        result = _raw_tool(**kwargs)
        assert isinstance(result, str), f"tool must return str, got {type(result).__name__}"
        return _json.loads(result)

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


def test_dataset_merge_unions_rows_by_key(artifact_home):
    from tui_gateway import artifact_store as store

    store.set_artifact(
        "contributors",
        "dataset",
        json.dumps({
            "key": "login",
            "columns": ["login", "name", "commits"],
            "rows": [
                {"login": "greg", "name": "Greg", "commits": 41},
                {"login": "0xclandestine", "name": "0xClandestine", "commits": 12},
            ],
        }),
        title="Darkbloom Contributors",
    )
    merged = store.set_artifact(
        "contributors",
        "dataset",
        json.dumps({
            "rows": [
                {"login": "greg", "name": "Greg", "commits": 44},
                {"login": "newperson", "name": "New Person", "commits": 1},
            ],
        }),
    )
    body = json.loads(merged["content"])
    rows = {r["login"]: r for r in body["rows"]}
    assert len(rows) == 3                       # union, not replace
    assert rows["greg"]["commits"] == 44        # incoming wins
    assert rows["0xclandestine"]["commits"] == 12  # untouched rows survive
    assert body["key"] == "login"               # key carried over
    assert body["columns"] == ["login", "name", "commits"]

    # Keyless rows are dropped, not crashed on.
    weird = store.set_artifact(
        "contributors", "dataset",
        json.dumps({"rows": [{"name": "no login"}]}),
    )
    assert len(json.loads(weird["content"])["rows"]) == 3


def test_tombstones_survive_merge(artifact_home):
    """A user-deleted entry (_deleted: true, set from the app) must not be
    resurrected by an agent re-emitting the same row/marker without the
    flag; an explicit _deleted (true/false) on the incoming entry wins."""
    from tui_gateway import artifact_store as store

    store.set_artifact(
        "confs", "dataset",
        json.dumps({
            "key": "name",
            "rows": [
                {"name": "Acme Conf", "_deleted": True},
                {"name": "Other Conf", "status": "going"},
            ],
        }),
    )
    merged = store.set_artifact(
        "confs", "dataset",
        json.dumps({"rows": [
            {"name": "Acme Conf", "status": "found again"},   # no _deleted → stays dead
            {"name": "Third Conf"},
        ]}),
    )
    rows = {r["name"]: r for r in json.loads(merged["content"])["rows"]}
    assert rows["Acme Conf"]["_deleted"] is True
    assert rows["Acme Conf"]["status"] == "found again"   # fields still merge
    assert "Third Conf" in rows

    # Explicit un-delete wins.
    revived = store.set_artifact(
        "confs", "dataset",
        json.dumps({"rows": [{"name": "Acme Conf", "_deleted": False}]}),
    )
    rows = {r["name"]: r for r in json.loads(revived["content"])["rows"]}
    assert rows["Acme Conf"]["_deleted"] is False


def test_map_marker_tombstones_survive_merge(artifact_home):
    from tui_gateway import artifact_store as store

    store.set_artifact(
        "apts", "map",
        json.dumps({"markers": [
            {"lat": 1.0, "lon": 2.0, "label": "gone", "_deleted": True},
        ]}),
    )
    merged = store.set_artifact(
        "apts", "map",
        json.dumps({"markers": [
            {"lat": 1.0, "lon": 2.0, "label": "gone", "note": "re-listed"},
            {"lat": 3.0, "lon": 4.0, "label": "new"},
        ]}),
    )
    markers = {m["label"]: m for m in json.loads(merged["content"])["markers"]}
    assert markers["gone"]["_deleted"] is True
    assert "new" in markers


def test_model_merge_entity_sets_relations_tombstones(artifact_home):
    """Ensemble models: per-set union by key with tombstone carry, untouched
    sets survive a partial update, relations dedupe by (from, to, type)."""
    from tui_gateway import artifact_store as store

    store.set_artifact(
        "bkk-life", "model",
        json.dumps({
            "entities": {
                "apartments": {"key": "name", "items": [
                    {"name": "A", "_deleted": True},
                    {"name": "B", "status": "viewed"},
                ]},
                "gyms": {"key": "name", "items": [{"name": "Felix"}]},
            },
            "relations": [{"from": "apartments/B", "to": "gyms/Felix", "type": "walkable"}],
        }),
    )
    merged = store.set_artifact(
        "bkk-life", "model",
        json.dumps({
            "entities": {"apartments": {"key": "name", "items": [
                {"name": "A", "rent": 999},          # no _deleted → stays dead
                {"name": "C"},
            ]}},
            "relations": [
                {"from": "apartments/B", "to": "gyms/Felix", "type": "walkable", "note": "8 min"},
                {"from": "apartments/C", "to": "gyms/Felix", "type": "walkable"},
            ],
        }),
    )
    body = json.loads(merged["content"])
    apartments = {i["name"]: i for i in body["entities"]["apartments"]["items"]}
    assert apartments["A"]["_deleted"] is True       # tombstone carried
    assert apartments["A"]["rent"] == 999            # fields still merge
    assert "B" in apartments and "C" in apartments
    assert "gyms" in body["entities"]                # untouched set survives
    rels = body["relations"]
    assert len(rels) == 2                            # triple-deduped
    assert any(r.get("note") == "8 min" for r in rels)  # incoming wins field-wise
