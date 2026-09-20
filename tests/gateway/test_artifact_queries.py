"""Tests for artifact backend queries — the read side of intents.

Covers:
- validate_params: coercion, defaults, required, unknown keys, bounds, enums
- validate_declarations: shape checks at write time; store carries queries
- invoke(): happy path through the built-in artifact.rows handler, etags,
  pagination, tombstones; conflict / unsupported / failed paths; artifact
  bind + narrowing; handler failures; result size cap; rate limit
- subscriptions: baseline result, poll cadence, emit only on etag change,
  mark_changed, unsubscribe, handler disappearing
- plugin loader: register_query_handler staged like intents; built-ins survive
"""

import json
import textwrap

import pytest


@pytest.fixture()
def artifact_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_queries():
    from tui_gateway import artifact_queries as aq
    original = dict(aq._QUERY_HANDLERS)
    aq.reset_for_tests()
    aq.set_emitter(None)
    yield
    aq.reset_for_tests()
    aq._QUERY_HANDLERS.clear()
    aq._QUERY_HANDLERS.update(original)
    aq.set_emitter(None)


# ── helpers ────────────────────────────────────────────────────────────────


def _dataset(artifact_id="orders", rows=None):
    from tui_gateway import artifact_store as store
    rows = rows if rows is not None else [
        {"id": "o1", "state": "open"},
        {"id": "o2", "state": "open"},
        {"id": "o3", "state": "closed", "_deleted": True},
    ]
    return store.set_artifact(
        artifact_id=artifact_id, kind="dataset",
        content=json.dumps({"key": "id", "rows": rows}),
        title="Orders", updated_by="test", replace=True,
    )


def _dashboard(queries, artifact_id="dash"):
    from tui_gateway import artifact_store as store
    return store.set_artifact(
        artifact_id=artifact_id, kind="html",
        content="<html><section data-hermes-query='rows'></section></html>",
        title="Dash", updated_by="test", queries=queries,
    )


ROWS_QUERY = {
    "id": "rows", "query": "artifact.rows",
    "bind": {"source": "orders"},
    "params": {"limit": {"type": "int", "min": 1, "max": 50, "default": 10}},
    "live": {"mode": "poll", "interval_s": 5},
}


# ── validate_params ────────────────────────────────────────────────────────


def test_validate_params_coerces_and_defaults():
    from tui_gateway.artifact_queries import validate_params
    schema = {
        "state": {"type": "enum", "values": ["open", "closed"], "default": "open"},
        "limit": {"type": "int", "min": 1, "max": 500, "default": 100},
        "q": {"type": "string", "max": 8},
        "ratio": {"type": "number", "min": 0},
        "flag": {"type": "bool"},
        "cursor": {"type": "cursor"},
    }
    out = validate_params(schema, {"limit": "25", "flag": "true", "ratio": 1, "q": "abc"})
    assert out == {"state": "open", "limit": 25, "q": "abc", "ratio": 1.0, "flag": True}


@pytest.mark.parametrize("supplied, message", [
    ({"nope": 1}, "unknown parameter"),
    ({"limit": True}, "expected an integer"),
    ({"limit": 0}, "below minimum"),
    ({"limit": 501}, "above maximum"),
    ({"state": "archived"}, "must be one of"),
    ({"q": "toolongvalue"}, "longer than"),
    ({"q": "bad\x00"}, "control characters"),
    ({"ratio": "nan"}, "finite"),
    ({"flag": "maybe"}, "expected a boolean"),
])
def test_validate_params_rejects(supplied, message):
    from tui_gateway.artifact_queries import QueryError, validate_params
    schema = {
        "state": {"type": "enum", "values": ["open", "closed"]},
        "limit": {"type": "int", "min": 1, "max": 500},
        "q": {"type": "string", "max": 8},
        "ratio": {"type": "number"},
        "flag": {"type": "bool"},
    }
    with pytest.raises(QueryError, match=message):
        validate_params(schema, supplied)


def test_validate_params_required_and_none_schema():
    from tui_gateway.artifact_queries import QueryError, validate_params
    with pytest.raises(QueryError, match="required"):
        validate_params({"source": {"type": "string", "required": True}}, {})
    assert validate_params(None, {}) == {}
    with pytest.raises(QueryError, match="unknown parameter"):
        validate_params(None, {"x": 1})


