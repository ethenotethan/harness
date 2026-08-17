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
"""

import ast
import builtins
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
