"""Revision history and structural diffs: architecture.history joins snapshots to
git and marks the deployed revision; architecture.diff compares two snapshots by
stable identity (tui_gateway/architecture_store.py, methods_architecture.py)."""
import copy
import json
import logging

import pytest

from tests.gateway.architecture_fixtures import minimal_document
from tui_gateway import architecture_store as store
import tui_gateway.methods_architecture as ma


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def handlers():
    pending = dict(ma._registry._pending)
    events = []
    for fn in pending.values():
        fn.__globals__["_ok"] = lambda rid, result: {"rid": rid, "result": result}
        fn.__globals__["_err"] = lambda rid, code, msg: {"rid": rid, "error": {"code": code, "message": msg}}
        fn.__globals__["_broadcast_global_event"] = lambda event, payload=None: events.append((event, payload))
        fn.__globals__.setdefault("logger", logging.getLogger("test"))
    return pending, events


def _write_manifest(tmp_path, stem="demo", **extra):
    root = tmp_path / stem
    (root / "architecture" / "model").mkdir(parents=True, exist_ok=True)
    (root / "architecture" / "model" / "model.json").write_text(json.dumps(minimal_document()), encoding="utf-8")
    manifest = {"name": stem.title(), "description": f"The {stem} service.", "root": str(root)}
    manifest.update(extra)
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / f"{stem}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _fake_git(head=SHA_B, log=None, numstat=""):
    """A git runner: rev-parse → head; log -1 <rev> → one line; log range → the
    configured lines; diff --numstat → the configured text. Records calls."""
    calls = []

    def run(root, *args):
        calls.append(args)
        if args[:2] == ("rev-parse", "HEAD"):
            return head
        if args[0] == "log" and args[1] == "-1":
            rev = args[-1]
            return f"{rev}\x1fAda\x1f2026-09-25T10:00:00+00:00\x1fsubject for {rev[:4]}\n"
        if args[0] == "log":
            return log or ""
        if args[0] == "diff":
            return numstat
        return ""

    run.calls = calls
    return run


# ── diff_models: pure, identity-aware ───────────────────────────────────────


def test_diff_models_reports_changes_by_stable_identity():
    before = minimal_document()
    after = copy.deepcopy(before)
    # Rename a node (new id, same history_key): not churn.
    after["interplay"]["nodes"][0]["id"] = "n1-renamed"
    after["interplay"]["edges"][0]["source"] = "n1-renamed"
    after["interplay"]["flows"][0]["steps"][0]["from"] = "store:a:Alpha"
    # Add a node and an edge to it; remove nothing yet.
    after["interplay"]["nodes"].append({"id": "n3", "kind": "caller", "label": "Gamma", "component": "a", "path": "src/a.py", "line": 5, "history_key": "caller:a:Gamma"})
    after["interplay"]["edges"].append({"source": "n3", "target": "n2", "relation": "invokes", "class": "interplay"})
    # Flip an invariant, add one, drop none.
    after["interplay"]["invariants"][1]["status"] = "holds"
    after["interplay"]["invariants"].append({"id": "three", "kind": "x", "status": "holds", "why": "w", "checked": 1})
    # A file grows, one appears, one disappears.
    after["extraction"]["files"][0]["line_count"] = 42
    after["extraction"]["files"].append({"path": "src/new.py", "component": "a", "line_count": 3, "citations": 0, "semantic_citations": 0, "touched": False,
                                         "declaration_count": 0, "mapped_declarations": 0, "passes": [], "declarations": []})
    after["extraction"]["files"] = [f for f in after["extraction"]["files"] if f["path"] != "src/untouched.py"]
    # A gate job appears.
    after["ci"]["jobs"].append({"id": "local/lint", "name": "Lint", "workflow": after["ci"]["workflows"][0]["id"], "family": "posture", "role": "local", "needs": []})

    result = store.diff_models(before, after)
    assert [n["history_key"] for n in result["nodes"]["added"]] == ["caller:a:Gamma"]
    assert result["nodes"]["added"][0]["label"] == "Gamma" and result["nodes"]["added"][0]["kind"] == "caller"
    assert result["nodes"]["removed"] == [], "a rename that keeps the history key is not churn"
    assert result["edges"]["added"] == [{"source": "caller:a:Gamma", "target": "endpoint:b:Beta", "relation": "invokes", "class": "interplay"}]
    assert result["edges"]["removed"] == [], "the renamed node's edge resolves to the same keys"
    assert result["invariants"] == {"added": ["three"], "removed": [], "changed": [{"id": "two", "from": "violated", "to": "holds"}]}
    assert result["files"] == {"added": ["src/new.py"], "removed": ["src/untouched.py"], "changed": [{"path": "src/a.py", "lines_from": 10, "lines_to": 42}]}
    assert result["gates"]["jobs_added"] == ["local/lint"] and result["gates"]["jobs_removed"] == []
    assert result["gates"]["ratchets_added"] == [] and result["gates"]["ratchets_removed"] == []
    assert result["summary"]["from"]["nodes"] == 2 and result["summary"]["to"]["nodes"] == 3
    # Symmetric: swapping the arguments swaps added and removed.
    reverse = store.diff_models(after, before)
    assert [n["history_key"] for n in reverse["nodes"]["removed"]] == ["caller:a:Gamma"]
    assert reverse["invariants"]["changed"] == [{"id": "two", "from": "holds", "to": "violated"}]
    assert reverse["files"]["removed"] == ["src/new.py"]