# ── declarations + store ───────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "not a list",
    [{"query": "artifact.rows"}],
    [{"id": "a"}],
    [{"id": "a", "query": "x"}, {"id": "a", "query": "y"}],
    [{"id": "a", "query": "x", "params": []}],
    [{"id": "a", "query": "x", "bind": "open"}],
    [{"id": "a", "query": "x", "invalidated_by": "archive"}],
])
def test_declarations_are_shape_checked_at_write(artifact_home, bad):
    from tui_gateway import artifact_store as store
    with pytest.raises(ValueError):
        store.set_artifact(
            artifact_id="dash", kind="html", content="<html/>",
            updated_by="test", queries=bad,
        )


def test_store_persists_and_carries_queries_forward(artifact_home):
    from tui_gateway import artifact_store as store
    first = _dashboard([ROWS_QUERY])
    assert first["queries"] == [ROWS_QUERY]
    # A later write that omits queries keeps them, exactly like actions.
    again = store.set_artifact(
        artifact_id="dash", kind="html", content="<html>v2</html>", updated_by="test",
    )
    assert again["queries"] == [ROWS_QUERY]
    assert store.get_artifact("dash")["queries"] == [ROWS_QUERY]


# ── invoke ─────────────────────────────────────────────────────────────────


def test_invoke_rows_happy_path(artifact_home):
    from tui_gateway.artifact_queries import invoke
    _dataset()
    dash = _dashboard([ROWS_QUERY])

    result = invoke("dash", dash["rev"], "rows", {"limit": 5})
    assert result["status"] == "ok"
    # Tombstoned rows are not data.
    assert [r["id"] for r in result["data"]["rows"]] == ["o1", "o2"]
    assert result["data"]["total"] == 2
    # The bound source travelled with the validated params.
    assert result["params"] == {"source": "orders", "limit": 5}
    assert len(result["etag"]) == 16
    # Identical data, identical etag — the subscription diff depends on it.
    assert invoke("dash", dash["rev"], "rows", {"limit": 5})["etag"] == result["etag"]


def test_invoke_paginates_with_cursor(artifact_home):
    from tui_gateway.artifact_queries import invoke
    _dataset(rows=[{"id": f"o{i}"} for i in range(5)])
    dash = _dashboard([ROWS_QUERY])

    page1 = invoke("dash", dash["rev"], "rows", {"limit": 2})
    assert [r["id"] for r in page1["data"]["rows"]] == ["o0", "o1"]
    assert page1["next_cursor"] == "2"
    page3 = invoke("dash", dash["rev"], "rows", {"limit": 2}, cursor="4")
    assert [r["id"] for r in page3["data"]["rows"]] == ["o4"]
    assert "next_cursor" not in page3


def test_invoke_conflict_unsupported_and_failed(artifact_home):
    from tui_gateway.artifact_queries import invoke
    _dataset()
    dash = _dashboard([ROWS_QUERY, {"id": "ghost", "query": "nobody.registered.this"}])

    assert invoke("dash", dash["rev"] + 1, "rows", {})["status"] == "conflict"
    # None skips the revision check — what the poller uses.
    assert invoke("dash", None, "rows", {})["status"] == "ok"
    assert invoke("dash", dash["rev"], "nope", {})["status"] == "unsupported"
    ghost = invoke("dash", dash["rev"], "ghost", {})
    assert ghost["status"] == "unsupported"
    assert "plugin" in ghost["reason"]
    assert invoke("missing", 1, "rows", {})["status"] == "failed"


def test_invoke_enforces_artifact_narrowing_and_binding(artifact_home):
    from tui_gateway.artifact_queries import invoke
    _dataset()
    dash = _dashboard([ROWS_QUERY])
    rev = dash["rev"]

    # The artifact capped limit at 50 even though the handler allows 1000.
    over = invoke("dash", rev, "rows", {"limit": 51})
    assert over["status"] == "failed" and "above maximum" in over["reason"]
    # A page cannot repoint a bound parameter at a different artifact.
    hijack = invoke("dash", rev, "rows", {"source": "secrets"})
    assert hijack["status"] == "failed" and "bound" in hijack["reason"]
    # ...and cannot invent parameters the declaration doesn't have.
    extra = invoke("dash", rev, "rows", {"where": "1=1"})
    assert extra["status"] == "failed" and "unknown parameter" in extra["reason"]


