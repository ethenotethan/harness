"""The architecture ratchet: a service's constraints may only tighten against the base.

Each test builds a scratch git repository holding a small service, commits it as
the base, mutates the working tree and asserts the guard names exactly the
loosening — and stays quiet on tightening."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter", reason="the architecture compiler needs tree-sitter (pip install -r tools/architecture_compiler/requirements.txt)")
pytest.importorskip("tree_sitter_language_pack", reason="the architecture compiler needs tree-sitter-language-pack")

from tools import architecture_ratchet as ratchet  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

STORE = '''import json

class SessionStore:
    def save(self, sessions):
        with open("sessions.json", "w") as handle:
            json.dump(sessions, handle)
'''
API = '''import requests
from flask import Flask
from app.store import SessionStore

app = Flask(__name__)
_store = SessionStore()

@app.get("/api/sessions")
def list_sessions():
    _store.save({})
    return "ok"
'''
CONFIG = {
    "title": "Fixture", "description": "A fixture service.", "repository": None,
    "components": [{"id": "core", "label": "Core", "description": "Everything.", "patterns": ["**"]}],
    "gates": {"commands": [{"id": "check", "name": "Model current", "command": "python3 -m tools.architecture_compiler . --check"}]},
    "external_systems": [{"id": "flask", "label": "Flask", "category": "web", "description": "The framework.", "signatures": [r"\bflask\b"]}],
    "declared": {"invariants": [{"id": "sessions-bounded", "kind": "sessions_bounded", "why": "The store never holds more than 100 sessions."}]},
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_config(root: Path, config: dict) -> None:
    write(root, "architecture/config.json", json.dumps(config, indent=2))


@pytest.fixture
def service(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "app/store.py", STORE)
    write(root, "app/api.py", API)
    write(root, "app/extra.py", "VERSION = 1\n")
    write_config(root, CONFIG)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def run(root: Path, *extra: str) -> tuple[int, str]:
    result = subprocess.run([sys.executable, "-m", "tools.architecture_ratchet", "--root", str(root), "HEAD", *extra], cwd=REPO, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def test_an_unchanged_or_tightened_tree_passes(service: Path):
    problems, note = ratchet.evaluate(service, "HEAD")
    assert problems == [] and "compared" in note
    config = json.loads(json.dumps(CONFIG))
    config["declared"]["invariants"].append({"id": "one-store", "kind": "one_store", "why": "One store class."})
    config["external_systems"].append({"id": "http", "label": "requests", "category": "http", "description": "d", "signatures": [r"\brequests\b"]})
    config["components"].insert(0, {"id": "api", "label": "API", "description": "Routes.", "patterns": ["app/api.py"]})
    write_config(service, config)
    assert ratchet.evaluate(service, "HEAD")[0] == [], "a new invariant, a newly claimed package and a split component are tightenings"
    code, output = run(service)
    assert code == 0 and "no constraint is looser" in output


def test_loosenings_are_named_and_refused_unless_labelled(service: Path):
    config = json.loads(json.dumps(CONFIG))
    config["declared"]["invariants"] = []
    config["external_systems"] = []
    config["exclude"] = ["app/extra.py"]
    write_config(service, config)
    write(service, "app/api.py", API.replace('@app.get("/api/sessions")', 'PREFIX = "/api"\n@app.get(PREFIX + "/sessions")').replace("_store.save({})", "_store.save({})\n    _store.purge()"))
    problems, _ = ratchet.evaluate(service, "HEAD")
    assert "invariant 'sessions-bounded' was removed" in problems
    assert "architecture/config.json: declared invariant 'sessions-bounded' was removed" in problems
    assert "architecture/config.json: external system 'flask' was removed" in problems
    assert "architecture/config.json: exclude grew by 1 pattern(s): app/extra.py" in problems
    assert "invariant 'routes-declared-literally' held on the base and is violated now" in problems
    assert any(p.startswith("invariant 'routes-declared-literally': unparsed sites grew 0 → 1: app/api.py:9") for p in problems), problems
    assert "invariant 'store-calls-resolve' held on the base and is violated now" in problems
    assert any("store-calls-resolve" in p and "app/api.py:12 (purge)" in p for p in problems), problems
    assert any(p.startswith("invariant 'vendors-declared': unparsed sites grew 1 → 2: app/api.py:2 (flask)") for p in problems), "flask is no longer claimed by a declared system"
    code, output = run(service)
    assert code == 1 and "loosening(s) relative to HEAD" in output and "constraints-loosened" in output
    code, output = run(service, "--allow-loosening")
    assert code == 0 and "Allowed" in output and "sessions-bounded" in output


def test_trees_without_a_config_have_nothing_to_ratchet(service: Path):
    (service / "architecture/config.json").unlink()
    problems, note = ratchet.evaluate(service, "HEAD")
    assert problems == [] and "nothing to ratchet" in note
    # A base without a config is a service adopting the compiler: the head's constraints are all new.
    write_config(service, CONFIG)
    git(service, "rm", "-q", "--cached", "architecture/config.json")
    git(service, "commit", "-q", "-m", "no config")
    problems, note = ratchet.evaluate(service, "HEAD")
    assert problems == [] and "adopting" in note


def test_a_head_that_does_not_compile_is_reported_not_judged(service: Path):
    config = json.loads(json.dumps(CONFIG))
    config["external_systems"].append({"id": "ghost", "label": "Ghost", "category": "backend", "description": "d", "signatures": [r"\bGhostSDK\b"]})
    write_config(service, config)
    problems, _ = ratchet.evaluate(service, "HEAD")
    assert len(problems) == 1 and problems[0].startswith("the working tree does not compile") and "ghost" in problems[0]


def test_semantic_layer_freshness_is_checked_only_when_present():
    base = {"interplay": {"invariants": []}, "components": []}
    head = json.loads(json.dumps(base))
    assert ratchet.compare_models(base, head) == [], "no semantic section: nothing to say"
    base["semantic"] = {"records": [{"id": "a", "stale": True}, {"id": "b"}], "flows": [{"id": "f"}]}
    head["semantic"] = {"records": [{"id": "a", "stale": True}, {"id": "b", "stale": True}], "flows": [{"id": "f", "stale": True}]}
    problems = ratchet.compare_models(base, head)
    assert problems == ["semantic records: 1 went stale and were not refreshed: b", "semantic flows: 1 went stale and were not refreshed: f"]
