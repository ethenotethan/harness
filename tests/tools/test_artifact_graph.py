"""Living artifacts on the cron dataflow graph.

The store's documents (``tui_gateway/artifact_store.py``) name the crons that
tend them in their JSON content (``maintainers``) and are stamped with who last
wrote them (``updated_by``). ``tools/artifact_graph.py`` turns that into graph
declarations and ``build_cron_graph(artifacts=…)`` draws them: one ``artifact``
node per document, ``maintains`` edges from known maintaining crons, an observed
``writes`` edge from a ``cron:`` writer nothing else links, and a merge onto any
``artifact:<id>`` ref a job already declared. The configuration digest treats
``maintains`` as topology and the revision stamps as weather.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME shared by the cron store and the artifact store."""
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


def _seed(store, job_id):
    """Three artifacts: a JSON map with a known and an unknown maintainer, a
    markdown doc a cron last wrote, an html page a session last wrote."""
    store.set_artifact(
        artifact_id="ops-map",
        kind="map",
        title="Ops map",
        content=json.dumps({
            "maintainers": [f"cron:{job_id}", "cron:ghost"],
            "markers": [{"label": "HQ", "lat": 1, "lon": 2}],
        }),
        updated_by=f"cron:{job_id}",
        queries=[{"id": "orders.open", "query": "postgres.orders_open"}],
    )
    store.set_artifact(
        artifact_id="notes",
        kind="markdown",
        title="",
        content="# notes\n\nno maintainers here",
        updated_by=f"cron:{job_id}",
    )
    store.set_artifact(
        artifact_id="page",
        kind="html",
        title="Page",
        content="<h1>hi</h1>",
        updated_by="session:x",
    )


def _edges(graph, **match):
    return [
        e for e in graph["edges"]
        if all(e.get(key) == value for key, value in match.items())
    ]


def _node(graph, node_id):
    found = [n for n in graph["nodes"] if n["id"] == node_id]
    assert len(found) == 1, f"expected one {node_id!r} node, got {len(found)}"
    return found[0]


class TestCollector:
    def test_one_declaration_per_artifact_sorted_by_id(self, hermes_env):
        from cron.jobs import create_job
        from tools.artifact_graph import collect_graph_artifacts
        from tui_gateway import artifact_store as store

        job = create_job(prompt="tend the map", schedule="every 1h")
        _seed(store, job["id"])

        declarations = collect_graph_artifacts()
        assert [d["id"] for d in declarations] == [
            "artifact:notes", "artifact:ops-map", "artifact:page",
        ]

        ops = declarations[1]
        assert ops["label"] == "Ops map"
        assert ops["artifact_id"] == "ops-map"
        assert ops["artifact_kind"] == "map"
        assert ops["rev"] == 1
        assert ops["updated_by"] == f"cron:{job['id']}"
        assert ops["updated_at"]
        assert ops["maintainers"] == [f"cron:{job['id']}", "cron:ghost"]
        assert ops["queries"] == ["orders.open"]

        notes = declarations[0]
        assert notes["label"] == "notes"  # no title → id
        assert notes["artifact_kind"] == "markdown"
        assert notes["maintainers"] == []  # non-JSON kinds carry none
        assert "queries" not in notes

        page = declarations[2]
        assert page["updated_by"] == "session:x"
        assert page["maintainers"] == []

    def test_content_is_read_once_and_revisions_never(self, hermes_env, monkeypatch):
        from tools.artifact_graph import collect_graph_artifacts
        from tui_gateway import artifact_store as store

        store.set_artifact(artifact_id="a", kind="map", content=json.dumps({"maintainers": ["cron:x"]}))
        calls = {"list": 0}
        real_list = store.list_artifacts

        def counting_list(include_content=False):
            calls["list"] += 1
            return real_list(include_content=include_content)

        monkeypatch.setattr(store, "list_artifacts", counting_list)
        monkeypatch.setattr(
            store, "list_revisions",
            lambda *_a, **_k: pytest.fail("revisions must not be loaded"),
        )
        monkeypatch.setattr(
            store, "get_artifact",
            lambda *_a, **_k: pytest.fail("one index read, not one per artifact"),
        )
        assert collect_graph_artifacts()[0]["maintainers"] == ["cron:x"]
        assert calls["list"] == 1

    def test_malformed_content_yields_no_maintainers(self):
        from tools.artifact_graph import artifact_declaration

        assert artifact_declaration({"id": "a", "kind": "map", "content": "{not json"})["maintainers"] == []
        assert artifact_declaration({"id": "a", "kind": "table", "content": "[1, 2]"})["maintainers"] == []
        assert artifact_declaration({"id": "a", "kind": "map", "content": json.dumps({"maintainers": "cron:x"})})["maintainers"] == []
        assert artifact_declaration({"id": "a", "kind": "map", "content": json.dumps({"maintainers": ["cron:x", 3, " cron:x ", ""]})})["maintainers"] == ["cron:x"]
        assert artifact_declaration({"kind": "map", "content": "{}"}) is None

    def test_store_error_fails_open(self, hermes_env, monkeypatch, caplog):
        from tools.artifact_graph import collect_graph_artifacts
        from tui_gateway import artifact_store as store

        def boom(**_kw):
            raise OSError("index unreadable")

        monkeypatch.setattr(store, "list_artifacts", boom)
        with caplog.at_level(logging.WARNING, logger="tools.artifact_graph"):
            assert collect_graph_artifacts() == []
        assert any("artifact overlay unavailable" in rec.message for rec in caplog.records)


