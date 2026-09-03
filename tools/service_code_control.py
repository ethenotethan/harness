"""Enforce merged-pull-request provenance for managed service launches.

A declaration is policy, not evidence. This module verifies the local checkout,
remote base ancestry, and GitHub's merged pull-request record before a service
process may start, then returns the sealed evidence exposed in the context graph.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^(?!/)(?!.*(?:\.\.|//|@\{|\\))[A-Za-z0-9._/-]+(?<![/.])$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_POLICY_KEYS = {"provider", "repository", "base_branch", "revision", "remote"}


def normalize_service_code_control(value: Any) -> Dict[str, str]:
    """Validate the one supported fail-closed service release policy."""
    if not isinstance(value, dict):
        raise ValueError("service code control must be an object")
    unexpected = set(value) - _POLICY_KEYS
    if unexpected:
        raise ValueError(
            "service code control has unsupported keys: " + ", ".join(sorted(unexpected))
        )

    provider = value.get("provider")
    repository = value.get("repository")
    base_branch = value.get("base_branch")
    revision = value.get("revision")
    remote = value.get("remote", "origin")
    if provider != "github":
        raise ValueError("service code control provider must be 'github'")
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise ValueError("service code control repository must be 'owner/name'")
    if not isinstance(base_branch, str) or not _BRANCH_RE.fullmatch(base_branch):
        raise ValueError("service code control base_branch is invalid")
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise ValueError("service code control revision must be a lowercase 40-character SHA")
    if not isinstance(remote, str) or not _REMOTE_RE.fullmatch(remote):
        raise ValueError("service code control remote is invalid")
    return {
        "provider": provider,
        "repository": repository,
        "base_branch": base_branch,
        "revision": revision,
        "remote": remote,
    }


def _run_git(source_root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(source_root), *args],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=30,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "output", "") or str(exc)
        raise ValueError(f"git verification failed: {detail.strip()}") from exc


def _github_pull_requests(repository: str, revision: str) -> List[Dict[str, Any]]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/commits/{revision}/pulls",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "hermes-service-code-control",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise ValueError(f"GitHub pull-request verification failed: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("GitHub pull-request verification returned a non-list response")
    return [item for item in payload if isinstance(item, dict)]


def verify_service_code_control(
    value: Any,
    *,
    source_root: Path | str,
    github_get: Callable[[str, str], List[Dict[str, Any]]] = _github_pull_requests,
) -> Dict[str, Any]:
    """Return verified evidence or raise before any service process is spawned."""
    policy = normalize_service_code_control(value)
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"service source root does not exist: {root}")

    status = _run_git(root, "status", "--porcelain", "--untracked-files=normal")
    if status:
        raise ValueError("service code control requires a clean checkout")
    head = _run_git(root, "rev-parse", "HEAD")
    if head != policy["revision"]:
        raise ValueError(
            f"service checkout HEAD {head} does not match declared revision "
            f"{policy['revision']}"
        )

    _run_git(root, "fetch", "--quiet", policy["remote"], policy["base_branch"])
    try:
        subprocess.run(
            [
                "git", "-C", str(root), "merge-base", "--is-ancestor",
                policy["revision"], "FETCH_HEAD",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "declared revision is not present on the fetched remote base branch"
        ) from exc

    pull_requests = github_get(policy["repository"], policy["revision"])
    merged = next(
        (
            pull
            for pull in pull_requests
            if pull.get("merged_at")
            and pull.get("merge_commit_sha") == policy["revision"]
            and isinstance(pull.get("base"), dict)
            and pull["base"].get("ref") == policy["base_branch"]
            and isinstance(pull.get("number"), int)
            and isinstance(pull.get("html_url"), str)
        ),
        None,
    )
    if merged is None:
        raise ValueError(
            "declared revision is not the merge commit of a merged pull request "
            f"into {policy['repository']}:{policy['base_branch']}"
        )

    return {
        "status": "verified",
        "enforcement": "merged-pull-request",
        "provider": "github",
        "repository": policy["repository"],
        "base_branch": policy["base_branch"],
        "revision": policy["revision"],
        "pull_request": {
            "number": merged["number"],
            "url": merged["html_url"],
            "merged_at": merged["merged_at"],
        },
    }
