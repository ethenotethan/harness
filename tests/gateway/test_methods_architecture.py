"""architecture.* handlers, called directly with a fake rid (see test_cron_dataflow.py)."""
import json
import logging

import pytest

import tui_gateway.methods_architecture as ma
from tui_gateway import architecture_store as store


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def handlers():
    """The registered handlers with `_ok`/`_err`/`_broadcast_global_event` stubbed."""
    pending = dict(ma._registry._pending)
    events = []

    def _ok(rid, result):
        return {"rid": rid, "result": result}

    def _err(rid, code, msg):
        return {"rid": rid, "error": {"code": code, "message": msg}}

    def _broadcast_global_event(event, payload=None):
        events.append((event, payload))

    for fn in pending.values():
        fn.__globals__["_ok"] = _ok
        fn.__globals__["_err"] = _err
        fn.__globals__["_broadcast_global_event"] = _broadcast_global_event
        fn.__globals__.setdefault("logger", logging.getLogger("test"))
    return pending, events


def _write_service(tmp_path, check=None):
    root = tmp_path / "svc"
    (root / "architecture" / "model").mkdir(parents=True)
    (root / "architecture" / "model" / "model.json").write_text(json.dumps({
        "schema_version": "1.0.0", "title": "Demo", "components": [{"id": "a"}],
        "interplay": {"nodes": [], "edges": [], "invariants": []},
    }), encoding="utf-8")
    manifest = {"name": "Demo", "description": "A demo.", "root": str(root)}
    if check:
        manifest["check"] = check
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / "demo.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_installed_handlers_resolve_every_name_at_runtime(home, tmp_path):
    """HandlerRegistry.install rebinds each handler onto the *server's* globals, so a
    helper defined in this module is not visible at runtime (the bug behind
    "name '_service_param' is not defined"). Install onto a fake server that has
    exactly the globals server.py provides and call the installed handlers."""
    import types

    _write_service(tmp_path, check=["python3", "-c", "print('ok')"])
    fake = types.ModuleType("fake_server")
    events = []
    fake._methods = {}
    fake._ok = lambda rid, result: {"rid": rid, "result": result}
    fake._err = lambda rid, code, msg: {"rid": rid, "error": {"code": code, "message": msg}}
    fake._broadcast_global_event = lambda event, payload=None: events.append((event, payload))
    fake._profile_scoped = lambda fn: fn
    fake.logger = logging.getLogger("fake")
    ma.register(fake)
    assert set(fake._methods) == {"architecture.list", "architecture.describe", "architecture.check", "architecture.history"}
    listed = fake._methods["architecture.list"](1, {})
    assert listed["result"]["services"][0]["id"] == "arch:demo"
    described = fake._methods["architecture.describe"](2, {"service": "arch:demo"})
    assert described["result"]["model"]["title"] == "Demo"
    assert fake._methods["architecture.describe"](3, {})["error"]["code"] == 4029
    checked = fake._methods["architecture.check"](4, {"service": "demo"})
    assert checked["result"]["check"]["status"] == "passed"
    history = fake._methods["architecture.history"](5, {"service": "arch:demo"})
    assert history["result"]["latest"] == described["result"]["revision"]
    assert [e[1]["reason"] for e in events] == ["snapshot", "check"]


def test_registry_names_match_docs_and_capabilities():
    names = {name for name, _ in ma._registry._pending}
    assert names == {"architecture.list", "architecture.describe", "architecture.check", "architecture.history"}
    capabilities = open("tui_gateway/methods_harness.py", encoding="utf-8").read()
    for name in names:
        assert f'"{name}"' in capabilities, f"{name} not advertised by gateway.capabilities"
    server = open("tui_gateway/server.py", encoding="utf-8").read()
    for name in names:
        assert f'"{name}",' in server, f"{name} is not pool-routed (_LONG_HANDLERS)"
    assert "methods_architecture as _methods_architecture" in server and "_methods_architecture," in server


def test_list_describe_history_and_check(home, tmp_path, handlers):
    pending, events = handlers
    _write_service(tmp_path, check=["python3", "-c", "print('fine')"])

    listed = pending["architecture.list"](1, {})
    assert listed["rid"] == 1
    (service,) = listed["result"]["services"]
    assert service["id"] == "arch:demo" and service["source"] == "local" and service["check_configured"] is True
    assert service["status"]["ref"] == "arch:demo" and service["status"]["snapshots"] == 0

    described = pending["architecture.describe"](2, {"service": "arch:demo"})
    document = described["result"]
    assert document["service"]["id"] == "arch:demo" and document["model"]["title"] == "Demo"
    assert document["summary"]["components"] == 1
    assert events == [("architecture.changed", {"service": "arch:demo", "revision": document["revision"], "source": "local", "reason": "snapshot"})]

    # The same revision again: no second announcement, same document.
    again = pending["architecture.describe"](3, {"service": "demo"})
    assert again["result"]["revision"] == document["revision"] and len(events) == 1

    stored = pending["architecture.describe"](4, {"service": "arch:demo", "revision": document["revision"]})
    assert stored["result"]["model"]["title"] == "Demo" and len(events) == 1
    missing = pending["architecture.describe"](5, {"service": "arch:demo", "revision": "nope"})
    assert missing["error"]["code"] == 4404

    checked = pending["architecture.check"](6, {"service": "arch:demo"})
    assert checked["result"]["check"]["status"] == "passed" and "fine" in checked["result"]["check"]["output"]
    assert events[-1][0] == "architecture.changed" and events[-1][1]["reason"] == "check" and events[-1][1]["status"] == "passed"

    history = pending["architecture.history"](7, {"service": "arch:demo"})
    result = history["result"]
    assert result["latest"] == document["revision"]
    assert [r["revision"] for r in result["revisions"]] == [document["revision"]]
    assert result["checks"][0]["status"] == "passed"


def test_parameter_and_lookup_errors(home, tmp_path, handlers):
    pending, events = handlers
    for name in ("architecture.describe", "architecture.check", "architecture.history"):
        assert pending[name](1, {})["error"]["code"] == 4029
        assert pending[name](1, {"service": "   "})["error"]["code"] == 4029
        assert pending[name](1, {"service": "arch:nobody"})["error"]["code"] == 4030
    assert pending["architecture.list"](2, {})["result"] == {"services": []}
    _write_service(tmp_path)
    (tmp_path / "svc" / "architecture" / "model" / "model.json").unlink()
    unavailable = pending["architecture.describe"](3, {"service": "arch:demo"})
    assert unavailable["error"]["code"] == 4032
    unconfigured = pending["architecture.check"](4, {"service": "arch:demo"})
    assert unconfigured["result"]["check"]["status"] == "unavailable"
    assert events == []