class TestGraph:
    def test_nodes_fields_and_edges(self, hermes_env):
        from cron.jobs import build_cron_graph, create_job
        from tools.artifact_graph import collect_graph_artifacts
        from tui_gateway import artifact_store as store

        job = create_job(prompt="tend the map", schedule="every 1h")
        jid = job["id"]
        _seed(store, jid)

        graph = build_cron_graph(artifacts=collect_graph_artifacts())

        ops = _node(graph, "artifact:ops-map")
        assert ops["kind"] == "artifact"
        assert ops["type"] == "artifact"
        assert ops["label"] == "Ops map"
        assert ops["artifact_id"] == "ops-map"
        assert ops["artifact_kind"] == "map"
        assert ops["rev"] == 1
        assert ops["updated_by"] == f"cron:{jid}"
        assert ops["updated_at"]
        # The unknown maintainer stays on the list …
        assert ops["maintainers"] == [f"cron:{jid}", "cron:ghost"]
        assert ops["queries"] == ["orders.open"]
        # … but draws nothing, and no node is fabricated for it.
        assert _edges(graph, source="ghost") == []
        assert not any(n["id"] == "ghost" for n in graph["nodes"])

        # Known maintainer → maintains; the observed write is covered by it.
        assert _edges(graph, target="artifact:ops-map") == [
            {"source": jid, "target": "artifact:ops-map", "type": "maintains"},
        ]

        # Markdown doc last written by the cron: observed write only.
        notes = _node(graph, "artifact:notes")
        assert notes["artifact_kind"] == "markdown"
        assert notes["maintainers"] == []
        assert _edges(graph, target="artifact:notes") == [
            {"source": jid, "target": "artifact:notes", "type": "writes"},
        ]

        # A session writer draws nothing; the node is still there.
        page = _node(graph, "artifact:page")
        assert page["updated_by"] == "session:x"
        assert _edges(graph, target="artifact:page") == []

        # The cron node is untouched by the overlay.
        assert _node(graph, jid)["kind"] == "cron"

    def test_unknown_maintainer_is_logged_at_debug(self, hermes_env, caplog):
        from cron.jobs import build_cron_graph

        with caplog.at_level(logging.DEBUG, logger="cron.jobs"):
            build_cron_graph(jobs=[], artifacts=[{
                "id": "artifact:a", "artifact_id": "a", "maintainers": ["cron:ghost"],
            }])
        assert any("no such job" in rec.message and rec.levelno == logging.DEBUG for rec in caplog.records)

    def test_merges_onto_a_declared_output(self, hermes_env):
        from cron.jobs import build_cron_graph, create_job
        from tools.artifact_graph import collect_graph_artifacts
        from tui_gateway import artifact_store as store

        job = create_job(
            prompt="tend the map", schedule="every 1h", outputs=["artifact:ops-map"],
        )
        jid = job["id"]
        _seed(store, jid)

        graph = build_cron_graph(artifacts=collect_graph_artifacts())

        # One node, not two: the declared ref and the living record met.
        ops = _node(graph, "artifact:ops-map")
        assert ops["kind"] == "artifact"
        assert ops["type"] == "artifact"
        assert ops["label"] == "Ops map"  # living fields enrich the bare ref
        assert ops["artifact_kind"] == "map"
        assert ops["maintainers"] == [f"cron:{jid}", "cron:ghost"]

        # Both the declared write and the maintains relation are drawn, once each.
        assert sorted(_edges(graph, target="artifact:ops-map"), key=lambda e: e["type"]) == [
            {"source": jid, "target": "artifact:ops-map", "type": "maintains"},
            {"source": jid, "target": "artifact:ops-map", "type": "writes"},
        ]

    def test_a_declared_input_becomes_an_artifact_node(self, hermes_env):
        from cron.jobs import build_cron_graph, create_job

        reader = create_job(prompt="read", schedule="every 1h", inputs=["artifact:ops-map"])
        graph = build_cron_graph(artifacts=[{
            "id": "artifact:ops-map", "artifact_id": "ops-map", "label": "Ops map",
            "artifact_kind": "map", "updated_by": "agent", "maintainers": [],
        }])
        ops = _node(graph, "artifact:ops-map")
        assert ops["kind"] == "artifact"  # was a bare "source" before the record arrived
        assert _edges(graph, source="artifact:ops-map") == [
            {"source": "artifact:ops-map", "target": reader["id"], "type": "reads"},
        ]

    def test_two_builds_are_identical(self, hermes_env):
        from cron.jobs import build_cron_graph, create_job
        from tools.artifact_graph import collect_graph_artifacts
        from tui_gateway import artifact_store as store

        job = create_job(prompt="tend", schedule="every 1h", outputs=["artifact:ops-map"])
        _seed(store, job["id"])

        first = build_cron_graph(artifacts=collect_graph_artifacts())
        second = build_cron_graph(artifacts=list(reversed(collect_graph_artifacts())))
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_no_artifacts_is_the_old_graph(self, hermes_env):
        from cron.jobs import build_cron_graph, create_job

        create_job(prompt="x", schedule="every 1h", side_effects=["notify:desktop"])
        assert build_cron_graph() == build_cron_graph(artifacts=None) == build_cron_graph(artifacts=[])


