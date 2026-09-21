"""/v1/ws peers must be live transports for the duration of the socket.

Regression: the API server's WebSocket handler built its own aiohttp transport
and never registered it with ``tui_gateway.server``'s live-transport registry
(``tui_gateway.ws`` did). Session-less pushes from background threads — the
artifact-query poller's ``artifact.query.changed``, ``skin.changed`` — are
delivered only through that registry, so every Portal connected via the API
server silently missed them while direct RPC replies kept working.
"""
import asyncio
import json
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

KEY = "sk-test-ws"


def _ws_app() -> web.Application:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_get("/v1/ws", adapter._handle_ws)
    return app


def _frames(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def _receive_event(ws, kind: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, f"no {kind} frame within {timeout}s"
        for frame in _frames(await asyncio.wait_for(ws.receive_str(), remaining)):
            params = frame.get("params") or {}
            if frame.get("method") == "event" and params.get("type") == kind:
                return frame


@pytest.mark.asyncio
async def test_ws_peer_is_live_for_the_socket_lifetime(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from tui_gateway import server as tui

    before = set(tui._live_transports)
    async with TestClient(TestServer(_ws_app())) as cli:
        async with cli.ws_connect("/v1/ws", headers={"Authorization": f"Bearer {KEY}"}) as ws:
            await _receive_event(ws, "gateway.ready")
            # Registered immediately after the handshake, not lazily on first RPC.
            assert len(set(tui._live_transports) - before) == 1

            # What the query poller does: a bare thread, no request context.
            payload = {"artifact_id": "dash", "query_id": "rows", "etag": "e1", "status": "ok"}
            worker = threading.Thread(
                target=tui._broadcast_global_event, args=("artifact.query.changed", payload),
                name="artifact-query-poller-test",
            )
            worker.start()
            await asyncio.get_running_loop().run_in_executor(None, worker.join)

            frame = await _receive_event(ws, "artifact.query.changed")
            assert frame["params"]["payload"] == payload
            assert frame["params"]["session_id"] == ""

        # Socket closed → unregistered in the handler's finally.
        for _ in range(100):
            if not (set(tui._live_transports) - before):
                break
            await asyncio.sleep(0.02)
        assert not (set(tui._live_transports) - before)
