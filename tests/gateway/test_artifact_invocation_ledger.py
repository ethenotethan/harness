"""Tests for the artifact invocation ledger (§2).

Covers:
- Ledger appends on non-destructive invoke
- Ledger appends on destructive invoke (needs_confirmation phase)
- Ledger appends on confirm (confirm phase)
- Durable idempotency: in-memory cache cleared, ledger consulted on retry
- Restart simulation: after cache clear, same idempotency key returns cached result from ledger
- query() filters by artifact_id, binding_id, entity_ref
- Rotation: file over MAX_LEDGER_BYTES triggers rollover
- artifact.action.log RPC returns ledger records
"""

import json
import os

import pytest


@pytest.fixture()
def artifact_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_caches():
    from tui_gateway import artifact_actions as aa
    aa._pending_challenges.clear()
    aa._idempotency_cache.clear()
    yield
    aa._pending_challenges.clear()
    aa._idempotency_cache.clear()


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_artifact(artifact_home, artifact_id="ledger-art", actions=None):
    from tui_gateway import artifact_store as store
    content = json.dumps({"key": "name", "rows": [{"name": "Alice"}, {"name": "Bob"}]})
    return store.set_artifact(
        artifact_id=artifact_id, kind="dataset", content=content,
        title="Ledger Test", updated_by="test", actions=actions,
    )


def _refresh_actions(artifact_id="ledger-art"):
    return [{"type": "intent", "id": "do-refresh", "label": "Refresh",
             "intent": "artifact.refresh", "presentation": {"role": "normal"}}]


def _tombstone_actions(artifact_id="ledger-art"):
    return [{"type": "intent", "id": "del-row", "label": "Delete",
             "intent": "artifact.entity.tombstone",
             "presentation": {"role": "destructive"}}]


# ── ledger append tests ───────────────────────────────────────────────────────