class TestCommitment:
    """``cron/changesets.configuration_digest`` over a graph with artifacts."""

    def _artifacts(self, jid, **overrides):
        base = {
            "id": "artifact:ops-map", "artifact_id": "ops-map", "label": "Ops map",
            "artifact_kind": "map", "rev": 1, "updated_at": "2026-09-28T09:00:00+00:00",
            "updated_by": "session:x", "maintainers": [f"cron:{jid}"],
        }
        base.update(overrides)
        return [base]

    def test_revision_stamps_do_not_move_the_digest(self, hermes_env):
        from cron.changesets import configuration_digest
        from cron.jobs import build_cron_graph, create_job

        jid = create_job(prompt="tend", schedule="every 1h")["id"]
        before = configuration_digest(build_cron_graph(artifacts=self._artifacts(jid)))
        after = configuration_digest(build_cron_graph(artifacts=self._artifacts(
            jid, rev=41, updated_at="2026-09-29T00:00:00+00:00", updated_by="agent",
            artifact_kind="table",
        )))
        assert before == after

    def test_a_maintainer_change_moves_the_digest(self, hermes_env):
        from cron.changesets import configuration_digest
        from cron.jobs import build_cron_graph, create_job

        jid = create_job(prompt="tend", schedule="every 1h")["id"]
        other = create_job(prompt="also tend", schedule="every 2h")["id"]
        one = configuration_digest(build_cron_graph(artifacts=self._artifacts(jid)))
        two = configuration_digest(build_cron_graph(artifacts=self._artifacts(jid, maintainers=[f"cron:{jid}", f"cron:{other}"])))
        none = configuration_digest(build_cron_graph(artifacts=self._artifacts(jid, maintainers=[])))
        assert len({one, two, none}) == 3

    def test_maintains_edges_are_rows(self, hermes_env):
        from cron.changesets import configuration_form
        from cron.jobs import build_cron_graph, create_job

        jid = create_job(prompt="tend", schedule="every 1h")["id"]
        rows = configuration_form(build_cron_graph(artifacts=self._artifacts(jid)))
        assert any(row.startswith("1:e") and "maintains" in row for row in rows)


class TestCronGraphRPC:
    @pytest.fixture
    def handler(self, monkeypatch):
        import tui_gateway.methods_tools as mt

        fn = dict(mt._registry._pending)["cron.graph"]
        captured = {}

        def _ok(rid, result):
            captured["rid"] = rid
            captured["result"] = result
            return {"result": result}

        def _err(rid, code, msg):
            captured["error"] = (code, msg)
            return {"error": {"code": code, "message": msg}}

        monkeypatch.setitem(fn.__globals__, "_ok", _ok)
        monkeypatch.setitem(fn.__globals__, "_err", _err)
        fn.__globals__.setdefault("logger", logging.getLogger("t"))

        # Keep the liveness providers out of it: this is about artifacts.
        import tools.service_graph as sg

        monkeypatch.setattr(sg, "collect_graph_services", lambda: [])
        return fn, captured

    def test_handler_returns_artifact_nodes(self, hermes_env, handler):
        from cron.jobs import create_job
        from tui_gateway import artifact_store as store

        fn, captured = handler
        jid = create_job(prompt="tend", schedule="every 1h")["id"]
        _seed(store, jid)

        fn(7, {})
        assert captured["rid"] == 7
        assert "error" not in captured
        graph = captured["result"]
        ops = _node(graph, "artifact:ops-map")
        assert ops["kind"] == "artifact"
        assert ops["maintainers"] == [f"cron:{jid}", "cron:ghost"]
        assert _edges(graph, source=jid, target="artifact:ops-map", type="maintains")
        assert _edges(graph, source=jid, target="artifact:notes", type="writes")

    def test_collector_failure_does_not_break_the_graph(self, hermes_env, handler, monkeypatch):
        from cron.jobs import create_job
        import tools.artifact_graph as ag

        fn, captured = handler
        jid = create_job(prompt="tend", schedule="every 1h")["id"]

        def boom():
            raise RuntimeError("overlay exploded")

        monkeypatch.setattr(ag, "collect_graph_artifacts", boom)
        fn(8, {})
        assert "error" not in captured
        graph = captured["result"]
        assert _node(graph, jid)["kind"] == "cron"
        assert not any(n["kind"] == "artifact" for n in graph["nodes"])
