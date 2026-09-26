"""Architecture models per service: manifests, snapshots, checks.

A service that conforms to the architecture standard ships a compiler that emits
an *architecture model* (``architecture/model/model.json`` — components, the
system map, invariants with status, stores, externals, the CI plane) and a
``--check`` that fails when the committed model drifts from the tree. Harness
learns about such a service from a **manifest**: one JSON file per service under
``~/.hermes/services/architecture/<id>.json``. The manifest is the durable,
operator-owned object; everything else here is derived from it.

Two kinds of service share one manifest shape and one read path:

* **local** — a checkout Harness can reach (``root``). The model is read from the
  tree, the revision is the checkout's ``git`` head (or a content digest when
  there is no repository), and ``check`` — the compiler's own ``--check`` — can be
  run on demand. This is the never-pushed case; nothing leaves the machine.
* **github** — a repository (``repository`` + ``ref``). The model is fetched from
  the repository at that ref; checks come from the repository's own CI, so
  ``architecture.check`` reports them as unavailable here.

Manifest shape (``name`` and ``description`` required; ``root`` or ``repository``
required; the rest optional)::

    {
      "id": "portal",                                  # defaults to the file stem
      "name": "Portal",                                # display name
      "description": "Native macOS/iOS client …",      # markdown detail card
      "root": "/Users/me/Desktop/portal",              # local checkout
      "repository": "owner/name", "ref": "main",       # or a GitHub repository
      "model": "architecture/model/model.json",        # path inside root/repo
      "check": ["python3", "scripts/build_architecture.py", "--check"],
      "source_files": ["scripts/build_architecture.py"],
      "inputs": [], "outputs": [], "side_effects": [], "relationships": []
    }

Every model read is validated against the ``hermes.architecture`` contract
(``architecture_contract.py``, vendored byte-for-byte into every consumer): a
document that does not conform is refused with error 4033 and never
snapshotted, so a stored revision is always a conforming one.

Every revision read is stored as a **snapshot** under
``~/.hermes/architecture/<id>/<revision>.json`` with an index, so Portal can walk
a service's models over time; the first snapshot (genesis) is never pruned.
Check runs land in ``checks.json`` beside them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home

from tui_gateway import architecture_contract as contract

logger = logging.getLogger(__name__)

SERVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
DEFAULT_MODEL_PATH = "architecture/model/model.json"
MAX_MODEL_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOTS = 30
MAX_CHECKS = 20
MAX_CHECK_OUTPUT = 4000
CHECK_TIMEOUT_S = 300.0
FETCH_TIMEOUT_S = 20.0
# The prefix every architecture service id carries on the dataflow graph, so it
# can never collide with a resource ref, a cron id, or a proc_/docker:/nomad:/
# launchd: service id.
ID_PREFIX = "arch:"
# A manifest may bind to a service another provider already puts on the graph
# (``"runtime": {"provider": "launchd", "id": "ai.hermes.gateway"}``); the
# architecture then annotates that runtime node instead of adding an ``arch:``
# node beside it. The canonical graph identity per provider mirrors what the
# provider's own collector emits, so a binding is exact, never a name match.
RUNTIME_PROVIDERS = ("launchd", "docker", "nomad", "process")
RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:/-]{0,127}$")

_lock = threading.Lock()


class ArchitectureError(Exception):
    """A user-facing failure with a JSON-RPC error code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Paths ────────────────────────────────────────────────────────────────────


def manifests_dir(home: Optional[str] = None) -> Path:
    base = Path(home) if home else Path(get_hermes_home())
    return base / "services" / "architecture"


def snapshots_root(home: Optional[str] = None) -> Path:
    base = Path(home) if home else Path(get_hermes_home())
    return base / "architecture"


