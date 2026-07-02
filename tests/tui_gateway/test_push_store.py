"""Tests for the APNs device-token registry and sender gating."""

import pytest

from tui_gateway import push_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolate the token store per test."""
    monkeypatch.setattr(push_store, "_store_path", lambda: tmp_path / "push_tokens.json")
    return tmp_path / "push_tokens.json"


class TestRegistry:
    def test_register_and_list(self, store):
        entry = push_store.register_token("ABCD" * 16, platform="macos", device_name="Test Mac")
        assert entry["token"] == "abcd" * 16  # normalized lowercase
        assert entry["platform"] == "macos"
        tokens = push_store.list_tokens()
        assert len(tokens) == 1
        assert tokens[0]["device_name"] == "Test Mac"

    def test_register_is_idempotent(self, store):
        push_store.register_token("aa11", platform="ios")
        first = push_store.list_tokens()[0]
        push_store.register_token("AA11", platform="ios", device_name="Phone")
        tokens = push_store.list_tokens()
        assert len(tokens) == 1
        assert tokens[0]["device_name"] == "Phone"
        assert tokens[0]["registered"] == first["registered"]

    def test_register_rejects_bad_input(self, store):
        assert "error" in push_store.register_token("")
        assert "error" in push_store.register_token("abc", platform="android")
        assert push_store.list_tokens() == []

    def test_unregister(self, store):
        push_store.register_token("aa11")
        assert push_store.unregister_token("AA11") is True
        assert push_store.unregister_token("aa11") is False
        assert push_store.list_tokens() == []

    def test_prune_token(self, store):
        push_store.register_token("dead")
        push_store.prune_token("dead")
        assert push_store.list_tokens() == []

    def test_registry_bounded(self, store):
        for i in range(push_store.MAX_TOKENS + 5):
            push_store.register_token(f"tok{i:04d}")
        assert len(push_store.list_tokens()) == push_store.MAX_TOKENS


class TestSenderGating:
    def test_unconfigured_is_noop(self, monkeypatch):
        from tui_gateway import apns_sender

        for var in ("APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID"):
            monkeypatch.delenv(var, raising=False)
        assert apns_sender.is_configured() is False
        # Must not raise or spawn work when unconfigured.
        apns_sender.notify_all("t", "b", session_id="s1")

    def test_configured_detection(self, monkeypatch, tmp_path):
        from tui_gateway import apns_sender

        key = tmp_path / "AuthKey_TEST.p8"
        key.write_text("---fake---", encoding="utf-8")
        monkeypatch.setenv("APNS_KEY_PATH", str(key))
        monkeypatch.setenv("APNS_KEY_ID", "ABC123DEFG")
        monkeypatch.setenv("APNS_TEAM_ID", "TEAM456789")
        monkeypatch.setenv("APNS_BUNDLE_ID", "com.researchoors.HermesNative.macOS")
        assert apns_sender.is_configured() is True
        cfg = apns_sender._config()
        assert cfg["host"].endswith("api.push.apple.com")
        monkeypatch.setenv("APNS_ENV", "sandbox")
        assert apns_sender._config()["host"].endswith("api.sandbox.push.apple.com")
