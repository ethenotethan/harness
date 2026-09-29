"""/v1/ws dispatches JSON-RPC requests concurrently per connection.

Portal (the macOS client) runs everything over one WebSocket. The handler used
to ``await`` each dispatch inline in the read loop, so a 20 s ``session.list``
held up every request behind it — and the loop stopped reading frames while
the handler ran. These tests pin the fan-out: fast requests overtake slow ones,
the per-connection semaphore bounds concurrency, a handler's events still
precede its own response, and a mid-request disconnect tears down cleanly.
"""
import asyncio
import json
import logging
import threading
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.platforms.api_server as api_server_mod
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


class FakeTuiServer:
    """Stand-in for ``tui_gateway.server`` with the surface ``_handle_ws`` uses."""

    def __init__(self, slow: float = 1.0) -> None:
        self.slow = slow
        self.running = 0
        self.max_running = 0
        self.calls = 0
        self._lock = threading.Lock()
        self.registered = []
        self.unregistered = []
        self.started = threading.Event()
        self.finished = threading.Event()
        self._sessions = {}
        self._stdio_transport = object()

    def register_live_transport(self, transport) -> None:
        self.registered.append(transport)

    def unregister_live_transport(self, transport) -> None:
        self.unregistered.append(transport)

    def dispatch(self, req, transport):
        method = req.get("method")
        rid = req.get("id")
        with self._lock:
            self.calls += 1
        if method == "slow":
            with self._lock:
                self.running += 1
                self.max_running = max(self.max_running, self.running)
            self.started.set()
            try:
                time.sleep(self.slow)
            finally:
                with self._lock:
                    self.running -= 1
                self.finished.set()
            return {"jsonrpc": "2.0", "result": {"method": "slow"}, "id": rid}
        if method == "fast":
            return {"jsonrpc": "2.0", "result": {"method": "fast"}, "id": rid}
        if method == "emit":
            # Handlers write events from the worker thread through the bound
            # transport; the response is the dispatch return value.
            ok = transport.write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {"type": "test.event", "payload": {"for": rid}},
                }
            )
            return {"jsonrpc": "2.0", "result": {"event_written": ok}, "id": rid}
        if method == "long":
            # A _LONG_HANDLERS-style method: writes its own response, returns None.
            transport.write({"jsonrpc": "2.0", "result": {"method": "long"}, "id": rid})
            return None
        if method == "boom":
            raise RuntimeError("handler exploded")
        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": "no such method"}, "id": rid}


def _app() -> web.Application:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    app = web.Application()
    app.router.add_get("/v1/ws", adapter._handle_ws)
    return app


async def _recv(ws, timeout: float = 5.0) -> dict:
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    assert msg.type.name == "TEXT", msg
    return json.loads(msg.data)


async def _open(cli):
    ws = await cli.ws_connect("/v1/ws")
    ready = await _recv(ws)
    assert ready["params"]["type"] == "gateway.ready"
    return ws


def _rpc(method: str, rid) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": {}, "id": rid})


@pytest.fixture
def fake(monkeypatch):
    server = FakeTuiServer()
    monkeypatch.setattr(api_server_mod, "_tui_server", server)
    return server


@pytest.mark.asyncio
async def test_fast_request_overtakes_slow_one_on_same_connection(fake):
    fake.slow = 1.0
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        await ws.send_str(_rpc("slow", "slow-1"))
        await ws.send_str(_rpc("fast", "fast-2"))
        first = await _recv(ws)
        second = await _recv(ws)
        await ws.close()
    assert first["id"] == "fast-2" and first["result"]["method"] == "fast"
    assert second["id"] == "slow-1" and second["result"]["method"] == "slow"


@pytest.mark.asyncio
async def test_semaphore_bounds_inflight_requests(fake, monkeypatch):
    n = 2
    monkeypatch.setenv("HERMES_WS_MAX_INFLIGHT", str(n))
    fake.slow = 0.3
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        ids = [f"s{i}" for i in range(n + 2)]
        for rid in ids:
            await ws.send_str(_rpc("slow", rid))
        got = {(await _recv(ws))["id"] for _ in ids}
        await ws.close()
    assert got == set(ids)
    assert fake.max_running == n, "expected exactly N handlers running at once"


@pytest.mark.asyncio
async def test_invalid_max_inflight_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_WS_MAX_INFLIGHT", "not-a-number")
    assert api_server_mod._ws_max_inflight() == api_server_mod._WS_MAX_INFLIGHT_DEFAULT
    monkeypatch.setenv("HERMES_WS_MAX_INFLIGHT", "0")
    assert api_server_mod._ws_max_inflight() == 1
    monkeypatch.delenv("HERMES_WS_MAX_INFLIGHT")
    assert api_server_mod._ws_max_inflight() == 8


@pytest.mark.asyncio
async def test_handler_event_precedes_its_own_response(fake):
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        for i in range(5):
            await ws.send_str(_rpc("emit", f"e{i}"))
        frames = [await _recv(ws) for _ in range(10)]
        await ws.close()
    seen_event_for = set()
    for frame in frames:
        if frame.get("method") == "event":
            seen_event_for.add(frame["params"]["payload"]["for"])
        else:
            assert frame["id"] in seen_event_for, f"response {frame['id']} arrived before its event"
            assert frame["result"]["event_written"] is True
    assert len(seen_event_for) == 5


@pytest.mark.asyncio
async def test_long_handler_style_none_response_is_not_double_sent(fake):
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        await ws.send_str(_rpc("long", "l1"))
        await ws.send_str(_rpc("fast", "f1"))
        frames = [await _recv(ws) for _ in range(2)]
        # Nothing else should follow.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive(), timeout=0.3)
        await ws.close()
    assert {f["id"] for f in frames} == {"l1", "f1"}


@pytest.mark.asyncio
async def test_client_disconnect_mid_request_tears_down_cleanly(fake, caplog):
    fake.slow = 0.6
    caplog.set_level(logging.WARNING)
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        await ws.send_str(_rpc("slow", "orphan"))
        assert await asyncio.to_thread(fake.started.wait, 3.0)
        await ws.close()
        # The handler unwinds as soon as the close frame is read; the thread
        # work finishes later and its response is dropped.
        for _ in range(50):
            if fake.unregistered:
                break
            await asyncio.sleep(0.05)
        assert fake.unregistered == fake.registered and len(fake.registered) == 1
        assert fake.unregistered[0]._closed is True
        assert await asyncio.to_thread(fake.finished.wait, 3.0)
        await asyncio.sleep(0.05)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_handler_exception_answers_that_request_only(fake):
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        await ws.send_str(_rpc("boom", "b1"))
        await ws.send_str(_rpc("fast", "f1"))
        frames = {}
        for _ in range(2):
            frame = await _recv(ws)
            frames[frame["id"]] = frame
        await ws.close()
    assert frames["b1"]["error"]["code"] == -32603
    assert "handler exploded" in frames["b1"]["error"]["message"]
    assert frames["f1"]["result"]["method"] == "fast"


@pytest.mark.asyncio
async def test_parse_error_and_unavailable_gateway_keep_their_shape(fake, monkeypatch):
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        await ws.send_str("{not json")
        err = await _recv(ws)
        assert err == {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}, "id": None}
        await ws.close()

    monkeypatch.setattr(api_server_mod, "_tui_server", None)
    async with TestClient(TestServer(_app())) as cli:
        ws = await _open(cli)
        await ws.send_str(_rpc("fast", "x1"))
        err = await _recv(ws)
        assert err["id"] == "x1"
        assert err["error"] == {"code": -32603, "message": "tui gateway unavailable"}
        await ws.close()
