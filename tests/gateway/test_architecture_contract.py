"""The hermes.architecture v1 contract at the gateway: a document is validated on
every read, refused (4033) and never snapshotted when it does not conform, and
served with the contract named in the envelope, the node annotation and the
capabilities (tui_gateway.architecture_contract, architecture_store, handlers)."""
import copy
import json
import logging
from pathlib import Path

import pytest

import tui_gateway.methods_architecture as ma
import tui_gateway.methods_harness as mh
from tests.gateway.architecture_fixtures import local_manifest, minimal_document
from tui_gateway import architecture_contract as contract
from tui_gateway import architecture_store as store

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _write_service(tmp_path, document, service_id="demo"):
    root = tmp_path / service_id
    (root / "architecture" / "model").mkdir(parents=True)
    (root / "architecture" / "model" / "model.json").write_text(json.dumps(document), encoding="utf-8")
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / f"{service_id}.json").write_text(json.dumps(
        local_manifest(root, name=service_id.title(), description="A demo.")
    ), encoding="utf-8")
    return root


def _stub(module):
    pending = dict(module._registry._pending)
    for fn in pending.values():
        fn.__globals__["_ok"] = lambda rid, result: {"rid": rid, "result": result}
        fn.__globals__["_err"] = lambda rid, code, msg: {"rid": rid, "error": {"code": code, "message": msg}}
        fn.__globals__["_broadcast_global_event"] = lambda event, payload=None: None
        fn.__globals__.setdefault("logger", logging.getLogger("test"))
    return pending


# ── The contract module ──────────────────────────────────────────────────────


def test_minimal_document_conforms_and_names_the_contract():
    document = minimal_document()
    assert contract.validate_document(document) == []
    assert contract.conforms(document)
    assert contract.parse_version(document) == (1, 0, 0)
    described = contract.describe_contract()
    assert described["name"] == "hermes.architecture" and described["version"] == "1.0"
    assert described["major"] == 1 and described["minor"] == 0
    assert described["required_sections"] == ["components", "interplay", "extraction", "ci", "inventory", "evidence_metadata"]
    assert described["optional_sections"] == ["stores", "externals", "layers", "edges", "behavior"]
    assert described["schema_digest"] == contract.schema_digest()


def test_unsupported_major_is_one_message_and_nothing_else_is_reported():
    problems = contract.validate_document(minimal_document(schema_version="2.0.0"))
    assert problems == ["document.schema_version: major 2 is not supported (this consumer speaks hermes.architecture v1.x)"]
    assert contract.validate_document({"title": "x"}) == [
        "document.schema_version: missing or not semver (contract hermes.architecture v1.0)",
    ]
    assert contract.validate_document("not a dict")[0].startswith("document.schema_version")
    # A 1.x minor with fields this consumer has never heard of is accepted.
    assert contract.validate_document(minimal_document(schema_version="1.7.2", future_section={"anything": 1})) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.pop("extraction"), "document: missing required field 'extraction'"),
    (lambda d: d.pop("ci"), "document: missing required field 'ci'"),
    (lambda d: d["interplay"]["edges"].append({"source": "n1", "target": "ghost", "relation": "calls", "class": "interplay"}),
     "interplay.edges[1]: target 'ghost' is not a node"),
    (lambda d: d["extraction"]["entities"].pop(), "extraction.entities: construction 'n2' has no provenance"),
    (lambda d: d["extraction"]["entities"].append({"id": "orphan", "kind": "store", "label": "O", "origins": [{"path": "src/a.py", "line": 1, "rule": "py.class", "family": "store"}]}),
     "extraction.entities: 'orphan' is not a construction on the map"),
    (lambda d: d["ci"]["merge"]["inputs"].append("ghost/job"), "ci.merge: input 'ghost/job' is not a job"),
    (lambda d: d["ci"]["merge"].__setitem__("inputs", []), "document.ci.merge.inputs: needs at least 1 item(s), has 0"),
    (lambda d: d["extraction"]["files"][2].__setitem__("touched", True), "extraction.files[src/untouched.py]: touched must equal citations > 0"),
    (lambda d: d["extraction"]["passes"].pop(0), "extraction.passes: no mechanical pass"),
    (lambda d: d["interplay"]["invariants"][0].__setitem__("status", "maybe"), "document.interplay.invariants[0].status: 'maybe' is not one of"),
    (lambda d: d["interplay"]["flows"][0]["steps"][0].__setitem__("to", "nowhere"), "interplay.flows[f1].steps[0]: to 'nowhere' is not on the map"),
    (lambda d: d["ci"]["jobs"][0].__setitem__("role", "cron"), "document.ci.jobs[0].role: 'cron' is not one of"),
])
def test_each_loosening_is_named(mutate, needle):
    document = minimal_document()
    mutate(document)
    problems = contract.validate_document(document)
    assert any(needle in problem for problem in problems), f"{needle!r} not in {problems}"


def test_a_file_cited_only_by_people_is_untouched():
    document = minimal_document()
    record = document["extraction"]["files"][2]
    assert record["semantic_citations"] == 1 and record["citations"] == 0 and record["touched"] is False
    assert contract.validate_document(document) == []


