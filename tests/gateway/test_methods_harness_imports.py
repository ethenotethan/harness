"""Every name the split handler modules reference must actually resolve.

Regression guard for the class of bug that killed every wiki page: the
upstream rebase's handler split carried wiki handlers into
``methods_harness.py`` without their ``wiki_api`` imports. Python resolves
function-body names at CALL time, so the module imported cleanly and nothing
failed until the first ``wiki.scan`` — which died with ``NameError: name
'resolve_wiki' is not defined`` and the desktop showed "Failed to load page"
on every wiki page.

This test finds every bare name loaded inside a function body of each split
module and asserts it resolves against the module's own globals, its inline
(function-local) imports, Python builtins, or ``tui_gateway.server``'s
globals (handler bodies are rebound onto server's namespace at install time
— see ``method_ctx.py`` — so server globals are legitimately reachable).
A name none of those provide is exactly the wiki bug waiting for its first
caller.

That static check is deliberately GENEROUS: it accepts a name that only the
split module's own module-level imports provide. At runtime that guarantee
does not hold — ``HandlerRegistry.install`` rebinds every handler onto
``tui_gateway.server``'s namespace with ``types.FunctionType``, so a handler
body sees server.py's globals, builtins, and its own in-body imports, and
NOTHING from the module it was written in. ``wiki.list`` shipped broken
(``RPC error [5052]: name 'wiki_list' is not defined``) with the static test
green because ``wiki_list`` was a module-level import of methods_harness.py.
``test_handler_globals_resolve_in_rebound_namespace`` closes that gap: it
rebinds each handler exactly the way the server does and asserts every
global the bytecode loads (recursively through nested code objects) resolves
in the REBOUND namespace. In-body imports bind locals (``STORE_FAST``), so
they never show up as ``LOAD_GLOBAL`` and need no allow-list.
"""

import ast
import builtins
import dis
import importlib
import types
from pathlib import Path

import pytest

_GATEWAY_DIR = Path(__file__).resolve().parents[2] / "tui_gateway"
_SPLIT_MODULES = sorted(_GATEWAY_DIR.glob("methods_*.py"))


def _module_scope_names(tree: ast.Module) -> set[str]:
    """Names bound at module scope: imports, assignments, defs, classes."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for n in ast.walk(target):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Try):
            # try/except ImportError fallback blocks bind in both arms.
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        names.add((alias.asname or alias.name).split(".")[0])
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        for n in ast.walk(target):
                            if isinstance(n, ast.Name):
                                names.add(n.id)
    return names


def _function_unresolved_names(func: ast.AST, module_names: set[str]) -> set[str]:
    """Bare Name loads in ``func`` that neither local bindings nor
    ``module_names`` nor builtins provide."""
    bound: set[str] = set()
    loads: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Lambda):
            args = node.args
            for a in (
                list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            ):
                bound.add(a.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            args = node.args
            for a in (
                list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            ):
                bound.add(a.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            else:
                loads.append(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    builtin_names = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    return {
        n for n in loads
        if n not in bound and n not in module_names and n not in builtin_names
    }


def _server_globals() -> set[str]:
    """Names bound at module scope in tui_gateway/server.py — the namespace
    handler bodies are rebound onto at install time."""
    tree = ast.parse((_GATEWAY_DIR / "server.py").read_text())
    return _module_scope_names(tree)


@pytest.mark.parametrize("module_path", _SPLIT_MODULES, ids=lambda p: p.name)
def test_handler_names_resolve(module_path: Path) -> None:
    tree = ast.parse(module_path.read_text())
    reachable = _module_scope_names(tree) | _server_globals()
    unresolved: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            missing = _function_unresolved_names(node, reachable)
            if missing:
                unresolved[f"{node.name}:{node.lineno}"] = missing
    assert not unresolved, (
        f"{module_path.name} references names that resolve nowhere — these are "
        f"NameErrors waiting for their first caller (the wiki.scan bug): "
        f"{unresolved}"
    )


# ── Runtime check: names must resolve in the namespace handlers actually run in ──

_BUILTIN_NAMES = set(dir(builtins))


def _loaded_global_names(code: types.CodeType) -> set[str]:
    """Every name ``code`` (and any nested code object — lambdas, closures,
    comprehensions) loads from its globals. ``LOAD_GLOBAL`` is exactly the
    lookup that goes to ``fn.__globals__`` then builtins; locals bound by an
    in-body ``import`` are ``LOAD_FAST`` and are correctly excluded."""
    names = {
        ins.argval
        for ins in dis.get_instructions(code)
        if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_FROM_DICT_OR_GLOBALS")
    }
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _loaded_global_names(const)
    return names


def _rebind_like_the_server(fn, server) -> types.FunctionType:
    """Mirror ``HandlerRegistry.install`` byte-for-byte (see method_ctx.py):
    same code object, server.py's namespace as globals."""
    rebound = types.FunctionType(
        fn.__code__, vars(server), fn.__name__, fn.__defaults__, fn.__closure__
    )
    rebound.__kwdefaults__ = fn.__kwdefaults__
    return rebound


