"""Architecture manifests, model reads, snapshots and checks (tui_gateway.architecture_store)."""
import json
import subprocess
from pathlib import Path

import pytest

from tests.gateway.architecture_fixtures import DIGEST, local_manifest, minimal_document
from tui_gateway import architecture_store as store


@pytest.fixture
def home(tmp_path, monkeypatch):
    # get_hermes_home() reads HERMES_HOME live — no cache to reset.
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _model(**extra):
    return minimal_document(**extra)


def _write_service(home: Path, tmp_path: Path, service_id="demo", **manifest_extra):
    root = tmp_path / "svc"
    (root / "architecture" / "model").mkdir(parents=True)
    (root / "architecture" / "model" / "model.json").write_text(json.dumps(_model()), encoding="utf-8")
    manifest = local_manifest(root)
    manifest.update(manifest_extra)
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / f"{service_id}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


# ── Manifests ────────────────────────────────────────────────────────────────


def test_normalize_manifest_defaults_and_rejections(tmp_path):
    good = store.normalize_manifest({"name": "P", "description": "d", "root": str(tmp_path)}, "p")
    assert good["id"] == "p" and good["model"] == store.DEFAULT_MODEL_PATH and good["ref"] == "main"
    assert good["check"] is None and good["source_files"] == [] and good["repository"] is None
    gh = store.normalize_manifest({"name": "P", "description": "d", "repository": "o/r", "ref": "v1.2"}, "p")
    assert gh["root"] is None and store.source_of(gh) == "github" and store.source_of(good) == "local"
    for bad, reason in (
        ({"name": "P", "description": "d"}, "root or a GitHub"),
        ({"name": "", "description": "d", "root": str(tmp_path)}, "name is required"),
        ({"name": "P", "description": " ", "root": str(tmp_path)}, "description is required"),
        ({"name": "P", "description": "d", "root": "relative/path"}, "absolute"),
        ({"name": "P", "description": "d", "repository": "not-a-repo"}, "owner/name"),
        ({"name": "P", "description": "d", "root": str(tmp_path), "model": "../x.json"}, "inside the service"),
        ({"name": "P", "description": "d", "root": str(tmp_path), "check": "make check"}, "argv list"),
        ({"id": "Other", "name": "P", "description": "d", "root": str(tmp_path)}, "must match"),
        ({"id": "other", "name": "P", "description": "d", "root": str(tmp_path)}, "file name"),
    ):
        with pytest.raises(ValueError, match=reason):
            store.normalize_manifest(bad, "p")
    with pytest.raises(ValueError, match="JSON object"):
        store.normalize_manifest([], "p")


def test_list_and_load_manifests_skip_malformed(home, tmp_path):
    _write_service(home, tmp_path)
    store.manifests_dir().joinpath("broken.json").write_text("{not json", encoding="utf-8")
    store.manifests_dir().joinpath("nameless.json").write_text(json.dumps({"root": str(tmp_path)}), encoding="utf-8")
    manifests = store.list_manifests()
    assert [m["id"] for m in manifests] == ["demo"]
    assert store.load_manifest("demo")["name"] == "Demo"
    assert store.load_manifest("arch:demo")["id"] == "demo", "graph ids resolve too"
    assert store.load_manifest("missing") is None
    assert store.load_manifest("../etc") is None
    assert store.graph_id(manifests[0]) == "arch:demo"


def test_no_manifest_directory_is_empty(home):
    assert store.list_manifests() == []


# ── Reading models ───────────────────────────────────────────────────────────


def test_read_local_model_and_revision_without_git(home, tmp_path):
    root = _write_service(home, tmp_path)
    manifest = store.load_manifest("demo")
    read = store.read_model(manifest, git_runner=lambda _root: "")
    assert read["source"] == "local" and read["model"]["title"] == "Demo"
    assert read["revision"].startswith("sha256:"), "no repository → content digest"
    assert store.read_model(manifest, git_runner=lambda _root: "deadbeef")["revision"] == "deadbeef"
    (root / "architecture" / "model" / "model.json").unlink()
    with pytest.raises(store.ArchitectureError) as missing:
        store.read_model(manifest)
    assert missing.value.code == 4032 and "compiler" in missing.value.message


def test_model_must_be_json_object_with_schema_inside_root(home, tmp_path):
    root = _write_service(home, tmp_path)
    manifest = store.load_manifest("demo")
    (root / "architecture" / "model" / "model.json").write_text("[1,2]", encoding="utf-8")
    with pytest.raises(store.ArchitectureError, match="schema_version"):
        store.read_local_model(manifest)
    (root / "architecture" / "model" / "model.json").write_text("{", encoding="utf-8")
    with pytest.raises(store.ArchitectureError, match="valid JSON"):
        store.read_local_model(manifest)
    manifest["model"] = "architecture/../../outside.json"
    with pytest.raises(store.ArchitectureError, match="escapes"):
        store.read_local_model(manifest)


