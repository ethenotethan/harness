#!/usr/bin/env python3
"""Architecture ratchet: a service's declared constraints may only tighten.

The compiler proves invariants per compile (``components-assign-every-file``,
``routes-declared-literally``, ``store-calls-resolve`` …) and the config
declares what the map must contain (components, external systems, groups,
gates, project rules, declared constructions and invariants). All of that was
open to the same change that needed it: an agent blocked by a violated
invariant could delete it, exclude the directory, drop the external system or
remove the gate command, in the PR that needed to pass. This guard closes that
surface the way the fork-diff gate closes ``fork.yaml``: compile the tree as it
is on the base branch (a temporary worktree) and the working tree with the
same compiler, and refuse anything looser.

What "looser" means:

  the compiled model
      a proven or declared invariant removed or re-kinded; a proven invariant
      that held on the base and is violated now; a completeness invariant
      whose unparsed sites grew (the new sites are named); a component gone.
  architecture/config.json
      an external system, external group, gate command, project rule, declared
      node, edge or flow removed; ``exclude`` grown; ``languages`` narrowed.
  the semantic layer, when the service carries one
      a record or flow stale now that was fresh on the base. A model without a
      ``semantic`` section has nothing to check here.

Tightening — a new invariant, a new component, fewer unparsed sites, a new
system — always passes. A deliberate loosening (retiring an invariant after a
redesign) carries the ``constraints-loosened`` label, which the workflow turns
into ``--allow-loosening``: the guard reports every loosening and exits 0, so
it is explicit in the PR and in the log rather than silent in a diff.

Usage:
    python3 -m tools.architecture_ratchet [--root DIR] [BASE_REF] [--allow-loosening]

A tree without ``architecture/config.json`` has no constraints to ratchet; a
base without one is a service adopting the compiler, which is itself a
tightening.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.architecture_compile_python import CONFIG_PATH, CompileError  # noqa: E402
from tools.architecture_compiler import compile_service  # noqa: E402


# ── Trees ────────────────────────────────────────────────────────────────────


def compile_tree(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    """(model, raw config, error) for a tree: (None, None, None) when it has no
    config; (None, config, message) when it has one that does not compile."""
    config_path = root / CONFIG_PATH
    if not config_path.is_file():
        return None, None, None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, None, f"{CONFIG_PATH} is not valid JSON: {exc}"
    try:
        return compile_service(root), config, None
    except CompileError as exc:
        return None, config, str(exc)


def base_tree(root: Path, base_ref: str) -> Tuple[Optional[Path], Optional[tempfile.TemporaryDirectory]]:
    """The base branch checked out into a temporary worktree, or (None, None)
    when git cannot provide it (no repository, unknown ref)."""
    holder = tempfile.TemporaryDirectory(prefix="architecture-ratchet-")
    worktree = Path(holder.name) / "base"
    result = subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", "--quiet", str(worktree), base_ref],
                            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        holder.cleanup()
        return None, None
    return worktree, holder


def release_tree(root: Path, worktree: Optional[Path], holder: Optional[tempfile.TemporaryDirectory]) -> None:
    if worktree is not None:
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)], capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if holder is not None:
        holder.cleanup()


# ── Comparison ───────────────────────────────────────────────────────────────


def _ids(items: Any, key: str = "id") -> set:
    return {str(item[key]) for item in items or [] if isinstance(item, dict) and key in item}


def _sites(invariant: Dict[str, Any]) -> set:
    return {(u.get("path"), u.get("line"), u.get("method") or u.get("client_path") or u.get("package") or u.get("rule") or "") for u in invariant.get("unparsed") or []}


def compare_models(base: Dict[str, Any], head: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    base_invariants = {i["id"]: i for i in base["interplay"]["invariants"]}
    head_invariants = {i["id"]: i for i in head["interplay"]["invariants"]}
    for invariant_id, before in base_invariants.items():
        after = head_invariants.get(invariant_id)
        where = f"invariant {invariant_id!r}"
        if after is None:
            problems.append(f"{where} was removed")
            continue
        if after.get("kind") != before.get("kind"):
            problems.append(f"{where} changed kind {before.get('kind')!r} → {after.get('kind')!r}")
        if before.get("status") == "holds" and after.get("status") == "violated":
            problems.append(f"{where} held on the base and is violated now")
        grown = sorted(_sites(after) - _sites(before), key=lambda s: (str(s[0]), int(s[1] or 0), str(s[2])))
        if len(after.get("unparsed") or []) > len(before.get("unparsed") or []):
            named = ", ".join(f"{path}:{line} ({what})" if what else f"{path}:{line}" for path, line, what in grown[:5])
            more = f" … and {len(grown) - 5} more" if len(grown) > 5 else ""
            problems.append(f"{where}: unparsed sites grew {len(before.get('unparsed') or [])} → {len(after.get('unparsed') or [])}: {named}{more}")
    for component_id in sorted(_ids(base["components"]) - _ids(head["components"])):
        problems.append(f"component {component_id!r} was removed")
    semantic_base = base.get("semantic") if isinstance(base.get("semantic"), dict) else {}
    semantic_head = head.get("semantic") if isinstance(head.get("semantic"), dict) else {}
    for section in ("records", "flows"):
        before_stale = {str(r.get("id")) for r in semantic_base.get(section) or [] if isinstance(r, dict) and r.get("stale")}
        after_stale = sorted(str(r.get("id")) for r in semantic_head.get(section) or [] if isinstance(r, dict) and r.get("stale"))
        fresh_then = [r for r in after_stale if r not in before_stale]
        if fresh_then:
            problems.append(f"semantic {section}: {len(fresh_then)} went stale and were not refreshed: " + ", ".join(fresh_then[:8]) + (" …" if len(fresh_then) > 8 else ""))
    return problems


def compare_configs(base: Dict[str, Any], head: Dict[str, Any]) -> List[str]:
    problems: List[str] = []

    def removed(label: str, before: set, after: set) -> None:
        for item in sorted(before - after):
            problems.append(f"{CONFIG_PATH}: {label} {item!r} was removed")

    removed("external system", _ids(base.get("external_systems")), _ids(head.get("external_systems")))
    removed("external group", _ids(base.get("external_groups")), _ids(head.get("external_groups")))
    removed("gate command", _ids((base.get("gates") or {}).get("commands")), _ids((head.get("gates") or {}).get("commands")))
    removed("project rule", _ids(base.get("rules")), _ids(head.get("rules")))
    base_declared = base.get("declared") or {}
    head_declared = head.get("declared") or {}
    removed("declared node", _ids(base_declared.get("nodes")), _ids(head_declared.get("nodes")))
    removed("declared flow", _ids(base_declared.get("flows")), _ids(head_declared.get("flows")))
    removed("declared invariant", _ids(base_declared.get("invariants")), _ids(head_declared.get("invariants")))
    edge_key = lambda e: f"{e.get('source')} -{e.get('relation')}-> {e.get('target')}"  # noqa: E731
    removed("declared edge", {edge_key(e) for e in base_declared.get("edges") or [] if isinstance(e, dict)},
            {edge_key(e) for e in head_declared.get("edges") or [] if isinstance(e, dict)})
    grown = sorted(set(map(str, head.get("exclude") or [])) - set(map(str, base.get("exclude") or [])))
    if grown:
        problems.append(f"{CONFIG_PATH}: exclude grew by {len(grown)} pattern(s): " + ", ".join(grown))
    if base.get("languages") is not None and head.get("languages") is not None:
        narrowed = sorted(set(map(str, base["languages"])) - set(map(str, head["languages"])))
        if narrowed:
            problems.append(f"{CONFIG_PATH}: languages narrowed; dropped " + ", ".join(narrowed))
    elif base.get("languages") is None and head.get("languages") is not None:
        problems.append(f"{CONFIG_PATH}: languages was every pack and is now restricted to " + ", ".join(map(str, head["languages"])))
    return problems


# ── Driver ───────────────────────────────────────────────────────────────────


def evaluate(root: Path, base_ref: str) -> Tuple[List[str], str]:
    """(problems, note): every loosening of the working tree relative to the
    base ref, and a one-line account of what was compared."""
    head_model, head_config, head_error = compile_tree(root)
    if head_config is None:
        return [], f"no {CONFIG_PATH} in the working tree; nothing to ratchet"
    if head_model is None:
        return [f"the working tree does not compile, so nothing can be judged: {head_error}"], "head failed to compile"
    worktree, holder = base_tree(root, base_ref)
    try:
        if worktree is None:
            return [], f"{base_ref} is not available as a worktree; nothing to compare against"
        base_model, base_config, base_error = compile_tree(worktree)
    finally:
        release_tree(root, worktree, holder)
    if base_config is None:
        return [], f"{base_ref} has no {CONFIG_PATH}: the service is adopting the compiler"
    if base_model is None:
        return [], f"{base_ref} does not compile ({base_error}); the working tree does, which is the tightening"
    problems = compare_models(base_model, head_model) + compare_configs(base_config, head_config)
    return problems, f"compared {len(head_model['interplay']['invariants'])} invariant(s) and the declarations against {base_ref}"


def main(argv: Sequence[str]) -> int:
    args = list(argv)
    allow = "--allow-loosening" in args
    root = Path(".")
    if "--root" in args:
        at = args.index("--root")
        root = Path(args[at + 1])
        del args[at:at + 2]
    positional = [a for a in args if not a.startswith("--")]
    base_ref = positional[0] if positional else "origin/main"
    root = root.resolve()
    try:
        problems, note = evaluate(root, base_ref)
    except ImportError as exc:  # pragma: no cover - environment, not logic
        print(f"architecture ratchet: the compiler needs tree-sitter ({exc}); pip install -r tools/architecture_compiler/requirements.txt", file=sys.stderr)
        return 1
    if not problems:
        print(f"Architecture ratchet: no constraint is looser than {base_ref} ({note}).")
        return 0
    print(f"Architecture ratchet: {len(problems)} loosening(s) relative to {base_ref} ({note}):")
    for problem in problems:
        print(f"  - {problem}")
    if allow:
        print("Allowed: this change carries the constraints-loosened label; the loosening is explicit, not silent.")
        return 0
    print("\nA constraint may only tighten in an ordinary change. To loosen one deliberately, say why in the PR and add the "
          "`constraints-loosened` label; the guard then passes with the loosening on record.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
