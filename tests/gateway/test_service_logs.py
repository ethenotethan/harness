"""Service logs: declared and derived sinks by graph id, enforcement, bounded
tails, follow (tui_gateway/service_logs.py + methods_service.py)."""
import json
import logging
import os
import plistlib

import pytest

import tui_gateway.methods_service as ms
from tests.gateway.architecture_fixtures import local_manifest, minimal_document, write_manifest
from tui_gateway import architecture_store as store
from tui_gateway import service_logs as logs


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def plists(tmp_path, monkeypatch):
    """A private launchd plist directory the resolver searches instead of the real ones."""
    folder = tmp_path / "LaunchAgents"
    folder.mkdir()
    monkeypatch.setattr(logs, "_LAUNCHD_DIRS", (str(folder),))
    return folder


@pytest.fixture(autouse=True)
def _no_followers():
    logs.stop_all("test")
    yield
    logs.stop_all("test")


def _write_plist(folder, label, out=None, err=None):
    doc = {"Label": label}
    if out:
        doc["StandardOutPath"] = out
    if err:
        doc["StandardErrorPath"] = err
    with open(folder / f"{label}.plist", "wb") as handle:
        plistlib.dump(doc, handle)


def _service(tmp_path, service_id="demo", **manifest_extra):
    root = tmp_path / service_id
    (root / "architecture" / "model").mkdir(parents=True, exist_ok=True)
    (root / "architecture" / "model" / "model.json").write_text(json.dumps(minimal_document()), encoding="utf-8")
    manifest = local_manifest(root, name=service_id.title())
    manifest.update(manifest_extra)
    write_manifest(store.manifests_dir(), service_id, manifest)
    return root, manifest


def _handlers(module=ms):
    pending = dict(module._registry._pending)
    events = []
    for fn in pending.values():
        fn.__globals__["_ok"] = lambda rid, result: {"rid": rid, "result": result}
        fn.__globals__["_err"] = lambda rid, code, msg: {"rid": rid, "error": {"code": code, "message": msg}}
        fn.__globals__["_broadcast_global_event"] = lambda event, payload=None: events.append((event, payload))
        fn.__globals__.setdefault("logger", logging.getLogger("test"))
    return pending, events


# ── Sink declaration ─────────────────────────────────────────────────────────


def test_sinks_of_every_kind_normalise(tmp_path):
    plist_dir = tmp_path / "agents"
    plist_dir.mkdir()
    _write_plist(plist_dir, "demo", out="~/logs/out.log", err="/var/log/demo.err")
    runtime = {"provider": "launchd", "id": "demo", "graph_id": "launchd:demo"}
    sinks = logs.normalize_log_sinks([
        {"id": "app", "kind": "file", "path": "~/app.log", "label": "Application"},
        {"id": "dir", "kind": "directory", "path": str(tmp_path / "logs")},
        {"id": "out", "kind": "launchd_stdout"},
        {"id": "err", "kind": "launchd_stderr"},
        {"id": "explicit", "kind": "launchd_stdout", "path": "/tmp/explicit.log"},
    ], runtime, launchd_dirs=[str(plist_dir)])
    by_id = {s["id"]: s for s in sinks}
    assert by_id["app"]["label"] == "Application" and by_id["app"]["path"].endswith("/app.log") and "~" not in by_id["app"]["path"]
    assert by_id["dir"]["label"] == "dir"
    assert by_id["out"]["path"].endswith("/logs/out.log") and "problem" not in by_id["out"]
    assert by_id["err"]["path"] == "/var/log/demo.err"
    assert by_id["explicit"]["path"] == "/tmp/explicit.log", "an explicit path wins over the plist"
    assert logs.normalize_log_sinks(None, None) == []


def test_launchd_sink_without_plist_keeps_the_manifest_but_records_the_problem(tmp_path):
    runtime = {"provider": "launchd", "id": "ghost", "graph_id": "launchd:ghost"}
    [sink] = logs.normalize_log_sinks([{"id": "out", "kind": "launchd_stdout"}], runtime, launchd_dirs=[str(tmp_path)])
    assert sink["path"] is None and "no launchd plist" in sink["problem"]
    (tmp_path / "ghost.plist").write_bytes(b"not a plist")
    [sink] = logs.normalize_log_sinks([{"id": "out", "kind": "launchd_stdout"}], runtime, launchd_dirs=[str(tmp_path)])
    assert sink["path"] is None and "unreadable" in sink["problem"]
    _write_plist(tmp_path, "quiet")
    [sink] = logs.normalize_log_sinks([{"id": "out", "kind": "launchd_stdout"}], {**runtime, "id": "quiet"}, launchd_dirs=[str(tmp_path)])
    assert "declares no StandardOutPath" in sink["problem"]


