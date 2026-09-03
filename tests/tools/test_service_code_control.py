"""Code-controlled services must prove a merged revision before process launch.

The verifier exercises a real temporary Git repository while the GitHub response
is injected. Terminal integration pins the fail-closed boundary: a rejected
proof cannot reach the process registry, and verified evidence is the same
object persisted and rendered in the service graph.
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cron.jobs import build_cron_graph, normalize_service_declaration
from tools.service_code_control import (
    normalize_service_code_control,
    verify_service_code_control,
)


REVISION = "a" * 40


def _policy(**overrides):
    policy = {
        "provider": "github",
        "repository": "owner/service",
        "base_branch": "main",
        "revision": REVISION,
        "remote": "origin",
    }
    policy.update(overrides)
    return policy


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def _real_repository(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "Test")
    (work / "service.py").write_text("print('ready')\n", encoding="utf-8")
    _git(work, "add", "service.py")
    _git(work, "commit", "-q", "-m", "service")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work, _git(work, "rev-parse", "HEAD")


class TestPolicyNormalization:
    def test_accepts_only_fixed_github_pr_policy(self):
        assert normalize_service_code_control(_policy()) == _policy()

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("provider", "gitlab", "provider"),
            ("repository", "not-a-repo", "repository"),
            ("base_branch", "", "base_branch"),
            ("revision", "abc123", "40-character"),
            ("remote", "bad remote", "remote"),
        ],
    )
    def test_rejects_weak_or_ambiguous_policy(self, field, value, message):
        with pytest.raises(ValueError, match=message):
            normalize_service_code_control(_policy(**{field: value}))

    def test_service_declaration_carries_policy_without_freeform_escape_hatches(self):
        declaration = normalize_service_declaration(
            "API", "serves requests", code_control=_policy()
        )
        assert declaration["code_control"] == _policy()
        assert set(declaration["code_control"]) == {
            "provider", "repository", "base_branch", "revision", "remote"
        }


class TestMergedRevisionVerification:
    def test_real_git_checkout_and_merged_pr_produce_sealed_evidence(self, tmp_path):
        work, revision = _real_repository(tmp_path)
        policy = _policy(revision=revision)

        def github_get(repository, candidate_revision):
            assert repository == "owner/service"
            assert candidate_revision == revision
            return [{
                "number": 42,
                "html_url": "https://github.com/owner/service/pull/42",
                "merged_at": "2026-09-03T12:00:00Z",
                "merge_commit_sha": revision,
                "base": {"ref": "main"},
            }]

        evidence = verify_service_code_control(
            policy, source_root=work, github_get=github_get
        )

        assert evidence == {
            "status": "verified",
            "enforcement": "merged-pull-request",
            "provider": "github",
            "repository": "owner/service",
            "base_branch": "main",
            "revision": revision,
            "pull_request": {
                "number": 42,
                "url": "https://github.com/owner/service/pull/42",
                "merged_at": "2026-09-03T12:00:00Z",
            },
        }

    def test_dirty_checkout_fails_before_github_lookup(self, tmp_path):
        work, revision = _real_repository(tmp_path)
        (work / "service.py").write_text("changed\n", encoding="utf-8")
        called = False

        def github_get(*_args):
            nonlocal called
            called = True
            return []

        with pytest.raises(ValueError, match="clean checkout"):
            verify_service_code_control(
                _policy(revision=revision), source_root=work, github_get=github_get
            )
        assert called is False

    def test_unmerged_revision_fails_closed(self, tmp_path):
        work, revision = _real_repository(tmp_path)
        with pytest.raises(ValueError, match="merged pull request"):
            verify_service_code_control(
                _policy(revision=revision), source_root=work, github_get=lambda *_: []
            )

    def test_local_only_revision_fails_before_github_lookup(self, tmp_path):
        work, _ = _real_repository(tmp_path)
        (work / "service.py").write_text("print('new')\n", encoding="utf-8")
        _git(work, "add", "service.py")
        _git(work, "commit", "-q", "-m", "not pushed")
        revision = _git(work, "rev-parse", "HEAD")
        called = False

        def github_get(*_args):
            nonlocal called
            called = True
            return []

        with pytest.raises(ValueError, match="not present on the fetched remote base"):
            verify_service_code_control(
                _policy(revision=revision), source_root=work, github_get=github_get
            )
        assert called is False

    @pytest.mark.parametrize(
        "pull_override",
        [
            {"base": {"ref": "release"}},
            {"merge_commit_sha": "b" * 40},
            {"merged_at": None},
        ],
    )
    def test_wrong_pr_target_or_revision_fails_closed(self, tmp_path, pull_override):
        work, revision = _real_repository(tmp_path)
        pull = {
            "number": 42,
            "html_url": "https://github.com/owner/service/pull/42",
            "merged_at": "2026-09-03T12:00:00Z",
            "merge_commit_sha": revision,
            "base": {"ref": "main"},
        }
        pull.update(pull_override)
        with pytest.raises(ValueError, match="merged pull request"):
            verify_service_code_control(
                _policy(revision=revision),
                source_root=work,
                github_get=lambda *_: [pull],
            )


class TestTerminalLaunchGate:
    @staticmethod
    def _configure_local_terminal(monkeypatch, terminal_tool, tmp_path):
        monkeypatch.setattr(
            terminal_tool, "_get_env_config", lambda: {
                "env_type": "local", "cwd": str(tmp_path), "timeout": 30,
                "docker_image": "", "singularity_image": "",
                "modal_image": "", "daytona_image": "",
            }
        )
        monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda _task: None)
        monkeypatch.setattr(terminal_tool, "_resolve_task_host_cwd", lambda *_: None)
        monkeypatch.setattr(terminal_tool, "resolve_task_overrides", lambda _task: {})
        monkeypatch.setitem(terminal_tool._active_environments, "default", SimpleNamespace(env={}))
        monkeypatch.setitem(terminal_tool._last_activity, "default", 0.0)

    def test_rejected_proof_never_spawns_process(self, monkeypatch, tmp_path):
        import tools.terminal_tool as terminal_tool

        self._configure_local_terminal(monkeypatch, terminal_tool, tmp_path)

        with (
            patch("tools.service_code_control.verify_service_code_control", side_effect=ValueError("not merged")),
            patch("tools.process_registry.process_registry.spawn_local") as spawn,
        ):
            result = json.loads(terminal_tool.terminal_tool(
                command="python service.py",
                background=True,
                workdir=str(tmp_path),
                service_name="API",
                service_description="serves requests",
                service_code_control=_policy(),
            ))

        assert result["status"] == "error"
        assert "code control rejected" in result["error"]
        spawn.assert_not_called()

    def test_verified_evidence_is_attached_before_spawn(self, monkeypatch, tmp_path):
        import tools.terminal_tool as terminal_tool

        self._configure_local_terminal(monkeypatch, terminal_tool, tmp_path)
        evidence = {
            "status": "verified",
            "enforcement": "merged-pull-request",
            "provider": "github",
            "repository": "owner/service",
            "base_branch": "main",
            "revision": REVISION,
            "pull_request": {
                "number": 42,
                "url": "https://github.com/owner/service/pull/42",
                "merged_at": "2026-09-03T12:00:00Z",
            },
        }
        with (
            patch(
                "tools.service_code_control.verify_service_code_control",
                return_value=evidence,
            ),
            patch(
                "tools.process_registry.process_registry.spawn_local",
                return_value=SimpleNamespace(id="proc_verified", pid=1234),
            ) as spawn,
        ):
            result = json.loads(terminal_tool.terminal_tool(
                command="python service.py",
                background=True,
                workdir=str(tmp_path),
                service_name="API",
                service_description="serves requests",
                service_code_control=_policy(),
            ))

        assert result["error"] is None
        declaration = spawn.call_args.kwargs["service_declaration"]
        assert declaration["code_control_evidence"] == evidence


class TestGraphEvidence:
    def test_terminal_schema_exposes_closed_code_control_contract(self):
        from tools.terminal_tool import TERMINAL_SCHEMA

        schema = TERMINAL_SCHEMA["parameters"]["properties"]["service_code_control"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["provider"]["enum"] == ["github"]
        assert set(schema["required"]) == {
            "provider", "repository", "base_branch", "revision"
        }

    def test_graph_renders_repository_pr_and_revision_from_verified_evidence(self):
        evidence = {
            "status": "verified",
            "enforcement": "merged-pull-request",
            "provider": "github",
            "repository": "owner/service",
            "base_branch": "main",
            "revision": REVISION,
            "pull_request": {
                "number": 42,
                "url": "https://github.com/owner/service/pull/42",
                "merged_at": "2026-09-03T12:00:00Z",
            },
        }
        graph = build_cron_graph(jobs=[], services=[{
            "id": "proc_api",
            "label": "API",
            "description": "serves requests",
            "inputs": [], "outputs": [], "side_effects": [],
            "code_control": evidence,
        }])
        node = next(node for node in graph["nodes"] if node["id"] == "proc_api")
        assert node["code_control"] == evidence
        edges = {(edge["source"], edge["target"], edge["type"]) for edge in graph["edges"]}
        assert ("proc_api", "github:owner/service", "source_repository") in edges
        assert ("proc_api", "pr:owner/service#42", "released_via") in edges
        assert ("proc_api", f"git:{REVISION}", "runs_revision") in edges