def test_diff_models_of_identical_documents_is_empty():
    document = minimal_document()
    result = store.diff_models(document, copy.deepcopy(document))
    assert result["nodes"] == {"added": [], "removed": []}
    assert result["edges"] == {"added": [], "removed": []}
    assert result["invariants"] == {"added": [], "removed": [], "changed": []}
    assert result["files"] == {"added": [], "removed": [], "changed": []}
    assert all(value == [] for value in result["gates"].values())


# ── git helpers ──────────────────────────────────────────────────────────────


def test_commit_helpers_are_fail_open_and_bounded():
    assert store.commit_info(None, SHA_A) is None
    assert store.commit_info("/r", "working-tree", _fake_git()) is None
    assert store.commit_info("/r", "sha256:abcd", _fake_git()) is None
    assert store.commit_info("/r", SHA_A, _fake_git()) == {"sha": SHA_A, "author": "Ada", "date": "2026-09-25T10:00:00+00:00", "subject": f"subject for {SHA_A[:4]}"}
    assert store.commit_info("/r", SHA_A, lambda root, *a: "") is None, "git failure is not an error"
    assert store.commits_between("/r", None, SHA_A, _fake_git()) is None
    assert store.commits_between("/r", "working-tree", SHA_A, _fake_git()) is None
    lines = "".join(f"{i:040x}\x1fAda\x1f2026-09-25T10:{i:02d}:00+00:00\x1fc{i}\n" for i in range(60))
    since = store.commits_between("/r", SHA_A, SHA_B, _fake_git(log=lines))
    assert len(since) == 60 and since[0]["subject"] == "c0" and since[-1]["subject"] == "c59", "the runner is asked for at most 50; ordering is git's (oldest first)"
    git = _fake_git(log="")
    store.commits_between("/r", SHA_A, SHA_B, git)
    assert git.calls[-1] == ("log", "--reverse", f"--max-count={store.MAX_HISTORY_COMMITS}", f"--format={store._GIT_LOG_FORMAT}", f"{SHA_A}..{SHA_B}")
    stat = store.git_numstat("/r", SHA_A, SHA_B, _fake_git(numstat="3\t1\tsrc/a.py\n-\t-\tlogo.png\nbad line\n"))
    assert stat == {"stat": [{"path": "src/a.py", "additions": 3, "deletions": 1}, {"path": "logo.png", "additions": None, "deletions": None}], "truncated": False}
    big = "\n".join(f"1\t1\tf{i}.py" for i in range(store.MAX_DIFF_STAT + 5))
    stat = store.git_numstat("/r", SHA_A, SHA_B, _fake_git(numstat=big))
    assert len(stat["stat"]) == store.MAX_DIFF_STAT and stat["truncated"] is True