@pytest.mark.parametrize(
    "module_name", [p.stem for p in _SPLIT_MODULES], ids=lambda n: f"{n}.py"
)
def test_handler_globals_resolve_in_rebound_namespace(module_name: str) -> None:
    """For every @method handler in a split module: rebind it exactly the way
    server.py's register() does, then assert every global its bytecode loads
    resolves in the rebound namespace (or builtins).

    This is the check that catches ``wiki.list`` — the handler called
    ``wiki_list()``, a module-level import of methods_harness.py that the
    static test above happily counted as resolved, but which does not exist
    in server.py's namespace where the handler really runs."""
    server = importlib.import_module("tui_gateway.server")
    mod = importlib.import_module(f"tui_gateway.{module_name}")
    registry = getattr(mod, "_registry", None)
    if registry is None:
        pytest.skip(f"{module_name} has no HandlerRegistry")
    pending = list(registry._pending)
    assert pending, f"{module_name} registers no handlers"

    # The module must be reachable through the real registration path, i.e.
    # server.py imported it and called register(); otherwise the rebinding we
    # mirror here is not what production does.
    for rpc_name, _fn in pending:
        assert rpc_name in server._methods, (
            f"{module_name}: {rpc_name!r} is not in server._methods — server.py "
            "never registered this module"
        )

    unresolved: dict[str, set[str]] = {}
    for rpc_name, fn in pending:
        rebound = _rebind_like_the_server(fn, server)
        missing = {
            n
            for n in _loaded_global_names(rebound.__code__)
            if n not in rebound.__globals__ and n not in _BUILTIN_NAMES
        }
        if missing:
            unresolved[f"{rpc_name} ({fn.__code__.co_filename.rsplit('/', 1)[-1]}:{fn.__code__.co_firstlineno})"] = missing

    assert not unresolved, (
        f"{module_name}.py handlers load globals that do not exist in the "
        "namespace they are rebound onto (tui_gateway.server) — each is a "
        "NameError on the first RPC, invisible at import time. Import the "
        f"name INSIDE the handler body instead of at module level: {unresolved}"
    )


def test_static_check_alone_would_have_missed_wiki_list() -> None:
    """Pin the reason the rebound-namespace test exists: a name provided ONLY by
    a split module's module-level import passes the static check yet is
    unreachable at runtime. Build that exact situation synthetically so the
    guard is documented by a failing example, independent of the fix."""
    server = importlib.import_module("tui_gateway.server")
    ns: dict[str, object] = {}
    exec(
        "from tui_gateway.wiki_api import wiki_list\n"
        "def handler(rid, params):\n"
        "    return wiki_list()\n",
        ns,
    )
    handler = ns["handler"]
    # In its own module it works...
    assert "wiki_list" in handler.__globals__
    # ...but rebound the way the server does, the name is gone.
    rebound = _rebind_like_the_server(handler, server)
    missing = {
        n
        for n in _loaded_global_names(rebound.__code__)
        if n not in rebound.__globals__ and n not in _BUILTIN_NAMES
    }
    assert missing == {"wiki_list"}
    with pytest.raises(NameError, match="wiki_list"):
        rebound(1, {})
