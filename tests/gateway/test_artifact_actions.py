"""Tests for artifact backend intent invocation (V1 slice).

Covers:
- invoke() happy paths: non-destructive handler runs inline
- invoke() destructive handler: needs_confirmation + challenge issued
- confirm() happy path: challenge consumed, handler runs
- confirm() expired / wrong artifact: fails gracefully
- conflict: stale revision returns conflict without running handler
- idempotency: second invoke with same key returns cached result
- unsupported: unknown binding_id or unregistered intent
- tombstone handler: _deleted set in content, artifact updated
- refresh handler: returns succeeded (with/without maintainer)
- _tombstone_entity: dataset / map / model path coverage
"""

import json
import time

import pytest


@pytest.fixture()
def artifact_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset in-memory stores between tests."""
    from tui_gateway import artifact_actions as aa
    aa._pending_challenges.clear()
    aa._idempotency_cache.clear()
    yield
    aa._pending_challenges.clear()
    aa._idempotency_cache.clear()


# ── helpers ────────────────────────────────────────────────────────────────


def _make_artifact(artifact_home, artifact_id="test-art", kind="dataset", actions=None):
    from tui_gateway import artifact_store as store
    content = json.dumps({"key": "name", "rows": [{"name": "Alice"}, {"name": "Bob"}]})
    stored = store.set_artifact(
        artifact_id=artifact_id,
        kind=kind,
        content=content,
        title="Test",
        updated_by="test",
        actions=actions,
    )
    return stored


# ── invoke ─────────────────────────────────────────────────────────────────


def test_invoke_non_destructive_handler_runs_inline(artifact_home):
    from tui_gateway import artifact_actions as aa

    actions = [{"type": "intent", "id": "do-refresh", "label": "Refresh",
                "intent": "artifact.refresh", "presentation": {"role": "normal"}}]
    stored = _make_artifact(artifact_home, actions=actions)

    result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="do-refresh",
        entity_ref="",
        idempotency_key="key-1",
    )
    assert result["status"] == "succeeded"


def test_invoke_destructive_returns_needs_confirmation(artifact_home):
    from tui_gateway import artifact_actions as aa

    actions = [{"type": "intent", "id": "del-row", "label": "Delete",
                "intent": "artifact.entity.tombstone",
                "presentation": {"role": "destructive"}}]
    stored = _make_artifact(artifact_home, actions=actions)

    result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="del-row",
        entity_ref="dataset/alice",
        idempotency_key="key-2",
    )
    assert result["status"] == "needs_confirmation"
    assert "challenge" in result
    assert "prompt" in result


def test_invoke_conflict_stale_revision(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_store as store

    actions = [{"type": "intent", "id": "do-refresh", "label": "Refresh",
                "intent": "artifact.refresh", "presentation": {"role": "normal"}}]
    stored = _make_artifact(artifact_home, actions=actions)
    store.set_artifact("test-art", "dataset",
                       json.dumps({"key": "name", "rows": []}),
                       updated_by="other")  # bumps rev to 2

    result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],  # stale: still rev 1
        binding_id="do-refresh",
        entity_ref="",
        idempotency_key="key-3",
    )
    assert result["status"] == "conflict"


def test_invoke_unknown_binding_returns_unsupported(artifact_home):
    from tui_gateway import artifact_actions as aa

    stored = _make_artifact(artifact_home, actions=[])

    result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="no-such-binding",
        entity_ref="",
        idempotency_key="key-4",
    )
    assert result["status"] == "unsupported"


def test_invoke_unregistered_intent_returns_unsupported(artifact_home):
    from tui_gateway import artifact_actions as aa

    actions = [{"type": "intent", "id": "custom", "label": "Custom",
                "intent": "my.custom.intent.not.registered",
                "presentation": {"role": "normal"}}]
    stored = _make_artifact(artifact_home, actions=actions)

    result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="custom",
        entity_ref="",
        idempotency_key="key-5",
    )
    assert result["status"] == "unsupported"


def test_invoke_idempotency_key_returns_cached_result(artifact_home):
    from tui_gateway import artifact_actions as aa

    actions = [{"type": "intent", "id": "do-refresh", "label": "Refresh",
                "intent": "artifact.refresh", "presentation": {"role": "normal"}}]
    stored = _make_artifact(artifact_home, actions=actions)

    r1 = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="do-refresh",
        entity_ref="",
        idempotency_key="same-key",
    )
    # Mutate the artifact to bump rev — if idempotency works we still get r1
    from tui_gateway import artifact_store as store
    store.set_artifact("test-art", "dataset",
                       json.dumps({"key": "name", "rows": []}), updated_by="bump")

    r2 = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"] + 99,  # would conflict if not cached
        binding_id="do-refresh",
        entity_ref="",
        idempotency_key="same-key",
    )
    assert r1 == r2


# ── confirm ────────────────────────────────────────────────────────────────


def test_confirm_destructive_after_challenge(artifact_home):
    from tui_gateway import artifact_actions as aa

    actions = [{"type": "intent", "id": "del-row", "label": "Delete",
                "intent": "artifact.entity.tombstone",
                "presentation": {"role": "destructive"}}]
    stored = _make_artifact(artifact_home, actions=actions)

    invoke_result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="del-row",
        entity_ref="alice",
        idempotency_key="key-c1",
    )
    assert invoke_result["status"] == "needs_confirmation"

    confirm_result = aa.confirm(
        artifact_id="test-art",
        challenge=invoke_result["challenge"],
    )
    assert confirm_result["status"] == "succeeded"


def test_confirm_expired_challenge_fails(artifact_home):
    from tui_gateway import artifact_actions as aa

    actions = [{"type": "intent", "id": "del-row", "label": "Delete",
                "intent": "artifact.entity.tombstone",
                "presentation": {"role": "destructive"}}]
    stored = _make_artifact(artifact_home, actions=actions)

    invoke_result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="del-row",
        entity_ref="alice",
        idempotency_key="key-c2",
    )
    challenge = invoke_result["challenge"]

    # Manually expire the challenge by back-dating its expiry.
    aa._pending_challenges[challenge]["expires"] = time.monotonic() - 1
    result = aa.confirm(artifact_id="test-art", challenge=challenge)
    assert result["status"] == "failed"


def test_confirm_wrong_artifact_fails(artifact_home):
    from tui_gateway import artifact_actions as aa

    actions = [{"type": "intent", "id": "del-row", "label": "Delete",
                "intent": "artifact.entity.tombstone",
                "presentation": {"role": "destructive"}}]
    stored = _make_artifact(artifact_home, actions=actions)

    invoke_result = aa.invoke(
        artifact_id="test-art",
        artifact_rev=stored["rev"],
        binding_id="del-row",
        entity_ref="alice",
        idempotency_key="key-c3",
    )
    result = aa.confirm(
        artifact_id="other-artifact",  # wrong artifact
        challenge=invoke_result["challenge"],
    )
    assert result["status"] == "failed"


# ── tombstone handler ──────────────────────────────────────────────────────


def test_tombstone_handler_marks_dataset_row(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_store as store

    stored = _make_artifact(artifact_home)
    result = aa._handle_tombstone(
        artifact_id="test-art", binding_id="", entity_ref="alice"
    )
    assert result["status"] == "succeeded"

    updated = store.get_artifact("test-art")
    rows = json.loads(updated["content"])["rows"]
    alice = next(r for r in rows if r.get("name") == "Alice")
    assert alice["_deleted"] is True


def test_tombstone_handler_marks_map_marker(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_store as store

    content = json.dumps({"markers": [{"label": "Ekkamai", "lat": 13.72, "lon": 100.58}]})
    store.set_artifact("map-art", "map", content, updated_by="test")

    result = aa._handle_tombstone(
        artifact_id="map-art", binding_id="", entity_ref="ekkamai"
    )
    assert result["status"] == "succeeded"

    updated = store.get_artifact("map-art")
    markers = json.loads(updated["content"])["markers"]
    assert markers[0]["_deleted"] is True


def test_tombstone_handler_marks_model_entity(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_store as store

    content = json.dumps({
        "entities": {
            "issues": {"key": "id", "items": [{"id": "ISS-1"}, {"id": "ISS-2"}]}
        }
    })
    store.set_artifact("model-art", "model", content, updated_by="test")

    result = aa._handle_tombstone(
        artifact_id="model-art", binding_id="", entity_ref="issues/iss-1"
    )
    assert result["status"] == "succeeded"

    updated = store.get_artifact("model-art")
    items = json.loads(updated["content"])["entities"]["issues"]["items"]
    iss1 = next(i for i in items if i.get("id") == "ISS-1")
    assert iss1["_deleted"] is True


# ── refresh handler ────────────────────────────────────────────────────────


def test_refresh_with_no_maintainer(artifact_home):
    from tui_gateway import artifact_actions as aa

    _make_artifact(artifact_home)
    result = aa._handle_refresh(artifact_id="test-art", binding_id="", entity_ref="")
    assert result["status"] == "succeeded"
    assert "No maintainer" in result.get("message", "")


def test_refresh_with_maintainer_declared(artifact_home):
    from tui_gateway import artifact_actions as aa, artifact_store as store

    content = json.dumps({
        "maintainers": ["cron:abc123"],
        "key": "name", "rows": [],
    })
    store.set_artifact("test-art", "dataset", content, updated_by="test")

    result = aa._handle_refresh(artifact_id="test-art", binding_id="", entity_ref="")
    assert result["status"] == "succeeded"
    assert "maintainer" in result.get("message", "").lower()


# ── actions persisted through artifact store ──────────────────────────────


def test_actions_persist_and_carry_forward(artifact_home):
    from tui_gateway import artifact_store as store

    actions = [{"type": "intent", "id": "refresh", "label": "Refresh",
                "intent": "artifact.refresh"}]
    stored = _make_artifact(artifact_home, actions=actions)
    assert stored.get("actions") == actions

    # Update without supplying actions — should carry forward
    updated = store.set_artifact(
        "test-art", "dataset",
        json.dumps({"key": "name", "rows": []}),
        updated_by="agent",
    )
    assert updated.get("actions") == actions


def test_actions_overwritten_when_supplied(artifact_home):
    from tui_gateway import artifact_store as store

    original_actions = [{"type": "delete"}]
    _make_artifact(artifact_home, actions=original_actions)

    new_actions = [{"type": "toggle", "field": "done"}]
    updated = store.set_artifact(
        "test-art", "dataset",
        json.dumps({"key": "name", "rows": []}),
        updated_by="agent",
        actions=new_actions,
    )
    assert updated.get("actions") == new_actions