def test_runtime_info_reports_launchd_pid_fail_open():
    assert store.runtime_info({"name": "x"}) is None
    manifest = {"runtime": {"provider": "launchd", "id": "ai.demo", "graph_id": "launchd:ai.demo"}}
    info = store.runtime_info(manifest, runner=lambda args: "{\n\tstate = running\n\tpid = 4242\n}\n")
    assert info == {"provider": "launchd", "graph_id": "launchd:ai.demo", "pid": 4242}

    def boom(args):
        raise RuntimeError("launchctl unavailable")

    assert store.runtime_info(manifest, runner=boom) == {"provider": "launchd", "graph_id": "launchd:ai.demo"}
    docker = {"runtime": {"provider": "docker", "id": "0123456789ab", "graph_id": "docker:0123456789ab"}}
    assert store.runtime_info(docker, runner=boom) == {"provider": "docker", "graph_id": "docker:0123456789ab"}


# ── history() ────────────────────────────────────────────────────────────────


def _store_three(tmp_path):
    root = _write_manifest(tmp_path)
    manifest = store.load_manifest("demo")
    for revision in (SHA_A, SHA_B, SHA_C):
        model = minimal_document()
        model["title"] = f"Demo@{revision[:4]}"
        store.store_snapshot("demo", revision, model, "local")
    return root, manifest


def test_history_joins_snapshots_to_git_and_marks_the_deployed_revision(home, tmp_path):
    _root, manifest = _store_three(tmp_path)
    git = _fake_git(head=SHA_B, log=f"{'d' * 40}\x1fBob\x1f2026-09-25T11:00:00+00:00\x1fbetween\n")
    result = store.history(manifest, git=git)
    assert result["service"] == "arch:demo" and result["latest"] == SHA_C and result["head"] == SHA_B
    revisions = result["revisions"]
    assert [r["revision"] for r in revisions] == [SHA_A, SHA_B, SHA_C], "genesis first, newest last"
    assert [r["deployed"] for r in revisions] == [False, True, False], "deployed = the snapshot at the checkout's HEAD"
    assert revisions[1]["deployed_at"] == revisions[1]["stored_at"] and "deployed_at" not in revisions[0]
    assert revisions[0]["commit"]["sha"] == SHA_A and revisions[2]["commit"]["subject"] == f"subject for {SHA_C[:4]}"
    assert "commits_since_previous" not in revisions[0], "genesis has no previous snapshot"
    assert revisions[1]["commits_since_previous"] == [{"sha": "d" * 40, "author": "Bob", "date": "2026-09-25T11:00:00+00:00", "subject": "between"}]
    assert "runtime" not in result and result["checks"] == []
    # Existing fields survive.
    assert set(revisions[0]) >= {"revision", "source", "stored_at", "summary", "contract"}


def test_history_without_git_and_for_github_services(home, tmp_path):
    _root, manifest = _store_three(tmp_path)
    store.store_snapshot("demo", "working-tree", minimal_document(), "local")
    result = store.history(manifest, git=lambda root, *a: "")
    # No git: the checkout's revision is the model's content digest, which no
    # stored git revision equals, so nothing is marked deployed.
    assert str(result["head"]).startswith("sha256:")
    assert all(r["deployed"] is False for r in result["revisions"])
    assert all("commit" not in r for r in result["revisions"])
    assert "commits_since_previous" not in result["revisions"][-1], "a working-tree revision is not a commit range end"
    store.manifests_dir().joinpath("remote.json").write_text(json.dumps({
        "name": "Remote", "description": "d", "repository": "o/r", "ref": "main", "model": "architecture/model/model.json",
    }), encoding="utf-8")
    remote = store.load_manifest("remote")
    store.store_snapshot("remote", "v1", minimal_document(), "github")
    result = store.history(remote, git=_fake_git())
    assert result["source"] == "github" and result["head"] is None
    assert result["revisions"][0]["deployed"] is False and "commit" not in result["revisions"][0]