@pytest.mark.parametrize("bad, reason", [
    ("app.log", "must be a list"),
    (["app.log"], "must be an object"),
    ([{"kind": "file", "path": "/x"}], "id must be a non-empty string"),
    ([{"id": " ", "kind": "file", "path": "/x"}], "id must be a non-empty string"),
    ([{"id": "a", "kind": "file", "path": "/x"}, {"id": "a", "kind": "file", "path": "/y"}], "duplicate sink id"),
    ([{"id": "a", "kind": "journal", "path": "/x"}], "kind must be one of"),
    ([{"id": "a", "kind": "file", "path": "relative/app.log"}], "must be absolute"),
    ([{"id": "a", "kind": "file"}], "needs a path"),
    ([{"id": "a", "kind": "file", "path": ""}], "non-empty string"),
    ([{"id": "a", "kind": "file", "path": "/x", "label": 3}], "label must be a string"),
    ([{"id": "a", "kind": "launchd_stdout"}], "needs a runtime binding with provider launchd"),
])
def test_malformed_sink_declarations_are_rejected(bad, reason):
    with pytest.raises(ValueError, match=reason):
        logs.normalize_log_sinks(bad, None)
    with pytest.raises(ValueError):
        store.normalize_manifest({"name": "P", "description": "d", "root": "/tmp", "logs": bad}, "p")


def test_manifest_carries_sinks_and_github_only_manifests_drop_them(tmp_path, caplog):
    manifest = store.normalize_manifest({"name": "P", "description": "d", "root": str(tmp_path),
                                         "logs": [{"id": "app", "kind": "file", "path": str(tmp_path / "a.log")}]}, "p")
    assert manifest["logs"] == [{"id": "app", "kind": "file", "label": "app", "path": str(tmp_path / "a.log")}]
    assert store.normalize_manifest({"name": "P", "description": "d", "root": str(tmp_path)}, "p")["logs"] == []
    with caplog.at_level(logging.WARNING, logger="tui_gateway.architecture_store"):
        remote = store.normalize_manifest({"name": "P", "description": "d", "repository": "o/r",
                                           "logs": [{"id": "app", "kind": "file", "path": "/x.log"}]}, "p")
    assert remote["logs"] == [] and "sinks ignored" in caplog.text


# ── Enforcement (architecture manifest conformance) ──────────────────────────


def test_local_service_without_log_capture_is_not_conforming(home, tmp_path):
    _service(tmp_path, "quiet", logs=[])
    manifest = store.load_manifest("quiet")
    status = store.status_for(manifest)
    assert status["conforming"] is False and status["problem"] == logs.NO_CAPTURE_PROBLEM
    assert status["logs"] == []
    assert store.describe(manifest)["service"]["logs"] == [], "the model itself still serves; the annotation names the gap"


def test_declared_sink_makes_the_service_conforming_even_before_the_file_exists(home, tmp_path):
    _service(tmp_path, "fresh", logs=[{"id": "app", "kind": "file", "path": str(tmp_path / "fresh" / "not-yet.log")}])
    manifest = store.load_manifest("fresh")
    assert store.status_for(manifest)["conforming"] is True
    [sink] = store.describe(manifest)["service"]["logs"]
    assert sink["exists"] is False and sink["size_bytes"] == 0 and sink["modified_at"] is None


def test_unresolvable_launchd_sink_is_not_conforming_and_problems_join(home, tmp_path, plists):
    root, _ = _service(tmp_path, "svc", runtime={"provider": "launchd", "id": "svc"}, logs=[{"id": "out", "kind": "launchd_stdout"}])
    manifest = store.load_manifest("svc")
    status = store.status_for(manifest)
    assert status["conforming"] is False and "no log sink resolves" in status["problem"] and "no launchd plist" in status["problem"]
    (root / "architecture" / "model" / "model.json").write_text("{", encoding="utf-8")
    problem = store.status_for(manifest)["problem"]
    assert "not valid JSON" in problem and "no log sink resolves" in problem