def test_schema_export_and_digest_do_not_drift():
    exported = (ROOT / "docs/api/architecture-document-v1.schema.json").read_text(encoding="utf-8")
    assert exported == contract.schema_json(), "regenerate with: python3 tui_gateway/architecture_contract.py --schema"
    docs = (ROOT / "docs/api/architecture.md").read_text(encoding="utf-8")
    assert contract.schema_digest() in docs, "docs/api/architecture.md must quote the current schema digest"
    schema = json.loads(exported)
    assert schema["required"] == ["schema_version", "title", "description", "source_tree_sha256", *contract.REQUIRED_SECTIONS]


# ── The store ────────────────────────────────────────────────────────────────


def test_describe_refuses_a_nonconforming_model_and_stores_no_snapshot(home, tmp_path):
    document = minimal_document()
    del document["extraction"]
    document["interplay"]["edges"].append({"source": "n1", "target": "ghost", "relation": "calls", "class": "interplay"})
    _write_service(tmp_path, document)
    manifest = store.load_manifest("demo")
    with pytest.raises(store.ArchitectureError) as err:
        store.describe(manifest, git_runner=lambda _root: "rev1")
    assert err.value.code == 4033
    assert "does not conform to hermes.architecture v1.0" in err.value.message
    assert "missing required field 'extraction'" in err.value.message
    assert store.list_snapshots("demo")["revisions"] == [], "nothing non-conforming is ever stored"
    status = store.status_for(manifest)
    assert status["conforming"] is False and "does not conform" in status["problem"]
    assert status["contract"] == {"name": "hermes.architecture", "version": "1.0"}


def test_message_shows_five_problems_and_counts_the_rest():
    message = store.nonconforming_message("m.json", [f"p{i}" for i in range(8)])
    assert message.endswith("p0; p1; p2; p3; p4 (+3 more)")
    assert store.nonconforming_message("m.json", ["only"]).endswith(": only")


def test_conforming_model_is_snapshotted_with_the_contract_in_the_envelope(home, tmp_path):
    _write_service(tmp_path, minimal_document())
    manifest = store.load_manifest("demo")
    document = store.describe(manifest, git_runner=lambda _root: "rev1")
    assert document["contract"] == contract.describe_contract()
    assert document["summary"]["files"] == 3 and document["summary"]["lines"] == 120
    index = store.list_snapshots("demo")
    assert index["latest"] == "rev1"
    assert index["revisions"][0]["contract"] == {"name": "hermes.architecture", "version": "1.0"}
    status = store.status_for(manifest)
    assert status["conforming"] is True and "problem" not in status
    # A stored revision is served with the envelope too.
    assert store.describe(manifest, revision="rev1")["contract"]["major"] == 1


def test_github_conformance_follows_snapshots(home):
    manifest = store.normalize_manifest({"name": "R", "description": "d", "repository": "o/r", "ref": "main"}, "remote")
    unread = store.status_for(manifest)
    assert unread["conforming"] is False and "not read yet" in unread["problem"]
    opener = lambda url, headers: json.dumps(minimal_document(source_revision="abc")).encode("utf-8")
    store.describe(manifest, opener=opener)
    assert store.status_for(manifest)["conforming"] is True

    bad = lambda url, headers: json.dumps(minimal_document(schema_version="9.0.0")).encode("utf-8")
    with pytest.raises(store.ArchitectureError) as err:
        store.describe(manifest, opener=bad)
    assert err.value.code == 4033 and "major 9" in err.value.message


# ── The handlers and the capabilities ────────────────────────────────────────


def test_handlers_carry_the_contract_and_surface_4033(home, tmp_path):
    pending = _stub(ma)
    _write_service(tmp_path, minimal_document())
    listed = pending["architecture.list"](1, {})["result"]
    assert listed["contract"] == {"name": "hermes.architecture", "version": "1.0"}
    (service,) = listed["services"]
    assert service["status"]["conforming"] is True and service["status"]["contract"]["version"] == "1.0"
    described = pending["architecture.describe"](2, {"service": "arch:demo"})["result"]
    assert described["contract"]["name"] == "hermes.architecture" and described["contract"]["schema_digest"] == contract.schema_digest()
    assert described["model"]["extraction"]["summary"]["entities"] == 2

    broken = copy.deepcopy(minimal_document())
    broken["ci"]["merge"]["inputs"] = ["nobody"]
    (tmp_path / "demo" / "architecture" / "model" / "model.json").write_text(json.dumps(broken), encoding="utf-8")
    refused = pending["architecture.describe"](3, {"service": "arch:demo"})
    assert refused["error"]["code"] == 4033
    assert "ci.merge: input 'nobody' is not a job" in refused["error"]["message"]
    relisted = pending["architecture.list"](4, {})["result"]["services"][0]["status"]
    assert relisted["conforming"] is False and "nobody" in relisted["problem"]


def test_capabilities_advertise_the_contract():
    pending = _stub(mh)
    result = pending["gateway.capabilities"](1, {})["result"]
    assert result["architecture"]["methods"] == ["architecture.list", "architecture.describe", "architecture.check", "architecture.history",
                                                 "architecture.logs", "architecture.logs.follow"]
    assert result["architecture"]["contract"] == contract.describe_contract()
    for name in result["architecture"]["methods"]:
        assert name in result["capability_names"], "the flat list old clients read is unchanged"
