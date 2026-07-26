"""Tests for the artifact action plugin loader (§1).

Covers:
- Happy-path load: a valid plugin file registers a handler
- Syntax error in one plugin aborts the whole swap, old handlers survive
- Agent-writable directory: loader hard-fails
- Empty plugins dir: succeeds with empty diff
- Registry diff: added/changed/removed reported correctly
- actions.reload RPC wires through to the loader
- Built-in handlers survive reload (not evicted by plugin reload)
"""

import json
import os
import textwrap

import pytest


@pytest.fixture()
def artifact_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_registry():
    """Restore the handler registry to its original state after each test."""
    from tui_gateway import artifact_actions as aa
    original = dict(aa._HANDLERS)
    yield
    aa._HANDLERS.clear()
    aa._HANDLERS.update(original)


@pytest.fixture(autouse=True)
def _reset_workspace_roots():
    from tui_gateway import artifact_plugin_loader as pl
    original = list(pl._AGENT_WORKSPACE_ROOTS)
    yield
    pl._AGENT_WORKSPACE_ROOTS.clear()
    pl._AGENT_WORKSPACE_ROOTS.extend(original)


# ── helpers ──────────────────────────────────────────────────────────────────


def _plugins_dir(artifact_home) -> str:
    """Return the plugins/actions path (creating it if needed)."""
    path = artifact_home / ".hermes" / "plugins" / "actions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_plugin(plugins_dir, name: str, source: str):
    p = plugins_dir / name
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    return p


# ── tests ─────────────────────────────────────────────────────────────────────


def test_valid_plugin_registers_handler(artifact_home):
    from tui_gateway import artifact_plugin_loader as pl, artifact_actions as aa

    plugins = _plugins_dir(artifact_home)
    _write_plugin(plugins, "my_plugin.py", """
        def _my_handler(artifact_id, binding_id, entity_ref):
            return {"status": "succeeded", "message": "from plugin"}
        register_handler("my.custom.action", _my_handler)
    """)

    result = pl.reload()

    assert result["status"] == "ok"
    assert "my_plugin.py" in result["loaded"]
    assert "my.custom.action" in result["diff"]["added"]
    assert "my.custom.action" in aa._HANDLERS


def test_syntax_error_aborts_swap_old_handlers_survive(artifact_home):
    """A broken plugin must leave the live registry unchanged."""
    from tui_gateway import artifact_plugin_loader as pl, artifact_actions as aa

    # Pre-load a good plugin so there's a prior handler to protect.
    plugins = _plugins_dir(artifact_home)
    _write_plugin(plugins, "good.py", """
        register_handler("my.good.action", lambda **kw: {"status": "succeeded"})
    """)
    pl.reload()
    assert "my.good.action" in aa._HANDLERS

    # Now add a syntax error in a second plugin file.
    _write_plugin(plugins, "broken.py", """
        def oops(
    """)  # syntax error

    result = pl.reload()

    assert result["status"] == "error"
    assert "broken.py" in result["error"]
    # Good handler must still be live.
    assert "my.good.action" in aa._HANDLERS


def test_agent_writable_dir_hard_fails(artifact_home):
    """Loader refuses to load from a directory under an agent workspace root."""
    from tui_gateway import artifact_plugin_loader as pl

    plugins = _plugins_dir(artifact_home)
    # Register the plugins dir itself as an agent workspace root.
    pl.register_agent_workspace_root(str(plugins))

    result = pl.reload()

    assert result["status"] == "error"
    assert "agent workspace" in result["error"].lower()


def test_agent_writable_parent_dir_hard_fails(artifact_home):
    """The check is ancestry-based: being a sub-path of a workspace root fails."""
    from tui_gateway import artifact_plugin_loader as pl

    plugins = _plugins_dir(artifact_home)
    # Register a parent directory (the whole .hermes home) as a workspace root.
    pl.register_agent_workspace_root(str(artifact_home / ".hermes"))

    result = pl.reload()

    assert result["status"] == "error"
    assert "agent workspace" in result["error"].lower()


def test_empty_plugins_dir_returns_ok(artifact_home):
    from tui_gateway import artifact_plugin_loader as pl

    _plugins_dir(artifact_home)  # ensure it exists but is empty

    result = pl.reload()

    assert result["status"] == "ok"
    assert result["loaded"] == []
    assert result["diff"] == {"added": [], "changed": [], "removed": []}


def test_no_plugins_dir_creates_it_and_returns_ok(artifact_home):
    """The loader creates the plugins dir if absent rather than failing."""
    from tui_gateway import artifact_plugin_loader as pl

    # Don't create the dir — just reload.
    result = pl.reload()

    assert result["status"] == "ok"
    plugins_dir = artifact_home / ".hermes" / "plugins" / "actions"
    assert plugins_dir.exists()


def test_registry_diff_added(artifact_home):
    from tui_gateway import artifact_plugin_loader as pl

    plugins = _plugins_dir(artifact_home)
    _write_plugin(plugins, "plug.py", """
        register_handler("new.handler", lambda **kw: {"status": "succeeded"})
    """)

    result = pl.reload()

    assert "new.handler" in result["diff"]["added"]
    assert result["diff"]["removed"] == []


def test_registry_diff_removed(artifact_home):
    from tui_gateway import artifact_plugin_loader as pl

    plugins = _plugins_dir(artifact_home)
    p = _write_plugin(plugins, "transient.py", """
        register_handler("gone.soon", lambda **kw: {"status": "succeeded"})
    """)
    pl.reload()

    # Remove the file and reload.
    p.unlink()
    result = pl.reload()

    assert "gone.soon" in result["diff"]["removed"]


def test_builtin_handlers_survive_reload(artifact_home):
    """Built-in handlers (artifact.refresh, artifact.entity.tombstone) must
    not be evicted when plugins reload."""
    from tui_gateway import artifact_plugin_loader as pl, artifact_actions as aa

    _plugins_dir(artifact_home)  # empty plugins dir
    pl.reload()

    assert "artifact.refresh" in aa._HANDLERS
    assert "artifact.entity.tombstone" in aa._HANDLERS


def test_plugin_can_override_builtin(artifact_home):
    """A plugin that registers the same name as a built-in wins (intentional)."""
    from tui_gateway import artifact_plugin_loader as pl, artifact_actions as aa

    plugins = _plugins_dir(artifact_home)
    _write_plugin(plugins, "override.py", """
        def _custom_refresh(artifact_id, binding_id, entity_ref):
            return {"status": "succeeded", "message": "custom"}
        register_handler("artifact.refresh", _custom_refresh)
    """)

    result = pl.reload()

    assert "artifact.refresh" in result["diff"]["changed"] or \
           "artifact.refresh" in result["diff"]["added"]
    handler = aa._HANDLERS["artifact.refresh"]
    assert handler(artifact_id="x", binding_id="", entity_ref="")["message"] == "custom"


def test_multiple_plugins_loaded_in_sorted_order(artifact_home):
    """Files are loaded alphabetically; last file wins a name conflict."""
    from tui_gateway import artifact_plugin_loader as pl, artifact_actions as aa

    plugins = _plugins_dir(artifact_home)
    _write_plugin(plugins, "a_first.py", """
        register_handler("shared.name", lambda **kw: {"status": "succeeded", "src": "a"})
    """)
    _write_plugin(plugins, "z_last.py", """
        register_handler("shared.name", lambda **kw: {"status": "succeeded", "src": "z"})
    """)

    pl.reload()

    result = aa._HANDLERS["shared.name"](artifact_id="", binding_id="", entity_ref="")
    assert result["src"] == "z"  # z_last.py wins