def test_history_carries_runtime_when_bound(home, tmp_path):
    _write_manifest(tmp_path, runtime={"provider": "launchd", "id": "ai.demo"})
    manifest = store.load_manifest("demo")
    result = store.history(manifest, git=_fake_git(), runtime_runner=lambda args: "pid = 7\n")
    assert result["runtime"] == {"provider": "launchd", "graph_id": "launchd:ai.demo", "pid": 7}


# ── diff() and the handlers ──────────────────────────────────────────────────


def test_diff_defaults_and_git_block(home, tmp_path):
    _root, manifest = _store_three(tmp_path)
    git = _fake_git(log=f"{'e' * 40}\x1fEve\x1f2026-09-25T12:00:00+00:00\x1fchange\n", numstat="5\t2\tsrc/b.py\n")
    result = store.diff(manifest, git=git)
    assert (result["from"], result["to"]) == (SHA_B, SHA_C), "to defaults to latest, from to the one before"
    assert result["summary"]["to"]["title"] == f"Demo@{SHA_C[:4]}" and result["summary"]["from"]["title"] == f"Demo@{SHA_B[:4]}"
    assert result["git"] == {"commits": [{"sha": "e" * 40, "author": "Eve", "date": "2026-09-25T12:00:00+00:00", "subject": "change"}],
                             "stat": [{"path": "src/b.py", "additions": 5, "deletions": 2}], "truncated": False}
    explicit = store.diff(manifest, SHA_A, SHA_C, git=git)
    assert (explicit["from"], explicit["to"]) == (SHA_A, SHA_C)
    with pytest.raises(store.ArchitectureError) as missing:
        store.diff(manifest, "nope", SHA_C, git=git)
    assert missing.value.code == 4404
    with pytest.raises(store.ArchitectureError) as genesis:
        store.diff(manifest, None, SHA_A, git=git)
    assert genesis.value.code == 4404 and "before" in genesis.value.message
    store.store_snapshot("demo", "working-tree", minimal_document(), "local")
    assert "git" not in store.diff(manifest, git=git), "no git block when an end is not a commit"


def test_handlers_history_diff_and_is_latest(home, tmp_path, handlers):
    pending, _events = handlers
    _store_three(tmp_path)
    history = pending["architecture.history"](1, {"service": "arch:demo"})["result"]
    assert history["latest"] == SHA_C and [r["revision"] for r in history["revisions"]] == [SHA_A, SHA_B, SHA_C]
    assert all("deployed" in r for r in history["revisions"]) and "checks" in history
    diff = pending["architecture.diff"](2, {"service": "demo"})["result"]
    assert (diff["from"], diff["to"]) == (SHA_B, SHA_C) and diff["nodes"] == {"added": [], "removed": []}
    assert pending["architecture.diff"](3, {"service": "demo", "from": SHA_A, "to": SHA_B})["result"]["from"] == SHA_A
    assert pending["architecture.diff"](4, {"service": "demo", "to": "nope"})["error"]["code"] == 4404
    assert pending["architecture.diff"](5, {"service": "demo", "from": "  "})["error"]["code"] == 4001
    assert pending["architecture.diff"](6, {"service": "demo", "from": 7})["error"]["code"] == 4001
    older = pending["architecture.describe"](7, {"service": "demo", "revision": SHA_A})["result"]
    assert older["is_latest"] is False and older["revision"] == SHA_A
    latest = pending["architecture.describe"](8, {"service": "demo", "revision": SHA_C})["result"]
    assert latest["is_latest"] is True


def test_capabilities_and_pool_advertise_diff():
    capabilities = open("tui_gateway/methods_harness.py", encoding="utf-8").read()
    assert capabilities.count('"architecture.diff"') == 2, "the methods list and capability_names"
    server = open("tui_gateway/server.py", encoding="utf-8").read()
    assert '"architecture.diff",' in server
    docs = open("docs/api/architecture.md", encoding="utf-8").read()
    assert "`architecture.diff`" in docs and "## Revision history" in docs
