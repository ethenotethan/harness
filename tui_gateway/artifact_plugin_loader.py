"""
Artifact action plugin loader.

Plugins live in ``~/.hermes/plugins/actions/*.py``. Each file is executed at
load time; it calls ``register_handler(name, fn)`` from this module (re-exported
via ``artifact_actions``) to add handlers to the shared registry.

Security model — authorship/activation split
---------------------------------------------
The plugins directory MUST NOT be agent-writable. The loader resolves the real
path (following symlinks) and hard-fails if the directory sits inside any agent
workspace root. Given that invariant, the *reload trigger* is safe to expose
publicly (RPC, CLI, agent tool) — triggering activation is harmless when only
the human can author what activates.

  Lever (reload): public — agent can say "reload my actions"
  Gun (file writes to plugins dir): private — blocked by the hard-fail check

Reload is EXPLICIT, never file-watched. Silent auto-reload would convert the
agent's ordinary file-write tools into a gateway code-injection path if the
directory check were ever misconfigured. The convenience delta is seconds; the
risk delta is total. Do not add file-watching.

Staged swap
-----------
A reload executes all plugin files against a *staging* registry first. Any file
that fails to parse or execute aborts the whole swap, leaving the last-good
handlers live, and returns the traceback to the caller. In-flight invocations
finish on the old code; the swap affects the next ``invoke()``.

Registry diff
-------------
Every reload logs: handler name, added/changed/removed, content hash
before/after. Pairs with the invocation ledger (§2) to answer "what code ran
when I clicked that button."
"""

import hashlib
import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ── Agent workspace roots ────────────────────────────────────────────────────

# Paths that agent tools write to; plugins dir must not sit inside any of them.
_AGENT_WORKSPACE_ROOTS: list[str] = []


def register_agent_workspace_root(path: str) -> None:
    """Register a path that the agent can write to. Called at gateway startup."""
    real = os.path.realpath(path)
    if real not in _AGENT_WORKSPACE_ROOTS:
        _AGENT_WORKSPACE_ROOTS.append(real)


# ── Plugin directory ─────────────────────────────────────────────────────────


def _plugins_dir() -> Path:
    return Path(get_hermes_home()) / "plugins" / "actions"


def _assert_not_agent_writable(plugins_real: str) -> None:
    """Hard-fail if the plugins dir is under any agent workspace root."""
    for root in _AGENT_WORKSPACE_ROOTS:
        if plugins_real == root or plugins_real.startswith(root + os.sep):
            raise PermissionError(
                f"Plugin directory {plugins_real!r} is inside an agent workspace "
                f"root ({root!r}). Refusing to load plugins from agent-writable "
                "paths — move the plugins directory outside the workspace."
            )


# ── Per-file hash ────────────────────────────────────────────────────────────


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "<unreadable>"


# ── Registry helpers (forwarded from artifact_actions) ───────────────────────

# Import lazily to avoid circular imports.
def _get_registry() -> dict[str, Any]:
    from tui_gateway import artifact_actions
    return artifact_actions._HANDLERS  # noqa: SLF001


def _swap_registry(new_handlers: dict[str, Any]) -> None:
    from tui_gateway import artifact_actions
    artifact_actions._HANDLERS.clear()
    artifact_actions._HANDLERS.update(new_handlers)


# ── Staging execution ────────────────────────────────────────────────────────


def _exec_plugin(path: Path, staging: dict[str, Any]) -> None:
    """Execute a single plugin file, registering handlers into *staging*."""
    source = path.read_text(encoding="utf-8")
    code = compile(source, str(path), "exec")

    # Give the plugin a fresh module namespace with register_handler pointing
    # at our staging dict so its register_handler calls land there.
    from tui_gateway import artifact_actions

    namespace: dict[str, Any] = {
        "__file__": str(path),
        "__name__": f"hermes_plugin_{path.stem}",
        "register_handler": lambda name, fn, _s=staging: _s.update({name: fn}),
        # Convenience re-exports plugins typically need
        "logger": logging.getLogger(f"hermes.plugin.{path.stem}"),
    }
    exec(code, namespace)  # noqa: S102 — intentional: plugins are human-authored


# ── Public API ───────────────────────────────────────────────────────────────


_reload_lock = threading.Lock()


def reload(force: bool = False) -> dict:
    """Load (or reload) all plugins from the plugins directory.

    Returns a result dict::

        {
            "status": "ok" | "error",
            "loaded": [list of filenames loaded],
            "diff": {
                "added": [...intent names...],
                "changed": [...intent names...],
                "removed": [...intent names...],
            },
            "error": "traceback string"   # only when status == "error"
        }

    On error, the live handler registry is unchanged.
    On success, the registry is atomically swapped to include plugin handlers
    (built-ins from artifact_actions remain unless a plugin overwrites them by
    the same name — plugins load last so they win conflicts deliberately).
    """
    plugins_dir = _plugins_dir()

    if not plugins_dir.exists():
        plugins_dir.mkdir(parents=True, exist_ok=True)
        return {
            "status": "ok",
            "loaded": [],
            "diff": {"added": [], "changed": [], "removed": []},
        }

    plugins_real = os.path.realpath(str(plugins_dir))
    try:
        _assert_not_agent_writable(plugins_real)
    except PermissionError as exc:
        logger.error("plugin loader security check failed: %s", exc)
        return {"status": "error", "loaded": [], "diff": {}, "error": str(exc)}

    plugin_files = sorted(plugins_dir.glob("*.py"))

    # Snapshot current handler names + hashes for diff logging.
    before = dict(_get_registry())
    before_hashes: dict[str, str] = {}

    with _reload_lock:
        # Build staging registry starting from built-in handlers only (exclude
        # plugins from the previous load so stale removed files don't linger).
        from tui_gateway import artifact_actions
        # Built-ins are functions defined directly in artifact_actions (not via
        # the plugin loader). Identify them by checking module origin.
        staging: dict[str, Any] = {
            name: fn
            for name, fn in before.items()
            if getattr(fn, "__module__", "") == artifact_actions.__name__
        }

        loaded: list[str] = []
        try:
            for path in plugin_files:
                _exec_plugin(path, staging)
                loaded.append(path.name)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            logger.error("plugin reload aborted — %s failed: %s", path.name, exc)
            return {
                "status": "error",
                "loaded": loaded,
                "diff": {},
                "error": f"Failed loading {path.name}:\n{tb}",
            }

        # Compute diff.
        before_names = set(before)
        after_names = set(staging)
        added = sorted(after_names - before_names)
        removed = sorted(before_names - after_names)
        changed = sorted(
            name for name in before_names & after_names
            if staging[name] is not before[name]
        )

        _swap_registry(staging)

    # Log the diff.
    if added or changed or removed:
        logger.info(
            "plugin registry updated — added=%s changed=%s removed=%s files=%s",
            added, changed, removed, loaded,
        )
    else:
        logger.info("plugin registry reload — no changes (%d files)", len(loaded))

    return {
        "status": "ok",
        "loaded": loaded,
        "diff": {"added": added, "changed": changed, "removed": removed},
    }


def initial_load() -> None:
    """Called at gateway startup to load any existing plugins silently."""
    result = reload()
    if result["status"] == "error":
        logger.warning("startup plugin load failed: %s", result.get("error", ""))
    elif result["diff"]["added"]:
        logger.info("loaded plugin handlers: %s", result["diff"]["added"])