def test_github_manifest_is_exempt_from_log_capture(home):
    manifest = store.normalize_manifest({"name": "R", "description": "d", "repository": "o/r"}, "remote")
    assert logs.capture_problem(manifest) is None


def test_describe_and_list_carry_resolved_sinks(home, tmp_path):
    import tui_gateway.methods_architecture as ma

    root, _ = _service(tmp_path)
    loaded = store.load_manifest("demo")
    [sink] = store.describe(loaded)["service"]["logs"]
    assert sink["exists"] is True and sink["size_bytes"] == len("started\n") and sink["modified_at"].endswith("Z")
    assert sink["path"] == str(root / "logs" / "app.log") and sink["kind"] == "file" and sink["label"] == "app"
    pending, _ = _handlers(ma)
    listed = pending["architecture.list"](1, {})["result"]
    assert listed["services"][0]["logs"][0]["exists"] is True
    assert listed["services"][0]["status"]["logs"] == ["app"]


def test_directory_sink_reads_the_newest_file(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    older = folder / "a.log"
    newer = folder / "b.log"
    older.write_text("old\n")
    newer.write_text("new\n")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    (folder / ".hidden").write_text("x")
    sink = {"id": "d", "kind": "directory", "path": str(folder)}
    assert logs.active_file(sink) == newer
    assert logs.resolve_sink(sink)["path"] == str(newer)
    empty = tmp_path / "empty"
    empty.mkdir()
    resolved = logs.resolve_sink({"id": "e", "kind": "directory", "path": str(empty)})
    assert resolved["exists"] is False and "no log files yet" in resolved["problem"]


# ── Resolution by graph id ───────────────────────────────────────────────────


def test_arch_service_resolves_its_declared_sinks(home, tmp_path):
    root, _ = _service(tmp_path)
    sinks = logs.service_sinks("arch:demo")
    assert [s["id"] for s in sinks] == ["app"] and sinks[0]["path"] == str(root / "logs" / "app.log")
    with pytest.raises(logs.LogError) as exc:
        logs.service_sinks("arch:nobody")
    assert exc.value.code == 4030


def test_bound_arch_service_unions_the_plist_sinks(home, tmp_path, plists):
    out = tmp_path / "out.log"
    err = tmp_path / "err.log"
    out.write_text("o\n")
    _write_plist(plists, "cam", out=str(out), err=str(err))
    _service(tmp_path, "cam", runtime={"provider": "launchd", "id": "cam"})
    sinks = logs.service_sinks("arch:cam")
    assert [(s["id"], s["kind"]) for s in sinks] == [("app", "file"), ("launchd_stdout", "launchd_stdout"), ("launchd_stderr", "launchd_stderr")]
    assert sinks[1]["path"] == str(out) and sinks[2]["path"] == str(err)
    # A declared sink with the derived id, or the same (kind, path), is not duplicated.
    _service(tmp_path, "cam2", runtime={"provider": "launchd", "id": "cam"},
             logs=[{"id": "launchd_stdout", "kind": "launchd_stdout"}, {"id": "mine", "kind": "launchd_stderr", "path": str(err)}])
    assert [s["id"] for s in logs.service_sinks("arch:cam2")] == ["launchd_stdout", "mine"]


def test_launchd_service_resolves_plist_sinks_and_unions_a_bound_manifest(home, tmp_path, plists):
    out = tmp_path / "out.log"
    out.write_text("o\n")
    _write_plist(plists, "cam", out=str(out))
    assert [(s["id"], s["kind"], s["path"]) for s in logs.service_sinks("launchd:cam")] == [("stdout", "launchd_stdout", str(out))]
    root, _ = _service(tmp_path, "cam", runtime={"provider": "launchd", "id": "cam"})
    sinks = logs.service_sinks("launchd:cam")
    assert [s["id"] for s in sinks] == ["stdout", "app"] and sinks[1]["path"] == str(root / "logs" / "app.log")
    assert logs.service_sinks("launchd:unknown") == [], "no plist and no manifest: nothing to read"
    with pytest.raises(logs.LogError) as exc:
        logs.select_sink([], None, "launchd:unknown")
    assert exc.value.code == 4040


@pytest.mark.parametrize("graph_id", ["docker:0123456789ab", "nomad:dashboard", "proc_0123456789ab"])
def test_unsupported_providers_are_an_honest_4042(home, graph_id):
    with pytest.raises(logs.LogError) as exc:
        logs.service_sinks(graph_id)
    assert exc.value.code == 4042 and logs.UNSUPPORTED_PROVIDER_PROBLEM in exc.value.message


def test_non_graph_ids_are_rejected(home):
    for bad in ("https://example", "launchd:", "demo"):
        with pytest.raises(logs.LogError) as exc:
            logs.service_sinks(bad)
        assert exc.value.code == 4001, bad


# ── Reads ────────────────────────────────────────────────────────────────────


def _lines(n, prefix="line"):
    return "".join(f"{prefix} {i}\n" for i in range(n))


def test_tail_returns_the_last_complete_lines_and_a_cursor(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(_lines(10) + "partial")
    result = logs.read_tail(path, 3)
    assert result["lines"] == ["line 7", "line 8", "line 9"]
    assert int(result["cursor"]) == len(_lines(10)) and result["truncated"] is True
    everything = logs.read_tail(path, 50)
    assert everything["lines"] == [f"line {i}" for i in range(10)] and everything["truncated"] is False
    (tmp_path / "nolines.log").write_text("no newline yet")
    assert logs.read_tail(tmp_path / "nolines.log", 5) == {"lines": [], "cursor": "0", "truncated": False, "scanned_from": 0}


def test_tail_scan_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "TAIL_SCAN_BYTES", 64)
    path = tmp_path / "big.log"
    path.write_text(_lines(200))
    result = logs.read_tail(path, 1000)
    assert result["truncated"] is True and result["lines"][-1] == "line 199"
    assert all(line.startswith("line ") for line in result["lines"]), "the half-scanned first line is dropped"
    assert int(result["cursor"]) == path.stat().st_size


def test_cursor_reads_only_what_was_appended(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(_lines(3))
    first = logs.read_tail(path, 10)
    with open(path, "a") as handle:
        handle.write("line 3\nline 4\nhalf")
    since = logs.read_since(path, int(first["cursor"]), 10)
    assert since["lines"] == ["line 3", "line 4"] and since["truncated"] is False
    assert int(since["cursor"]) == len(_lines(5))
    again = logs.read_since(path, int(since["cursor"]), 10)
    assert again["lines"] == [] and again["cursor"] == since["cursor"]
    with open(path, "a") as handle:
        handle.write("-done\n")
    assert logs.read_since(path, int(again["cursor"]), 10)["lines"] == ["half-done"]


def test_cursor_read_is_bounded_and_resumable(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(_lines(7))
    page = logs.read_since(path, 0, 3)
    assert page["lines"] == ["line 0", "line 1", "line 2"] and page["truncated"] is True
    rest = logs.read_since(path, int(page["cursor"]), 10)
    assert rest["lines"] == ["line 3", "line 4", "line 5", "line 6"] and rest["truncated"] is False


def test_rotation_restarts_from_the_tail(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(_lines(50))
    cursor = int(logs.read_tail(path, 5)["cursor"])
    path.write_text(_lines(2, "fresh"))
    result = logs.read_since(path, cursor, 5)
    assert result["rotated"] is True and result["lines"] == ["fresh 0", "fresh 1"]


def test_undecodable_bytes_are_replaced_not_fatal(tmp_path):
    path = tmp_path / "app.log"
    path.write_bytes(b"ok\n\xff\xfe bad\n")
    assert logs.read_tail(path, 5)["lines"] == ["ok", "�� bad"]


def test_param_parsing():
    assert logs.parse_lines(None) == logs.DEFAULT_LINES and logs.parse_lines(7) == 7
    for bad in (0, logs.MAX_LINES + 1, "5", True, 2.5):
        with pytest.raises(logs.LogError) as exc:
            logs.parse_lines(bad)
        assert exc.value.code == 4001
    assert logs.parse_cursor(None) is None and logs.parse_cursor("") is None and logs.parse_cursor("12") == 12 and logs.parse_cursor(3) == 3
    for bad in ("x", -1, True, 1.5):
        with pytest.raises(logs.LogError):
            logs.parse_cursor(bad)


# ── RPC ──────────────────────────────────────────────────────────────────────


def test_logs_rpc_tails_and_continues(home, tmp_path):
    root, _ = _service(tmp_path)
    (root / "logs" / "app.log").write_text(_lines(5))
    pending, _ = _handlers()
    result = pending["service.logs"](1, {"service": "arch:demo", "lines": 2})["result"]
    assert result["service"] == "arch:demo" and result["lines"] == ["line 3", "line 4"] and result["truncated"] is True
    assert result["sink"]["id"] == "app" and result["sink"]["exists"] is True and result["encoding"] == "utf-8-replace"
    assert [s["id"] for s in result["sinks"]] == ["app"]
    with open(root / "logs" / "app.log", "a") as handle:
        handle.write("line 5\n")
    more = pending["service.logs"](2, {"service": "arch:demo", "sink": "app", "cursor": result["cursor"]})["result"]
    assert more["lines"] == ["line 5"] and "rotated" not in more


def test_logs_rpc_reads_a_launchd_node(home, tmp_path, plists):
    out = tmp_path / "out.log"
    out.write_text("boot\nready\n")
    _write_plist(plists, "cam", out=str(out))
    pending, _ = _handlers()
    result = pending["service.logs"](1, {"service": "launchd:cam"})["result"]
    assert result["service"] == "launchd:cam" and result["sink"]["id"] == "stdout" and result["lines"] == ["boot", "ready"]


def test_logs_rpc_errors(home, tmp_path):
    _service(tmp_path)
    _service(tmp_path, "quiet", logs=[])
    _service(tmp_path, "fresh", logs=[{"id": "app", "kind": "file", "path": str(tmp_path / "fresh" / "nope.log")}])
    pending, _ = _handlers()
    call = pending["service.logs"]
    assert call(1, {})["error"]["code"] == 4029
    assert call(2, {"service": "arch:nobody"})["error"]["code"] == 4030
    assert call(3, {"service": "arch:demo", "sink": "ghost"})["error"]["code"] == 4040
    assert call(4, {"service": "arch:quiet"})["error"]["code"] == 4040
    assert call(5, {"service": "arch:fresh"})["error"]["code"] == 4041
    assert call(6, {"service": "arch:demo", "lines": 0})["error"]["code"] == 4001
    assert call(7, {"service": "arch:demo", "cursor": "abc"})["error"]["code"] == 4001
    assert call(8, {"service": "docker:0123456789ab"})["error"]["code"] == 4042
    assert call(9, {"service": "not-a-graph-id"})["error"]["code"] == 4001


def test_only_resolved_sinks_are_ever_read(home, tmp_path):
    _service(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope\n")
    pending, _ = _handlers()
    assert pending["service.logs"](1, {"service": "arch:demo", "sink": str(secret)})["error"]["code"] == 4040


# ── Follow ───────────────────────────────────────────────────────────────────


def test_follow_broadcasts_appended_lines_and_stops(home, tmp_path):
    root, _ = _service(tmp_path)
    sinks = logs.service_sinks("arch:demo")
    events = []
    clock = {"now": 100.0}
    follower = logs.start_follow(sinks, "arch:demo", None, lambda e, p: events.append((e, p)), spawn=False, clock=lambda: clock["now"])
    assert follower.cursor == (root / "logs" / "app.log").stat().st_size, "following starts at the current end"
    assert follower.poll_once() is None and events == []
    with open(root / "logs" / "app.log", "a") as handle:
        handle.write("one\ntwo\npartial")
    payload = follower.poll_once()
    assert payload["lines"] == ["one", "two"] and payload["service"] == "arch:demo" and payload["sink"] == "app"
    assert events == [("service.log", payload)]
    assert follower.poll_once() is None, "a partial line waits for its newline"
    same = logs.start_follow(sinks, "arch:demo", "app", lambda e, p: None, spawn=False)
    assert same is follower and logs.following("arch:demo", "app") is follower
    (root / "logs" / "app.log").write_text("fresh\n")
    rotated = follower.poll_once()
    assert rotated["rotated"] is True and rotated["lines"] == ["fresh"]
    assert logs.stop_follow("arch:demo", "app") is True
    assert events[-1][1]["stopped"] == "stopped" and logs.following("arch:demo", "app") is None
    assert logs.stop_follow("arch:demo", "app") is False


def test_follow_idle_timeout(home, tmp_path):
    _service(tmp_path)
    sinks = logs.service_sinks("arch:demo")
    events = []
    clock = {"now": 0.0}
    follower = logs.start_follow(sinks, "arch:demo", None, lambda e, p: events.append((e, p)), spawn=False, clock=lambda: clock["now"])
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += logs.FOLLOW_IDLE_S / 2 + 1

    follower.run(interval=1.0, sleep=sleep)
    assert follower.stop_requested.is_set() and events[-1][1]["stopped"] == "idle-timeout"
    assert len(sleeps) == 2 and logs.following("arch:demo", "app") is None
    clock["now"] = 0.0
    follower = logs.start_follow(sinks, "arch:demo", None, lambda e, p: None, spawn=False, clock=lambda: clock["now"])
    clock["now"] = logs.FOLLOW_IDLE_S - 1
    follower.touch()
    assert follower.idle_for() == 0.0


def test_follow_rpc(home, tmp_path):
    root, _ = _service(tmp_path)
    _service(tmp_path, "fresh", logs=[{"id": "app", "kind": "file", "path": str(tmp_path / "fresh" / "nope.log")}])
    pending, events = _handlers()
    follow = pending["service.logs.follow"]
    assert follow(1, {})["error"]["code"] == 4029
    assert follow(2, {"service": "arch:nobody"})["error"]["code"] == 4030
    assert follow(3, {"service": "arch:demo", "enabled": "yes"})["error"]["code"] == 4001
    assert follow(4, {"service": "arch:demo", "sink": "ghost"})["error"]["code"] == 4040
    assert follow(5, {"service": "arch:fresh"})["error"]["code"] == 4041
    assert follow(6, {"service": "nomad:x"})["error"]["code"] == 4042
    started = follow(7, {"service": "arch:demo"})["result"]
    assert started == {"service": "arch:demo", "following": True, "sink": "app", "cursor": str((root / "logs" / "app.log").stat().st_size)}
    follower = logs.following("arch:demo", "app")
    assert follower is not None and follower.thread is not None and follower.thread.daemon
    with open(root / "logs" / "app.log", "a") as handle:
        handle.write("hello\n")
    assert follower.poll_once()["lines"] == ["hello"]
    assert events[-1] == ("service.log", {"service": "arch:demo", "sink": "app", "lines": ["hello"], "cursor": str((root / "logs" / "app.log").stat().st_size)})
    stopped = follow(8, {"service": "arch:demo", "enabled": False})["result"]
    assert stopped == {"service": "arch:demo", "following": False, "sink": "app", "stopped": True}
    assert events[-1][1]["stopped"] == "stopped"
    assert follow(9, {"service": "arch:demo", "enabled": False})["result"]["stopped"] is False


def test_installed_handlers_resolve_every_name_at_runtime(home, tmp_path):
    import types

    _service(tmp_path)
    fake = types.ModuleType("fake_server")
    fake._methods = {}
    fake._ok = lambda rid, result: {"rid": rid, "result": result}
    fake._err = lambda rid, code, msg: {"rid": rid, "error": {"code": code, "message": msg}}
    fake._broadcast_global_event = lambda event, payload=None: None
    fake._profile_scoped = lambda fn: fn
    fake.logger = logging.getLogger("fake")
    ms.register(fake)
    assert set(fake._methods) == {"service.logs", "service.logs.follow"}
    assert fake._methods["service.logs"](1, {"service": "arch:demo"})["result"]["lines"] == ["started"]
    assert fake._methods["service.logs.follow"](2, {"service": "arch:demo"})["result"]["following"] is True
    assert fake._methods["service.logs.follow"](3, {"service": "arch:demo", "enabled": False})["result"]["stopped"] is True
    server = open("tui_gateway/server.py", encoding="utf-8").read()
    assert "methods_service as _methods_service" in server and "_methods_service," in server
    assert '"service.logs",' in server and '"service.logs.follow",' in server, "both run on the RPC pool"


def test_capabilities_advertise_service_logs():
    import tui_gateway.methods_harness as mh

    pending = dict(mh._registry._pending)
    fn = pending["gateway.capabilities"]
    fn.__globals__["_ok"] = lambda rid, result: {"result": result}
    fn.__globals__.setdefault("logger", logging.getLogger("test"))
    result = fn(1, {})["result"]
    assert result["service"] == {"methods": ["service.logs", "service.logs.follow"], "events": ["service.log"]}
    assert "service.logs" in result["capability_names"] and "service.logs.follow" in result["capability_names"]
    assert result["architecture"]["methods"] == [
        "architecture.list", "architecture.describe", "architecture.check", "architecture.history", "architecture.diff",
    ]
    assert result["architecture"]["events"] == ["architecture.changed"]