def _safe_dir_name(service_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", service_id)


def service_dir(service_id: str, home: Optional[str] = None) -> Path:
    return snapshots_root(home) / _safe_dir_name(service_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Manifests ────────────────────────────────────────────────────────────────


def _string_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def runtime_graph_id(provider: str, runtime_id: str) -> str:
    """The graph node id the named provider gives this runtime service."""
    if provider == "launchd":
        return f"launchd:{runtime_id}"
    if provider == "docker":
        return f"docker:{runtime_id[:12]}"
    if provider == "nomad":
        return f"nomad:{runtime_id}"
    if provider == "process":
        return runtime_id if runtime_id.startswith("proc_") else f"proc_{runtime_id}"
    raise ValueError(f"unsupported runtime provider {provider!r}")


def normalize_runtime_binding(value: Any) -> Dict[str, str]:
    """Validate ``runtime: {provider, id}``. Raises ValueError with a reason."""
    if not isinstance(value, dict):
        raise ValueError("runtime must be an object with provider and id")
    provider = value.get("provider")
    runtime_id = value.get("id")
    if not isinstance(provider, str) or provider not in RUNTIME_PROVIDERS:
        raise ValueError(f"runtime.provider must be one of {', '.join(RUNTIME_PROVIDERS)}")
    if not isinstance(runtime_id, str) or not runtime_id.strip():
        raise ValueError("runtime.id must be a non-empty string")
    runtime_id = runtime_id.strip()
    if not RUNTIME_ID_RE.match(runtime_id) or runtime_id.startswith(ID_PREFIX):
        raise ValueError(f"runtime.id {runtime_id!r} is not a valid {provider} identity")
    for prefix in ("launchd:", "docker:", "nomad:"):
        if runtime_id.startswith(prefix):
            raise ValueError(f"runtime.id must be the provider's own id, without the {prefix!r} prefix")
    return {"provider": provider, "id": runtime_id, "graph_id": runtime_graph_id(provider, runtime_id)}


def normalize_manifest(doc: Any, stem: str, launchd_dirs: Optional[List[str]] = None) -> Dict[str, Any]:
    """Validate one manifest document. Raises ValueError with a reason.
    ``launchd_dirs`` overrides where launchd plists are looked up (tests)."""
    if not isinstance(doc, dict):
        raise ValueError("manifest must be a JSON object")
    service_id = doc.get("id", stem)
    if not isinstance(service_id, str) or not SERVICE_ID_RE.match(service_id):
        raise ValueError("id must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    if service_id != stem:
        raise ValueError(f"id {service_id!r} must match the file name {stem!r}")
    name = doc.get("name")
    description = doc.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required (markdown shown on the service node)")
    root = doc.get("root")
    repository = doc.get("repository")
    if root is None and repository is None:
        raise ValueError("a manifest needs a local root or a GitHub repository")
    if root is not None:
        if not isinstance(root, str) or not root.strip():
            raise ValueError("root must be a non-empty path")
        root = str(Path(root).expanduser())
        if not os.path.isabs(root):
            raise ValueError("root must be an absolute path")
    if repository is not None:
        if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
            raise ValueError("repository must be owner/name")
    ref = doc.get("ref", "main")
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise ValueError("ref must be a branch, tag or commit")
    model = doc.get("model", DEFAULT_MODEL_PATH)
    if not isinstance(model, str) or not model.strip() or model.startswith("/") or ".." in model.split("/"):
        raise ValueError("model must be a relative path inside the service")
    check = doc.get("check")
    if check is not None:
        if not isinstance(check, list) or not check or not all(isinstance(part, str) and part for part in check):
            raise ValueError("check must be a non-empty argv list")
    manifest: Dict[str, Any] = {
        "id": service_id,
        "name": name.strip(),
        "description": description.strip(),
        "root": root,
        "repository": repository,
        "ref": ref,
        "model": model.strip(),
        "check": list(check) if check else None,
        "source_files": _string_list(doc.get("source_files"), "source_files"),
        "inputs": _string_list(doc.get("inputs"), "inputs"),
        "outputs": _string_list(doc.get("outputs"), "outputs"),
        "side_effects": _string_list(doc.get("side_effects"), "side_effects"),
    }
    if doc.get("relationships") is not None:
        manifest["relationships"] = doc["relationships"]
    if doc.get("runtime") is not None:
        manifest["runtime"] = normalize_runtime_binding(doc["runtime"])
    # Log capture: declared sinks (validated here; enforced for local services
    # in conformance_for). A GitHub-only service has no runtime here, so its
    # sinks are dropped with a warning rather than pretending to be readable.
    from tui_gateway import service_logs

    sinks = service_logs.normalize_log_sinks(doc.get("logs"), manifest.get("runtime"), launchd_dirs)
    if sinks and root is None:
        logger.warning("architecture manifest %s declares logs but has no local root; sinks ignored", service_id)
        sinks = []
    manifest["logs"] = sinks
    return manifest


def list_manifests(home: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every valid manifest, sorted by id. Malformed files are logged and skipped."""
    directory = manifests_dir(home)
    if not directory.is_dir():
        return []
    manifests: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            manifests.append(normalize_manifest(doc, path.stem))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("architecture manifest %s invalid: %s", path.name, exc)
    return manifests


def load_manifest(service_id: str, home: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The manifest for ``service_id`` (with or without the ``arch:`` prefix)."""
    bare = service_id[len(ID_PREFIX):] if service_id.startswith(ID_PREFIX) else service_id
    if not SERVICE_ID_RE.match(bare):
        return None
    path = manifests_dir(home) / f"{bare}.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return normalize_manifest(json.load(f), bare)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("architecture manifest %s invalid: %s", path.name, exc)
        return None


def service_param(params: Any) -> Optional[str]:
    """The ``service`` argument of an RPC, stripped, or None when missing/blank."""
    service_id = (params or {}).get("service") if isinstance(params, dict) else None
    if not isinstance(service_id, str) or not service_id.strip():
        return None
    return service_id.strip()


def graph_id(manifest: Dict[str, Any]) -> str:
    return f"{ID_PREFIX}{manifest['id']}"


def source_of(manifest: Dict[str, Any]) -> str:
    return "local" if manifest.get("root") else "github"


# ── Reading the model ────────────────────────────────────────────────────────


def _model_path(manifest: Dict[str, Any]) -> Path:
    root = Path(manifest["root"]).resolve()
    candidate = (root / manifest["model"]).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArchitectureError(4032, f"model path escapes the service root: {manifest['model']}") from exc
    return candidate


def local_revision(manifest: Dict[str, Any], git_runner: Optional[Callable[[str], str]] = None) -> str:
    """The checkout's git head, or a content digest of the model when there is no repository."""
    root = manifest.get("root")
    if not root:
        return ""
    runner = git_runner or _git_head
    head = runner(root)
    if head:
        return head
    try:
        path = _model_path(manifest)
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except (ArchitectureError, OSError):
        return ""


def _git_head(root: str) -> str:
    try:
        from tui_gateway.git_probe import run_git

        return run_git(root, "rev-parse", "HEAD")
    except Exception:  # pragma: no cover - probe is fail-open by contract
        return ""


def _parse_model(raw: bytes, where: str) -> Dict[str, Any]:
    if len(raw) > MAX_MODEL_BYTES:
        raise ArchitectureError(4032, f"model at {where} exceeds {MAX_MODEL_BYTES} bytes")
    try:
        model = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchitectureError(4032, f"model at {where} is not valid JSON: {exc}") from exc
    if not isinstance(model, dict) or "schema_version" not in model:
        raise ArchitectureError(4032, f"model at {where} is not an architecture model (no schema_version)")
    problems = contract.validate_document(model)
    if problems:
        raise ArchitectureError(4033, nonconforming_message(where, problems))
    return model


def nonconforming_message(where: str, problems: List[str], shown: int = 5) -> str:
    """One line a client can show verbatim: which contract, where, the first
    problems, and how many more the compiler will list."""
    head = "; ".join(problems[:shown])
    more = f" (+{len(problems) - shown} more)" if len(problems) > shown else ""
    return f"model at {where} does not conform to {contract.CONTRACT_NAME} v{contract.CONTRACT_VERSION}: {head}{more}"


def contract_ref() -> Dict[str, Any]:
    """The contract this gateway validates against, for envelopes and annotations."""
    return {"name": contract.CONTRACT_NAME, "version": contract.CONTRACT_VERSION}


def read_local_model(manifest: Dict[str, Any]) -> Dict[str, Any]:
    path = _model_path(manifest)
    if not path.is_file():
        raise ArchitectureError(4032, f"model not found at {path}; run the service's compiler first")
    return _parse_model(path.read_bytes(), str(path))


def _github_raw_url(manifest: Dict[str, Any]) -> str:
    return f"https://raw.githubusercontent.com/{manifest['repository']}/{manifest['ref']}/{manifest['model']}"


def read_github_model(manifest: Dict[str, Any], opener: Optional[Callable[[str, Dict[str, str]], bytes]] = None) -> Dict[str, Any]:
    url = _github_raw_url(manifest)
    headers: Dict[str, str] = {"User-Agent": "harness-architecture"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    fetch = opener or _http_get
    try:
        raw = fetch(url, headers)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ArchitectureError(4032, f"could not fetch {url}: {exc}") from exc
    return _parse_model(raw, url)


def _http_get(url: str, headers: Dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310 - https to GitHub only
        return response.read(MAX_MODEL_BYTES + 1)


def read_model(manifest: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """The service's current model with its revision and where it came from."""
    if manifest.get("root"):
        model = read_local_model(manifest)
        revision = local_revision(manifest, kwargs.get("git_runner")) or "working-tree"
        return {"model": model, "revision": revision, "source": "local"}
    model = read_github_model(manifest, kwargs.get("opener"))
    revision = str(model.get("source_revision") or manifest["ref"])
    return {"model": model, "revision": revision, "source": "github"}


# ── Summaries ────────────────────────────────────────────────────────────────


def summarize_model(model: Dict[str, Any]) -> Dict[str, Any]:
    """The counts Portal shows on a service node without loading the whole model."""
    interplay = model.get("interplay") if isinstance(model.get("interplay"), dict) else {}
    invariants = interplay.get("invariants") if isinstance(interplay.get("invariants"), list) else []
    ci = model.get("ci") if isinstance(model.get("ci"), dict) else {}
    ci_summary = ci.get("summary") if isinstance(ci.get("summary"), dict) else {}
    inventory = model.get("inventory") if isinstance(model.get("inventory"), dict) else {}
    violated = sorted(str(item.get("id")) for item in invariants if isinstance(item, dict) and item.get("status") != "holds")
    return {
        "schema_version": str(model.get("schema_version") or ""),
        "title": str(model.get("title") or ""),
        "source_tree_sha256": str(model.get("source_tree_sha256") or ""),
        "components": len(model.get("components") or []),
        "files": int(inventory.get("swift_files") or inventory.get("files") or 0),
        "lines": int(inventory.get("swift_lines") or inventory.get("lines") or 0),
        "nodes": len(interplay.get("nodes") or []),
        "edges": len(interplay.get("edges") or []),
        "flows": len(interplay.get("flows") or []),
        "invariants": {"total": len(invariants), "holds": len(invariants) - len(violated), "violated": violated},
        "stores": len((model.get("stores") or {}).get("items") or []) if isinstance(model.get("stores"), dict) else 0,
        "externals": len((model.get("externals") or {}).get("systems") or []) if isinstance(model.get("externals"), dict) else 0,
        "gates": int(ci_summary.get("gates") or 0),
        "ratchets": int(ci_summary.get("ratchets") or 0),
        "workflows": int(ci_summary.get("workflows") or 0),
    }


# ── Snapshots ────────────────────────────────────────────────────────────────


def _index_file(service_id: str, home: Optional[str] = None) -> Path:
    return service_dir(service_id, home) / "index.json"


def _snapshot_file(service_id: str, revision: str, home: Optional[str] = None) -> Path:
    return service_dir(service_id, home) / f"{_safe_dir_name(revision)}.json"


def _checks_file(service_id: str, home: Optional[str] = None) -> Path:
    return service_dir(service_id, home) / "checks.json"


def store_snapshot(service_id: str, revision: str, model: Dict[str, Any], source: str,
                   home: Optional[str] = None) -> Dict[str, Any]:
    """Record one revision's model. Idempotent per revision; prunes the oldest
    non-genesis snapshots past ``MAX_SNAPSHOTS``. Returns the index entry."""
    with _lock:
        index = _read_json(_index_file(service_id, home), {})
        revisions: List[Dict[str, Any]] = [r for r in index.get("revisions", []) if isinstance(r, dict)]
        existing = next((r for r in revisions if r.get("revision") == revision), None)
        if existing is None:
            entry = {
                "revision": revision,
                "source": source,
                "stored_at": _now_iso(),
                "summary": summarize_model(model),
                # Only a validated document reaches here (read_model validates
                # before describe() stores), so the entry records which contract.
                "contract": contract_ref(),
            }
            _write_json(_snapshot_file(service_id, revision, home), model)
            revisions.append(entry)
            while len(revisions) > MAX_SNAPSHOTS:
                # Genesis (the first snapshot) is kept; the oldest after it goes.
                victim = revisions.pop(1)
                try:
                    _snapshot_file(service_id, str(victim.get("revision")), home).unlink()
                except OSError:
                    pass
        else:
            entry = existing
        index = {"service": service_id, "latest": revision, "revisions": revisions}
        _write_json(_index_file(service_id, home), index)
        return entry


def list_snapshots(service_id: str, home: Optional[str] = None) -> Dict[str, Any]:
    index = _read_json(_index_file(service_id, home), {})
    revisions = [r for r in index.get("revisions", []) if isinstance(r, dict)]
    return {"service": service_id, "latest": index.get("latest"), "revisions": revisions}


def load_snapshot(service_id: str, revision: str, home: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = _snapshot_file(service_id, revision, home)
    model = _read_json(path, {})
    return model or None


# ── Checks ───────────────────────────────────────────────────────────────────


def record_check(service_id: str, result: Dict[str, Any], home: Optional[str] = None) -> None:
    with _lock:
        checks = _read_json(_checks_file(service_id, home), {})
        runs: List[Dict[str, Any]] = [r for r in checks.get("runs", []) if isinstance(r, dict)]
        runs.insert(0, result)
        del runs[MAX_CHECKS:]
        _write_json(_checks_file(service_id, home), {"service": service_id, "runs": runs})


def last_check(service_id: str, home: Optional[str] = None) -> Optional[Dict[str, Any]]:
    checks = _read_json(_checks_file(service_id, home), {})
    runs = [r for r in checks.get("runs", []) if isinstance(r, dict)]
    return runs[0] if runs else None


def list_checks(service_id: str, home: Optional[str] = None) -> List[Dict[str, Any]]:
    checks = _read_json(_checks_file(service_id, home), {})
    return [r for r in checks.get("runs", []) if isinstance(r, dict)]


def run_check(manifest: Dict[str, Any], runner: Optional[Callable[..., Any]] = None,
              timeout: float = CHECK_TIMEOUT_S, home: Optional[str] = None) -> Dict[str, Any]:
    """Run the service's own ``--check`` in its root and record the outcome.

    Local services only: a GitHub service's checks are its repository's CI, which
    this gateway does not run. ``status`` is ``passed``, ``failed`` (non-zero
    exit), or ``unavailable`` (no check configured, not local, spawn failure or
    timeout — the ``reason`` says which)."""
    service_id = manifest["id"]
    started = time.monotonic()
    checked_at = _now_iso()
    result: Dict[str, Any] = {"checked_at": checked_at, "revision": local_revision(manifest) or "working-tree"}
    if not manifest.get("root"):
        result.update({"status": "unavailable", "reason": "checks for a GitHub service run in its repository's CI"})
    elif not manifest.get("check"):
        result.update({"status": "unavailable", "reason": "no check command declared in the manifest"})
    else:
        run = runner or subprocess.run
        try:
            completed = run(
                manifest["check"], cwd=manifest["root"], capture_output=True, text=True, timeout=timeout,
            )
            output = ((completed.stdout or "") + (completed.stderr or "")).strip()
            result.update({
                "status": "passed" if completed.returncode == 0 else "failed",
                "exit_code": int(completed.returncode),
                "output": output[-MAX_CHECK_OUTPUT:],
            })
        except subprocess.TimeoutExpired:
            result.update({"status": "unavailable", "reason": f"check exceeded {int(timeout)}s"})
        except (OSError, ValueError) as exc:
            result.update({"status": "unavailable", "reason": f"could not run check: {exc}"})
    result["duration_s"] = round(time.monotonic() - started, 3)
    result["command"] = list(manifest.get("check") or [])
    record_check(service_id, result, home)
    return result


# ── Composite reads ──────────────────────────────────────────────────────────


def resolved_logs(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The manifest's log sinks as clients see them: existence, size, mtime."""
    from tui_gateway import service_logs

    return service_logs.resolve_sinks(manifest)


def status_for(manifest: Dict[str, Any], home: Optional[str] = None) -> Dict[str, Any]:
    """The cheap annotation a service node carries: source, revision, last check."""
    snapshots = list_snapshots(manifest["id"], home)
    check = last_check(manifest["id"], home)
    revision = local_revision(manifest) if manifest.get("root") else (snapshots.get("latest") or manifest["ref"])
    annotation: Dict[str, Any] = {
        "ref": graph_id(manifest),
        "source": source_of(manifest),
        "revision": revision or snapshots.get("latest") or "",
        "model": manifest["model"],
        "snapshots": len(snapshots.get("revisions") or []),
    }
    if manifest.get("runtime"):
        annotation["runtime"] = manifest["runtime"]["graph_id"]
    annotation["logs"] = [sink["id"] for sink in manifest.get("logs") or []]
    if check is not None:
        annotation["check"] = {"status": check.get("status"), "checked_at": check.get("checked_at")}
    latest = snapshots.get("latest")
    entry = next((r for r in snapshots.get("revisions") or [] if r.get("revision") == latest), None)
    if entry and isinstance(entry.get("summary"), dict):
        annotation["summary"] = entry["summary"]
    annotation["contract"] = contract_ref()
    annotation.update(conformance_for(manifest, snapshots, home))
    return annotation


def conformance_for(manifest: Dict[str, Any], snapshots: Optional[Dict[str, Any]] = None,
                    home: Optional[str] = None) -> Dict[str, Any]:
    """``{"conforming": bool, "problem"?: str}`` for the node annotation, cheaply:
    a local model is read and validated from its checkout (no network) AND the
    manifest must declare at least one resolvable log sink (log capture is part
    of the standard for local services); a GitHub model is conforming when a
    snapshot of it was stored (snapshots are only stored for validated documents)
    and unread until ``architecture.describe`` fetches it. Fail-open: a read
    failure is a non-conformance with the reason; several reasons are joined."""
    if manifest.get("root"):
        from tui_gateway import service_logs

        problems: List[str] = []
        try:
            read_local_model(manifest)
        except ArchitectureError as exc:
            problems.append(exc.message)
        capture = service_logs.capture_problem(manifest)
        if capture:
            problems.append(capture)
        if problems:
            return {"conforming": False, "problem": "; ".join(problems)}
        return {"conforming": True}
    snapshots = snapshots if snapshots is not None else list_snapshots(manifest["id"], home)
    if snapshots.get("latest"):
        return {"conforming": True}
    return {"conforming": False, "problem": "model not read yet; architecture.describe fetches and validates it"}


def describe(manifest: Dict[str, Any], revision: Optional[str] = None, home: Optional[str] = None,
             **read_kwargs: Any) -> Dict[str, Any]:
    """The model for a service at its current revision (reading and snapshotting
    it) or at a stored revision. Raises ArchitectureError when unavailable."""
    service_id = manifest["id"]
    if revision:
        model = load_snapshot(service_id, revision, home)
        if model is None:
            raise ArchitectureError(4404, f"no stored revision {revision} for {service_id}")
        source = next((r.get("source") for r in list_snapshots(service_id, home)["revisions"] if r.get("revision") == revision), source_of(manifest))
        entry = {"revision": revision, "source": source}
    else:
        read = read_model(manifest, **read_kwargs)
        model = read["model"]
        entry = store_snapshot(service_id, read["revision"], model, read["source"], home)
    return {
        "service": {
            "id": graph_id(manifest),
            "label": manifest["name"],
            "description": manifest["description"],
            "source": source_of(manifest),
            "root": manifest.get("root"),
            "repository": manifest.get("repository"),
            "ref": manifest.get("ref") if manifest.get("repository") else None,
            "model_path": manifest["model"],
            "check_configured": bool(manifest.get("check")),
            "runtime": manifest.get("runtime"),
            "logs": resolved_logs(manifest),
        },
        "revision": entry["revision"],
        "source": entry.get("source", source_of(manifest)),
        "stored_at": entry.get("stored_at"),
        "summary": entry.get("summary") or summarize_model(model),
        "check": last_check(service_id, home),
        "contract": contract.describe_contract(),
        # Whether this is the newest stored revision, so a client viewing an older
        # snapshot can say so (architecture.history lists them all).
        "is_latest": entry["revision"] == list_snapshots(service_id, home).get("latest"),
        "model": model,
    }


# ── Revision history ─────────────────────────────────────────────────────────
#
# A snapshot per revision is what the store already keeps; history joins those
# snapshots to the commits behind them (local checkouts only) and says which one
# the checkout is at now. "Deployed" here means exactly that — the revision the
# root is checked out at — not proof that a process restarted; a runtime
# provider that can report a pid does so under ``runtime``.

MAX_HISTORY_COMMITS = 50
MAX_DIFF_STAT = 500
WORKING_TREE_REVISION = "working-tree"
_GIT_LOG_FORMAT = "%H%x1f%an%x1f%aI%x1f%s"

GitRunner = Callable[..., str]


def _run_git(root: str, *args: str) -> str:
    """``git <args>`` in ``root``; empty string on any failure (fail-open)."""
    try:
        from tui_gateway.git_probe import run_git

        return run_git(root, *args) or ""
    except Exception:  # pragma: no cover - probe is fail-open by contract
        return ""


def _is_git_revision(revision: Optional[str]) -> bool:
    return bool(revision) and revision != WORKING_TREE_REVISION and not str(revision).startswith("sha256:")


def _parse_commits(text: str) -> List[Dict[str, str]]:
    commits: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4 or not parts[0].strip():
            continue
        commits.append({"sha": parts[0].strip(), "author": parts[1], "date": parts[2], "subject": parts[3]})
    return commits


def commit_info(root: Optional[str], revision: Optional[str], git: Optional[GitRunner] = None) -> Optional[Dict[str, str]]:
    """``{sha, author, date, subject}`` for one revision of a local root, or None
    when there is no root, the revision is not a git commit, or git fails."""
    if not root or not _is_git_revision(revision):
        return None
    commits = _parse_commits((git or _run_git)(root, "log", "-1", f"--format={_GIT_LOG_FORMAT}", str(revision)))
    return commits[0] if commits else None


def commits_between(root: Optional[str], previous: Optional[str], revision: Optional[str],
                    git: Optional[GitRunner] = None) -> Optional[List[Dict[str, str]]]:
    """The commits ``previous..revision`` oldest first, bounded at
    ``MAX_HISTORY_COMMITS``; None when either end is not a git commit."""
    if not root or not _is_git_revision(previous) or not _is_git_revision(revision):
        return None
    text = (git or _run_git)(root, "log", "--reverse", f"--max-count={MAX_HISTORY_COMMITS}",
                             f"--format={_GIT_LOG_FORMAT}", f"{previous}..{revision}")
    return _parse_commits(text)


def git_numstat(root: str, previous: str, revision: str, git: Optional[GitRunner] = None) -> Dict[str, Any]:
    """``git diff --numstat previous..revision`` as ``{stat: [...], truncated}``;
    binary files carry null counts."""
    text = (git or _run_git)(root, "diff", "--numstat", f"{previous}..{revision}")
    stat: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        additions = int(parts[0]) if parts[0].isdigit() else None
        deletions = int(parts[1]) if parts[1].isdigit() else None
        stat.append({"path": parts[2], "additions": additions, "deletions": deletions})
    truncated = len(stat) > MAX_DIFF_STAT
    return {"stat": stat[:MAX_DIFF_STAT], "truncated": truncated}


def runtime_info(manifest: Dict[str, Any], runner: Optional[Callable[[List[str]], str]] = None) -> Optional[Dict[str, Any]]:
    """What the runtime provider can say about the bound service: provider and
    graph id always; for launchd the running pid when ``launchctl print`` reports
    one (fail-open). No provider reports a start time, so ``started_at`` is
    absent rather than guessed."""
    binding = manifest.get("runtime")
    if not isinstance(binding, dict):
        return None
    info: Dict[str, Any] = {"provider": binding.get("provider"), "graph_id": binding.get("graph_id")}
    if binding.get("provider") == "launchd":
        try:
            from tools.launchd_services import _default_runner, _uid

            out = (runner or _default_runner)(["launchctl", "print", f"gui/{_uid()}/{binding.get('id')}"])
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("pid ="):
                    value = stripped.split("=", 1)[1].strip()
                    if value.isdigit():
                        info["pid"] = int(value)
                    break
        except Exception:
            pass
    return info


def history(manifest: Dict[str, Any], home: Optional[str] = None, git: Optional[GitRunner] = None,
            runtime_runner: Optional[Callable[[List[str]], str]] = None) -> Dict[str, Any]:
    """Stored revisions (genesis first, newest last) with the commit behind each,
    the commits since the previous snapshot, and which one the checkout is at."""
    service_id = manifest["id"]
    root = manifest.get("root")
    snapshots = list_snapshots(service_id, home)
    revisions: List[Dict[str, Any]] = [dict(r) for r in snapshots.get("revisions") or []]
    head = local_revision(manifest, (lambda r: (git or _run_git)(r, "rev-parse", "HEAD")) if root else None) if root else ""
    deployed_index = None
    for index, entry in enumerate(revisions):
        if root and head and entry.get("revision") == head:
            deployed_index = index  # the newest match wins
    previous: Optional[str] = None
    for index, entry in enumerate(revisions):
        revision = str(entry.get("revision") or "")
        if root:
            commit = commit_info(root, revision, git)
            if commit:
                entry["commit"] = commit
            since = commits_between(root, previous, revision, git)
            if since is not None:
                entry["commits_since_previous"] = since
        entry["deployed"] = index == deployed_index
        if entry["deployed"]:
            entry["deployed_at"] = entry.get("stored_at")
        previous = revision
    result: Dict[str, Any] = {
        "service": graph_id(manifest),
        "source": source_of(manifest),
        "latest": snapshots.get("latest"),
        "head": head or None,
        "revisions": revisions,
        "checks": list_checks(service_id, home),
    }
    runtime = runtime_info(manifest, runtime_runner)
    if runtime is not None:
        result["runtime"] = runtime
    return result


def _node_key(node: Dict[str, Any]) -> str:
    return str(node.get("history_key") or node.get("id") or "")


def _index_nodes(model: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    interplay = model.get("interplay") if isinstance(model.get("interplay"), dict) else {}
    return {_node_key(n): n for n in interplay.get("nodes") or [] if isinstance(n, dict) and _node_key(n)}


def _edge_keys(model: Dict[str, Any]) -> Dict[tuple, Dict[str, Any]]:
    interplay = model.get("interplay") if isinstance(model.get("interplay"), dict) else {}
    by_id = {str(n.get("id")): _node_key(n) for n in interplay.get("nodes") or [] if isinstance(n, dict)}
    edges: Dict[tuple, Dict[str, Any]] = {}
    for edge in interplay.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = by_id.get(str(edge.get("source")), str(edge.get("source")))
        target = by_id.get(str(edge.get("target")), str(edge.get("target")))
        relation = str(edge.get("relation") or "")
        edges[(source, target, relation)] = {"source": source, "target": target, "relation": relation, "class": edge.get("class")}
    return edges


def _node_record(key: str, node: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": node.get("id"), "history_key": key, "kind": node.get("kind"), "label": node.get("label"), "component": node.get("component")}


def diff_models(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """What changed between two documents, by stable identity: nodes by history
    key (a rename is not churn), edges by (source key, target key, relation),
    invariants by id, files by path, gate jobs and ratchets by id."""
    nodes_before, nodes_after = _index_nodes(before), _index_nodes(after)
    edges_before, edges_after = _edge_keys(before), _edge_keys(after)

    def invariants(model: Dict[str, Any]) -> Dict[str, str]:
        interplay = model.get("interplay") if isinstance(model.get("interplay"), dict) else {}
        return {str(i.get("id")): str(i.get("status") or "") for i in interplay.get("invariants") or [] if isinstance(i, dict) and i.get("id")}

    def files(model: Dict[str, Any]) -> Dict[str, int]:
        extraction = model.get("extraction") if isinstance(model.get("extraction"), dict) else {}
        return {str(f.get("path")): int(f.get("line_count") or 0) for f in extraction.get("files") or [] if isinstance(f, dict) and f.get("path")}

    def ids(model: Dict[str, Any], key: str) -> set:
        ci = model.get("ci") if isinstance(model.get("ci"), dict) else {}
        return {str(item.get("id")) for item in ci.get(key) or [] if isinstance(item, dict) and item.get("id")}

    inv_before, inv_after = invariants(before), invariants(after)
    files_before, files_after = files(before), files(after)
    return {
        "nodes": {
            "added": [_node_record(k, nodes_after[k]) for k in sorted(set(nodes_after) - set(nodes_before))],
            "removed": [_node_record(k, nodes_before[k]) for k in sorted(set(nodes_before) - set(nodes_after))],
        },
        "edges": {
            "added": [edges_after[k] for k in sorted(set(edges_after) - set(edges_before))],
            "removed": [edges_before[k] for k in sorted(set(edges_before) - set(edges_after))],
        },
        "invariants": {
            "added": sorted(set(inv_after) - set(inv_before)),
            "removed": sorted(set(inv_before) - set(inv_after)),
            "changed": [{"id": i, "from": inv_before[i], "to": inv_after[i]} for i in sorted(set(inv_before) & set(inv_after)) if inv_before[i] != inv_after[i]],
        },
        "files": {
            "added": sorted(set(files_after) - set(files_before)),
            "removed": sorted(set(files_before) - set(files_after)),
            "changed": [{"path": p, "lines_from": files_before[p], "lines_to": files_after[p]}
                        for p in sorted(set(files_before) & set(files_after)) if files_before[p] != files_after[p]],
        },
        "gates": {
            "jobs_added": sorted(ids(after, "jobs") - ids(before, "jobs")),
            "jobs_removed": sorted(ids(before, "jobs") - ids(after, "jobs")),
            "ratchets_added": sorted(ids(after, "ratchets") - ids(before, "ratchets")),
            "ratchets_removed": sorted(ids(before, "ratchets") - ids(after, "ratchets")),
        },
        "summary": {"from": summarize_model(before), "to": summarize_model(after)},
    }


def diff(manifest: Dict[str, Any], from_revision: Optional[str] = None, to_revision: Optional[str] = None,
         home: Optional[str] = None, git: Optional[GitRunner] = None) -> Dict[str, Any]:
    """The structural diff between two stored revisions; ``to`` defaults to the
    latest snapshot and ``from`` to the one stored before it. 4404 when a
    revision is not stored or there is nothing before ``to``. For a local root
    the commits and ``git diff --numstat`` between the two are attached (fail-open)."""
    service_id = manifest["id"]
    snapshots = list_snapshots(service_id, home)
    order = [str(r.get("revision")) for r in snapshots.get("revisions") or []]
    to_rev = to_revision or snapshots.get("latest")
    if not to_rev or to_rev not in order:
        raise ArchitectureError(4404, f"no stored revision {to_rev or '(latest)'} for {service_id}")
    if from_revision:
        from_rev = from_revision
    else:
        position = order.index(to_rev)
        if position == 0:
            raise ArchitectureError(4404, f"no stored revision before {to_rev} for {service_id}")
        from_rev = order[position - 1]
    before = load_snapshot(service_id, from_rev, home)
    after = load_snapshot(service_id, to_rev, home)
    if before is None:
        raise ArchitectureError(4404, f"no stored revision {from_rev} for {service_id}")
    if after is None:
        raise ArchitectureError(4404, f"no stored revision {to_rev} for {service_id}")
    result: Dict[str, Any] = {"service": graph_id(manifest), "from": from_rev, "to": to_rev}
    result.update(diff_models(before, after))
    root = manifest.get("root")
    if root and _is_git_revision(from_rev) and _is_git_revision(to_rev):
        commits = commits_between(root, from_rev, to_rev, git) or []
        numstat = git_numstat(root, from_rev, to_rev, git)
        result["git"] = {"commits": commits, "stat": numstat["stat"],
                         "truncated": numstat["truncated"] or len(commits) >= MAX_HISTORY_COMMITS}
    return result