def test_invoke_reports_handler_failures_and_caps_result_size(artifact_home):
    from tui_gateway import artifact_queries as aq

    def boom(**kw):
        raise RuntimeError("connection refused")

    def huge(**kw):
        return {"data": ["x" * 1000] * 300}

    def not_json(**kw):
        return {"data": {"when": object()}}

    aq.register_query_handler("test.boom", boom)
    aq.register_query_handler("test.huge", huge)
    aq.register_query_handler("test.obj", not_json)
    dash = _dashboard([
        {"id": "boom", "query": "test.boom"},
        {"id": "huge", "query": "test.huge"},
        {"id": "obj", "query": "test.obj"},
    ])
    boom_r = aq.invoke("dash", dash["rev"], "boom")
    assert boom_r["status"] == "failed" and "connection refused" in boom_r["reason"]
    huge_r = aq.invoke("dash", dash["rev"], "huge")
    assert huge_r["status"] == "failed" and "exceeds" in huge_r["reason"]
    # Non-JSON values are stringified (default=str), not rejected: dates and
    # Decimals from a database driver must land as text, not as an error.
    assert aq.invoke("dash", dash["rev"], "obj")["status"] == "ok"


def test_invoke_rate_limits_a_hot_slot(artifact_home, monkeypatch):
    from tui_gateway import artifact_queries as aq
    monkeypatch.setattr(aq, "RATE_LIMIT_CALLS", 3)
    _dataset()
    dash = _dashboard([ROWS_QUERY])
    for _ in range(3):
        assert aq.invoke("dash", dash["rev"], "rows")["status"] == "ok"
    limited = aq.invoke("dash", dash["rev"], "rows")
    assert limited["status"] == "failed" and "rate limited" in limited["reason"]


# ── subscriptions ──────────────────────────────────────────────────────────


def test_subscribe_requires_live_and_returns_baseline(artifact_home):
    from tui_gateway import artifact_queries as aq
    _dataset()
    static = dict(ROWS_QUERY, id="static")
    static.pop("live")
    dash = _dashboard([ROWS_QUERY, static])

    assert aq.subscribe("dash", dash["rev"], "static")["status"] == "unsupported"
    sub = aq.subscribe("dash", dash["rev"], "rows", {"limit": 5})
    assert sub["status"] == "ok"
    assert sub["subscription"] == aq.subscription_key("dash", "rows", {"source": "orders", "limit": 5})
    assert sub["interval_s"] == 5  # declared 5, at the clamp floor
    assert [r["id"] for r in sub["data"]["rows"]] == ["o1", "o2"]
    assert len(aq.active_subscriptions()) == 1
    aq.stop_poller()


def test_poll_emits_only_when_data_changed(artifact_home):
    import time
    from tui_gateway import artifact_queries as aq
    emitted = []
    aq.set_emitter(lambda name, payload: emitted.append((name, payload)))
    _dataset()
    dash = _dashboard([ROWS_QUERY])
    aq.subscribe("dash", dash["rev"], "rows", {"limit": 5})
    aq.stop_poller()
    now = time.monotonic()

    # Not due yet.
    assert aq.run_due(now) == []
    # Due, unchanged: silent.
    assert aq.run_due(now + 10) == []
    assert emitted == []
    # The source moved: one event, carrying the new etag.
    _dataset(rows=[{"id": "o9", "state": "open"}])
    events = aq.run_due(now + 20)
    assert len(events) == 1 and events[0]["status"] == "ok"
    assert emitted[0][0] == "artifact.query.changed"
    assert emitted[0][1]["artifact_id"] == "dash" and emitted[0][1]["query_id"] == "rows"
    # Same data again: silent again.
    assert aq.run_due(now + 30) == []


def test_mark_changed_makes_a_slot_due_now(artifact_home):
    import time
    from tui_gateway import artifact_queries as aq
    _dataset()
    dash = _dashboard([ROWS_QUERY])
    aq.subscribe("dash", dash["rev"], "rows")
    aq.stop_poller()
    now = time.monotonic()
    assert aq.run_due(now) == []
    _dataset(rows=[{"id": "fresh"}])
    # A plugin's LISTEN thread would call this; nothing is due by cadence.
    assert aq.mark_changed("artifact") == 1
    assert aq.mark_changed("postgres") == 0
    assert len(aq.run_due(now)) == 1