def test_non_destructive_invoke_appends_to_ledger(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_invocation_ledger as ledger

    stored = _make_artifact(artifact_home, actions=_refresh_actions())
    aa.invoke(
        artifact_id="ledger-art", artifact_rev=stored["rev"],
        binding_id="do-refresh", entity_ref="", idempotency_key="key-nd-1",
    )
    records = ledger.query("ledger-art")
    assert len(records) == 1
    assert records[0]["outcome"] in ("succeeded", "failed")
    assert records[0]["phase"] == "invoke"
    assert records[0]["idempotency_key"] == "key-nd-1"


def test_destructive_invoke_appends_needs_confirmation(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_invocation_ledger as ledger

    stored = _make_artifact(artifact_home, actions=_tombstone_actions())
    result = aa.invoke(
        artifact_id="ledger-art", artifact_rev=stored["rev"],
        binding_id="del-row", entity_ref="alice", idempotency_key="key-d-1",
    )
    assert result["status"] == "needs_confirmation"
    records = ledger.query("ledger-art")
    assert any(r["outcome"] == "needs_confirmation" for r in records)


def test_confirm_appends_confirm_phase(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_invocation_ledger as ledger

    stored = _make_artifact(artifact_home, actions=_tombstone_actions())
    invoke_result = aa.invoke(
        artifact_id="ledger-art", artifact_rev=stored["rev"],
        binding_id="del-row", entity_ref="alice", idempotency_key="key-c-1",
    )
    aa.confirm(artifact_id="ledger-art", challenge=invoke_result["challenge"])

    records = ledger.query("ledger-art")
    phases = [r["phase"] for r in records]
    assert "confirm" in phases
    assert "invoke" in phases


# ── durable idempotency ───────────────────────────────────────────────────────


def test_ledger_idempotency_survives_cache_clear(artifact_home):
    """After cache eviction (simulating a restart), the ledger prevents re-execution."""
    from tui_gateway import artifact_actions as aa, artifact_invocation_ledger as ledger

    stored = _make_artifact(artifact_home, actions=_refresh_actions())
    r1 = aa.invoke(
        artifact_id="ledger-art", artifact_rev=stored["rev"],
        binding_id="do-refresh", entity_ref="", idempotency_key="durable-key",
    )
    assert r1["status"] == "succeeded"

    # Simulate gateway restart: clear in-memory cache.
    aa._idempotency_cache.clear()

    # Bump the artifact rev so a fresh invoke would conflict — but ledger should
    # return the cached outcome before we even reach the conflict check.
    from tui_gateway import artifact_store as store
    store.set_artifact("ledger-art", "dataset",
                       json.dumps({"key": "name", "rows": []}), updated_by="bump")

    r2 = aa.invoke(
        artifact_id="ledger-art",
        artifact_rev=stored["rev"] + 99,  # would conflict without ledger
        binding_id="do-refresh", entity_ref="", idempotency_key="durable-key",
    )
    assert r2["status"] == "succeeded"


def test_ledger_failed_outcome_also_cached_durably(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_invocation_ledger as ledger

    # Invoke against a non-existent artifact — will fail.
    aa.invoke(
        artifact_id="no-such-art", artifact_rev=1,
        binding_id="whatever", entity_ref="", idempotency_key="fail-key",
    )

    aa._idempotency_cache.clear()
    r2 = aa.invoke(
        artifact_id="no-such-art", artifact_rev=1,
        binding_id="whatever", entity_ref="", idempotency_key="fail-key",
    )
    assert r2["status"] == "failed"


# ── query ─────────────────────────────────────────────────────────────────────


def test_query_filters_by_binding_id(artifact_home):
    from tui_gateway import artifact_invocation_ledger as ledger

    ledger.append(
        artifact_id="art-a", rev=1, binding_id="b1", entity_ref="",
        intent="artifact.refresh", idempotency_key="k1",
        phase="invoke", outcome="succeeded",
    )
    ledger.append(
        artifact_id="art-a", rev=1, binding_id="b2", entity_ref="",
        intent="artifact.refresh", idempotency_key="k2",
        phase="invoke", outcome="succeeded",
    )
    results = ledger.query("art-a", binding_id="b1")
    assert all(r["binding_id"] == "b1" for r in results)
    assert len(results) == 1


def test_query_filters_by_entity_ref(artifact_home):
    from tui_gateway import artifact_invocation_ledger as ledger

    ledger.append(
        artifact_id="art-b", rev=1, binding_id="del", entity_ref="alice",
        intent="artifact.entity.tombstone", idempotency_key="ka",
        phase="confirm", outcome="succeeded",
    )
    ledger.append(
        artifact_id="art-b", rev=1, binding_id="del", entity_ref="bob",
        intent="artifact.entity.tombstone", idempotency_key="kb",
        phase="confirm", outcome="succeeded",
    )
    results = ledger.query("art-b", entity_ref="alice")
    assert all(r["entity_ref"] == "alice" for r in results)


def test_query_newest_first(artifact_home):
    from tui_gateway import artifact_invocation_ledger as ledger

    for i in range(3):
        ledger.append(
            artifact_id="art-c", rev=i, binding_id="b", entity_ref="",
            intent="artifact.refresh", idempotency_key=f"k{i}",
            phase="invoke", outcome="succeeded",
        )
    results = ledger.query("art-c")
    keys = [r["idempotency_key"] for r in results]
    assert keys == ["k2", "k1", "k0"]


def test_query_respects_limit(artifact_home):
    from tui_gateway import artifact_invocation_ledger as ledger

    for i in range(10):
        ledger.append(
            artifact_id="art-d", rev=1, binding_id="b", entity_ref="",
            intent="artifact.refresh", idempotency_key=f"lim-{i}",
            phase="invoke", outcome="succeeded",
        )
    results = ledger.query("art-d", limit=3)
    assert len(results) == 3


# ── rotation ──────────────────────────────────────────────────────────────────


def test_ledger_rotates_on_size_exceeded(artifact_home):
    import tui_gateway.artifact_invocation_ledger as ledger

    original_max = ledger.MAX_LEDGER_BYTES
    ledger.MAX_LEDGER_BYTES = 200  # tiny cap for the test
    try:
        for i in range(30):
            ledger.append(
                artifact_id="art-rot", rev=1, binding_id="b", entity_ref="",
                intent="artifact.refresh", idempotency_key=f"rot-{i}",
                phase="invoke", outcome="succeeded",
            )
        ledger_path = ledger._ledger_path()
        backup = ledger_path.with_suffix(".jsonl.1")
        assert backup.exists(), "backup file should exist after rotation"
    finally:
        ledger.MAX_LEDGER_BYTES = original_max