def test_read_github_model_uses_raw_url_and_token(home, monkeypatch):
    manifest = store.normalize_manifest({"name": "P", "description": "d", "repository": "o/r", "ref": "abc"}, "p")
    seen = {}

    def opener(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return json.dumps(_model(source_revision="abc123")).encode("utf-8")

    monkeypatch.setenv("GITHUB_TOKEN", "t0k")
    read = store.read_model(manifest, opener=opener)
    assert seen["url"] == "https://raw.githubusercontent.com/o/r/abc/architecture/model/model.json"
    assert seen["headers"]["Authorization"] == "Bearer t0k"
    assert read == {"model": read["model"], "revision": "abc123", "source": "github"}

    def failing(url, headers):
        raise OSError("offline")

    with pytest.raises(store.ArchitectureError) as err:
        store.read_model(manifest, opener=failing)
    assert err.value.code == 4032 and "offline" in err.value.message


# ── Summaries, snapshots, checks ─────────────────────────────────────────────


def test_summarize_model_counts_what_portal_shows():
    summary = store.summarize_model(_model())
    assert summary == {
        "schema_version": "1.0.0", "title": "Demo", "source_tree_sha256": DIGEST,
        "components": 2, "files": 3, "lines": 120, "nodes": 2, "edges": 1, "flows": 1,
        "invariants": {"total": 2, "holds": 1, "violated": ["two"]},
        "stores": 1, "externals": 2, "gates": 14, "ratchets": 7, "workflows": 7,
    }
    assert store.summarize_model({"schema_version": "1.0.0"})["invariants"] == {"total": 0, "holds": 0, "violated": []}
    # Portal's compiler names its inventory swift_files/swift_lines; the contract names files/lines. Both count.
    swift = store.summarize_model(_model(inventory={"swift_files": 9, "swift_lines": 900, "declarations": 1}))
    assert (swift["files"], swift["lines"]) == (9, 900)


def test_snapshots_are_idempotent_and_keep_genesis(home, monkeypatch):
    monkeypatch.setattr(store, "MAX_SNAPSHOTS", 3)
    for revision in ("r1", "r2", "r3", "r4", "r5"):
        store.store_snapshot("demo", revision, _model(title=revision), "local")
    store.store_snapshot("demo", "r5", _model(title="again"), "local")
    index = store.list_snapshots("demo")
    assert index["latest"] == "r5"
    assert [r["revision"] for r in index["revisions"]] == ["r1", "r4", "r5"], "genesis kept, oldest after it pruned"
    assert store.load_snapshot("demo", "r1")["title"] == "r1"
    assert store.load_snapshot("demo", "r2") is None
    assert store.load_snapshot("demo", "r5")["title"] == "r5", "a known revision is not rewritten"
    assert index["revisions"][0]["summary"]["components"] == 2


def test_run_check_records_pass_fail_and_unavailable(home, tmp_path):
    _write_service(home, tmp_path, check=["true"])
    manifest = store.load_manifest("demo")

    class Completed:
        def __init__(self, code, out=""):
            self.returncode = code
            self.stdout = out
            self.stderr = ""

    passed = store.run_check(manifest, runner=lambda *a, **k: Completed(0, "ok\n"))
    assert passed["status"] == "passed" and passed["exit_code"] == 0 and passed["output"] == "ok"
    assert passed["command"] == ["true"] and passed["checked_at"] and "duration_s" in passed
    failed = store.run_check(manifest, runner=lambda *a, **k: Completed(2, "x" * 5000))
    assert failed["status"] == "failed" and len(failed["output"]) == store.MAX_CHECK_OUTPUT

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="true", timeout=1)

    timed_out = store.run_check(manifest, runner=timeout, timeout=1)
    assert timed_out["status"] == "unavailable" and "exceeded" in timed_out["reason"]

    def missing(*a, **k):
        raise OSError("no such file")

    assert store.run_check(manifest, runner=missing)["status"] == "unavailable"
    runs = store.list_checks("demo")
    assert [r["status"] for r in runs] == ["unavailable", "unavailable", "failed", "passed"], "newest first"
    assert store.last_check("demo")["status"] == "unavailable"

    manifest["check"] = None
    assert store.run_check(manifest)["reason"].startswith("no check command")
    github = store.normalize_manifest({"name": "P", "description": "d", "repository": "o/r"}, "gh")
    assert "repository's CI" in store.run_check(github)["reason"]


def test_run_check_really_runs_the_command(home, tmp_path):
    _write_service(home, tmp_path, check=["python3", "-c", "import sys; print('checked'); sys.exit(3)"])
    manifest = store.load_manifest("demo")
    result = store.run_check(manifest, timeout=60)
    assert result["status"] == "failed" and result["exit_code"] == 3 and "checked" in result["output"]


def test_status_and_describe_round_trip(home, tmp_path):
    root = _write_service(home, tmp_path, check=["true"], source_files=["scripts/x.py"])
    manifest = store.load_manifest("demo")
    before = store.status_for(manifest)
    assert before["ref"] == "arch:demo" and before["source"] == "local" and before["snapshots"] == 0
    assert "check" not in before

    document = store.describe(manifest, git_runner=lambda _root: "rev1")
    service = dict(document["service"])
    [sink] = service.pop("logs")
    assert sink["id"] == "app" and sink["kind"] == "file" and sink["exists"] is True and sink["path"] == str(root / "logs" / "app.log")
    assert service == {
        "id": "arch:demo", "label": "Demo", "description": "A demo service.", "source": "local",
        "root": str(root), "repository": None, "ref": None, "model_path": store.DEFAULT_MODEL_PATH,
        "check_configured": True, "runtime": None,
    }
    assert document["revision"] == "rev1" and document["model"]["title"] == "Demo"
    assert document["summary"]["components"] == 2 and document["check"] is None
    assert document["contract"]["name"] == "hermes.architecture" and document["contract"]["major"] == 1
    assert before["contract"] == {"name": "hermes.architecture", "version": "1.0"} and before["conforming"] is True
    store.run_check(manifest, runner=lambda *a, **k: type("C", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    after = store.status_for(manifest)
    assert after["snapshots"] == 1 and after["check"]["status"] == "passed" and after["summary"]["nodes"] == 2

    stored = store.describe(manifest, revision="rev1")
    assert stored["revision"] == "rev1" and stored["model"]["title"] == "Demo" and stored["check"]["status"] == "passed"
    with pytest.raises(store.ArchitectureError) as err:
        store.describe(manifest, revision="nope")
    assert err.value.code == 4404
