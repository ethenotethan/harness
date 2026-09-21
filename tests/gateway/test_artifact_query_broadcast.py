"""The poller's ``artifact.query.changed`` must reach every live WebSocket peer.

Regression: ``artifact.query.subscribe`` installed the emitter as
``_emit(event, "", payload)``. ``_emit`` resolves its transport from the
request-scoped contextvar; the poller is a bare daemon thread with no request
context, so every delayed push bottomed out on the stdio transport and no
WebSocket client ever saw it. Direct RPC replies worked, subscriptions were
silently dead. The emitter has to go through the live-transport registry.
"""
import io
import json
import threading
import time

import pytest


@pytest.fixture()
def artifact_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_queries():
    from tui_gateway import artifact_queries as aq

    aq.reset_for_tests()
    aq.set_emitter(None)
    yield
    aq.reset_for_tests()
    aq.set_emitter(None)


class _RecordingTransport:
    """Stand-in for a connected WS peer: records every frame written to it."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


def _orders(rows):
    from tui_gateway import artifact_store as store

    return store.set_artifact(
        artifact_id="orders", kind="dataset", title="Orders", updated_by="test", replace=True,
        content=json.dumps({"key": "id", "rows": rows}),
    )


def _dashboard():
    from tui_gateway import artifact_store as store

    return store.set_artifact(
        artifact_id="dash", kind="html", title="Dash", updated_by="test", replace=True,
        content="<section data-hermes-query='rows'><script type='application/json' data-hermes-sink></script></section>",
        queries=[{
            "id": "rows", "query": "artifact.rows", "bind": {"source": "orders"},
            "live": {"mode": "poll", "interval_s": 5},
        }],
    )


def _pushes(peer: _RecordingTransport) -> list[dict]:
    return [
        f for f in peer.frames
        if f.get("method") == "event" and (f.get("params") or {}).get("type") == "artifact.query.changed"
    ]


def test_poller_push_reaches_every_live_ws_peer(artifact_home):
    from tui_gateway import artifact_queries as aq
    from tui_gateway import server

    _orders([{"id": "o1", "state": "open"}])
    dash = _dashboard()

    subscriber, bystander = _RecordingTransport(), _RecordingTransport()
    server.register_live_transport(subscriber)
    server.register_live_transport(bystander)
    stdio = io.StringIO()
    previous_stdout = server._real_stdout
    server._real_stdout = stdio
    try:
        # Subscribe through the real RPC handler, bound to the subscriber's
        # transport for the request — exactly what tui_gateway.ws does.
        resp = server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "artifact.query.subscribe",
             "params": {"artifact_id": "dash", "artifact_rev": dash["rev"], "query_id": "rows"}},
            subscriber,
        )
        assert resp["result"]["status"] == "ok", resp
        aq.stop_poller()

        # The data moves; the next due tick must emit once.
        _orders([{"id": "o1", "state": "open"}, {"id": "o2", "state": "open"}])
        emitted: list[dict] = []
        worker = threading.Thread(
            target=lambda: emitted.extend(aq.run_due(time.monotonic() + 10)),
            name="artifact-query-poller-test",  # no request context, like the real poller
        )
        worker.start()
        worker.join(10)
        assert len(emitted) == 1 and emitted[0]["status"] == "ok", emitted

        for peer in (subscriber, bystander):
            pushes = _pushes(peer)
            assert len(pushes) == 1, f"peer missed the push; frames={peer.frames}"
            payload = pushes[0]["params"]["payload"]
            assert payload["artifact_id"] == "dash" and payload["query_id"] == "rows"
            assert payload["etag"] == emitted[0]["etag"]
        # Nothing fell through to stdio.
        assert stdio.getvalue() == ""
    finally:
        server._real_stdout = previous_stdout
        server.unregister_live_transport(subscriber)
        server.unregister_live_transport(bystander)


def test_push_falls_back_to_stdio_when_no_ws_peer(artifact_home):
    """No registered transports (stdio TUI, tests): the event still goes somewhere."""
    from tui_gateway import artifact_queries as aq
    from tui_gateway import server

    _orders([{"id": "o1"}])
    dash = _dashboard()
    stdio = io.StringIO()
    previous_stdout = server._real_stdout
    server._real_stdout = stdio
    try:
        resp = server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "artifact.query.subscribe",
             "params": {"artifact_id": "dash", "artifact_rev": dash["rev"], "query_id": "rows"}},
            None,
        )
        assert resp["result"]["status"] == "ok", resp
        aq.stop_poller()
        _orders([{"id": "o1"}, {"id": "o2"}])
        worker = threading.Thread(target=lambda: aq.run_due(time.monotonic() + 10))
        worker.start()
        worker.join(10)
        frames = [json.loads(line) for line in stdio.getvalue().splitlines() if line.strip()]
        assert [f["params"]["type"] for f in frames] == ["artifact.query.changed"]
    finally:
        server._real_stdout = previous_stdout