def test_rpc_subscription_broadcasts_changed_to_live_transports(artifact_home, monkeypatch):
    """Background query pushes must not depend on a request-local transport."""
    import time
    from tui_gateway import artifact_queries as aq, server

    broadcasts = []
    monkeypatch.setattr(
        server,
        "_broadcast_global_event",
        lambda event, payload=None: broadcasts.append((event, payload)),
    )
    _dataset()
    dash = _dashboard([ROWS_QUERY])

    response = server._methods["artifact.query.subscribe"]("sub-1", {
        "artifact_id": "dash",
        "artifact_rev": dash["rev"],
        "query_id": "rows",
        "params": {"limit": 5},
    })
    assert response["result"]["status"] == "ok"
    aq.stop_poller()

    _dataset(rows=[{"id": "fresh", "state": "open"}])
    assert len(aq.run_due(time.monotonic() + 20)) == 1
    assert broadcasts == [(
        "artifact.query.changed",
        {
            "artifact_id": "dash",
            "query_id": "rows",
            "params_hash": response["result"]["subscription"].rsplit("/", 1)[-1],
            "etag": broadcasts[0][1]["etag"],
            "status": "ok",
        },
    )]


def test_unsubscribe_and_vanished_handler(artifact_home):
    import time
    from tui_gateway import artifact_queries as aq
    emitted = []
    aq.set_emitter(lambda name, payload: emitted.append(payload))
    _dataset()
    dash = _dashboard([ROWS_QUERY])
    handle = aq.subscribe("dash", dash["rev"], "rows")["subscription"]
    aq.subscribe("dash", dash["rev"], "rows")  # second subscriber, same slot
    aq.stop_poller()
    assert len(aq.active_subscriptions()) == 1
    assert aq.unsubscribe(handle) == {"status": "ok", "removed": False}
    assert aq.unsubscribe(handle) == {"status": "ok", "removed": True}
    assert aq.active_subscriptions() == []

    # A slot whose handler goes away reports itself once, then stops.
    aq.subscribe("dash", dash["rev"], "rows")
    aq.stop_poller()
    aq._QUERY_HANDLERS.pop("artifact.rows")
    events = aq.run_due(time.monotonic() + 100)
    assert events[0]["status"] == "unsupported"
    assert aq.active_subscriptions() == []
    assert emitted[-1]["status"] == "unsupported"


# ── plugin loader ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_intent_registry():
    from tui_gateway import artifact_actions as aa
    original = dict(aa._HANDLERS)
    yield
    aa._HANDLERS.clear()
    aa._HANDLERS.update(original)


def test_plugin_registers_query_handler(artifact_home):
    from tui_gateway import artifact_plugin_loader as pl, artifact_queries as aq
    plugins = artifact_home / ".hermes" / "plugins" / "actions"
    plugins.mkdir(parents=True)
    (plugins / "weather.py").write_text(textwrap.dedent('''
        def _temps(artifact_id, query_id, params, cursor):
            if params["city"] == "nowhere":
                raise QueryError("no such city")
            return {"data": {"city": params["city"], "c": 21}}
        register_query_handler("weather.now", _temps,
                               params={"city": {"type": "string", "max": 40, "required": True}})
        # The change hook is in scope for LISTEN / webhook threads.
        assert callable(mark_query_changed)
    '''))
    result = pl.reload()
    assert result["status"] == "ok"
    assert result["queries"]["added"] == ["weather.now"]
    # The built-in survives a reload, exactly like intent built-ins.
    assert "artifact.rows" in aq.registered_query_names()

    dash = _dashboard([{"id": "now", "query": "weather.now"}])
    ok = aq.invoke("dash", dash["rev"], "now", {"city": "Bangkok"})
    assert ok["status"] == "ok" and ok["data"] == {"city": "Bangkok", "c": 21}
    missing = aq.invoke("dash", dash["rev"], "now", {})
    assert missing["status"] == "failed" and "required" in missing["reason"]
    typed = aq.invoke("dash", dash["rev"], "now", {"city": "nowhere"})
    assert typed["status"] == "failed" and typed["reason"] == "no such city"

    # Removing the file removes the handler on the next reload.
    (plugins / "weather.py").unlink()
    assert pl.reload()["queries"]["removed"] == ["weather.now"]
