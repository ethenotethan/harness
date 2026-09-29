"""Deployments: where a service's code runs when it is not on this machine.

The runtime providers (process, Docker, Nomad, launchd) answer "is it live, what
is live, show me its logs" for services on the gateway host. A Lambda, a Cloud
Run revision, an ECS service or a Fly app has no process, port or container the
gateway can see, so until now such a service could only be a standalone
``arch:<id>`` codebase node: the code was drawn, never whether it was up.

A manifest may now declare ``deployments``: one entry per environment the
codebase is deployed to, each with a provider name, a target, an explicit
**health probe**, an optional **revision probe** and its own **log sinks**. The
probes are the deployment's own declaration of how to ask — an HTTPS endpoint
with an expected status, or a bounded command whose exit code answers — never a
provider SDK the gateway has to carry. Logs use the ``command`` sink kind
(``aws logs tail``, ``gcloud logging read``, ``flyctl logs``), bounded like every
other read here. Each deployment is a graph node of its own
(``deploy:<manifest-id>/<deployment-id>``) beside the codebase node, so staging
and production are two nodes with two healths, both pointing back at the model.

Probing is explicit and cheap to reason about: ``service.deployments.probe`` runs
the probes now; the graph and ``architecture.describe`` read the last known
result (or probe once with a short timeout when nothing is known yet), never a
guess. Results are cached in memory for ``CACHE_TTL_S`` so a graph refresh does
not hammer a health endpoint. Only declared URLs and argv are ever contacted or
run — never a caller-supplied one.

Manifest shape::

    "deployments": [
      {
        "id": "prod",                                    # unique per manifest
        "environment": "production",                     # label; defaults to id
        "provider": "aws-lambda",                        # free-form token: aws-lambda, ecs, cloud-run, fly, vercel, kubernetes, …
        "target": "arn:aws:lambda:us-east-1:…:function:demo",
        "url": "https://demo.example.com",               # optional; drawn as an output
        "health": {"kind": "https", "url": "https://demo.example.com/healthz", "expect": [200, 204], "timeout_s": 5},
        "revision": {"kind": "command", "argv": ["aws", "lambda", "get-function-configuration", "--function-name", "demo", "--query", "Version", "--output", "text"]},
        "logs": [{"id": "cloudwatch", "kind": "command", "argv": ["aws", "logs", "tail", "/aws/lambda/demo", "--since", "15m", "--format", "short"]}]
      }
    ]

``health.kind`` is ``https`` (GET the URL; live when the status is in ``expect``,
default ``200``) or ``command`` (run argv without a shell; live on exit 0).
``revision.kind`` is ``command`` (first non-empty stdout line is the live
revision) or ``https`` (GET; the body, or ``json_path`` into a JSON body).
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ID_PREFIX = "deploy:"
DEPLOYMENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
HEALTH_KINDS = ("https", "command")
REVISION_KINDS = ("https", "command")
DEFAULT_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 60.0
DEFAULT_EXPECT = (200,)
MAX_BODY_BYTES = 256 * 1024
# How long a probe result stands in for a fresh one on the graph and in describe.
CACHE_TTL_S = 30.0
STATUSES = ("live", "down", "unknown")


class DeploymentError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Declaration ──────────────────────────────────────────────────────────────

def _timeout(value: Any, field: str) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field}.timeout_s must be a positive number of seconds")
    return float(min(value, MAX_TIMEOUT_S))


def _argv(value: Any, field: str) -> List[str]:
    if isinstance(value, str):
        value = shlex.split(value)
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part.strip() for part in value):
        raise ValueError(f"{field}.argv must be a non-empty list of strings")
    return [part.strip() for part in value]


def _https_url(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}.url is required")
    url = value.strip()
    if not (url.startswith("https://") or url.startswith("http://localhost") or url.startswith("http://127.0.0.1")):
        raise ValueError(f"{field}.url must be https:// (plain http is allowed for localhost only)")
    return url


def normalize_health(value: Any, field: str = "health") -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object with kind https or command")
    kind = value.get("kind")
    if kind not in HEALTH_KINDS:
        raise ValueError(f"{field}.kind must be one of {', '.join(HEALTH_KINDS)}")
    probe: Dict[str, Any] = {"kind": kind, "timeout_s": _timeout(value.get("timeout_s"), field)}
    if kind == "https":
        probe["url"] = _https_url(value.get("url"), field)
        expect = value.get("expect", list(DEFAULT_EXPECT))
        if isinstance(expect, int) and not isinstance(expect, bool):
            expect = [expect]
        if not isinstance(expect, list) or not expect or not all(isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599 for code in expect):
            raise ValueError(f"{field}.expect must be an HTTP status or a list of statuses")
        probe["expect"] = sorted(set(expect))
    else:
        probe["argv"] = _argv(value.get("argv"), field)
    return probe


def normalize_revision(value: Any, field: str = "revision") -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object with kind https or command")
    kind = value.get("kind")
    if kind not in REVISION_KINDS:
        raise ValueError(f"{field}.kind must be one of {', '.join(REVISION_KINDS)}")
    probe: Dict[str, Any] = {"kind": kind, "timeout_s": _timeout(value.get("timeout_s"), field)}
    if kind == "https":
        probe["url"] = _https_url(value.get("url"), field)
        json_path = value.get("json_path")
        if json_path is not None:
            if not isinstance(json_path, str) or not json_path.strip():
                raise ValueError(f"{field}.json_path must be a dotted key path")
            probe["json_path"] = json_path.strip()
    else:
        probe["argv"] = _argv(value.get("argv"), field)
    return probe


def normalize_deployments(value: Any, manifest_id: str) -> List[Dict[str, Any]]:
    """Validate ``deployments`` from a manifest. Raises ValueError with a reason.
    Log sinks are validated by ``service_logs.normalize_log_sinks`` with no
    runtime binding, so launchd kinds are refused and ``command`` is allowed."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("deployments must be a list of {id, provider, target, health, …}")
    from tui_gateway import service_logs

    deployments: List[Dict[str, Any]] = []
    seen: set = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"deployments[{index}] must be an object")
        dep_id = raw.get("id")
        if not isinstance(dep_id, str) or not DEPLOYMENT_ID_RE.match(dep_id):
            raise ValueError(f"deployments[{index}].id must match ^[a-z0-9][a-z0-9._-]{{0,63}}$")
        if dep_id in seen:
            raise ValueError(f"deployments: duplicate id {dep_id!r}")
        seen.add(dep_id)
        field = f"deployments[{dep_id}]"
        provider = raw.get("provider")
        if not isinstance(provider, str) or not PROVIDER_RE.match(provider):
            raise ValueError(f"{field}.provider must be a lowercase token such as aws-lambda, cloud-run, fly")
        target = raw.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"{field}.target is required (the provider's own identity for the deployment)")
        environment = raw.get("environment", dep_id)
        if not isinstance(environment, str) or not environment.strip():
            raise ValueError(f"{field}.environment must be a non-empty string")
        url = raw.get("url")
        if url is not None:
            url = _https_url(url, field)
        if raw.get("health") is None:
            raise ValueError(f"{field}.health is required: a deployment the gateway cannot probe cannot be drawn as live or down")
        deployment: Dict[str, Any] = {
            "id": dep_id,
            "graph_id": f"{ID_PREFIX}{manifest_id}/{dep_id}",
            "environment": environment.strip(),
            "provider": provider,
            "target": target.strip(),
            "url": url,
            "health": normalize_health(raw["health"], f"{field}.health"),
            "revision": normalize_revision(raw["revision"], f"{field}.revision") if raw.get("revision") is not None else None,
            "logs": service_logs.normalize_log_sinks(raw.get("logs"), None, None, allow_command=True),
        }
        deployments.append(deployment)
    return deployments


def parse_graph_id(graph_id: str) -> Optional[tuple]:
    """``deploy:<manifest>/<deployment>`` → (manifest_id, deployment_id), else None."""
    if not isinstance(graph_id, str) or not graph_id.startswith(ID_PREFIX):
        return None
    rest = graph_id[len(ID_PREFIX):]
    if "/" not in rest:
        return None
    manifest_id, dep_id = rest.split("/", 1)
    if not manifest_id or not dep_id:
        return None
    return manifest_id, dep_id


def find_deployment(manifest: Dict[str, Any], dep_id: str) -> Optional[Dict[str, Any]]:
    for deployment in manifest.get("deployments") or []:
        if deployment.get("id") == dep_id:
            return deployment
    return None


# ── Probing ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _http_get(url: str, timeout: float) -> tuple:
    """(status, body_text) for a GET; HTTP errors are statuses, not exceptions."""
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-gateway-deployment-probe/1.0"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - declared URL only
            body = response.read(MAX_BODY_BYTES)
            return int(response.status), body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_BODY_BYTES) if exc.fp else b""
        return int(exc.code), body.decode("utf-8", errors="replace")


def run_argv(argv: List[str], timeout: float) -> subprocess.CompletedProcess:
    """A bounded, shell-less run of a declared command. The executable must exist
    on PATH or be an absolute path; otherwise the caller reports it as unknown."""
    if not shutil.which(argv[0]):
        raise FileNotFoundError(argv[0])
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603 - declared argv only


def probe_health(probe: Dict[str, Any], runner: Callable[[List[str], float], subprocess.CompletedProcess] = run_argv,
                 http_get: Callable[[str, float], tuple] = _http_get) -> Dict[str, Any]:
    """``{status, probe, target, checked_at, latency_ms, message}`` — the same shape
    the Docker provider's health carries. Never raises: a probe that cannot run
    is ``unknown`` with the reason."""
    started = time.monotonic()
    result: Dict[str, Any] = {"status": "unknown", "probe": probe["kind"], "checked_at": _now(), "latency_ms": 0, "message": ""}
    try:
        if probe["kind"] == "https":
            result["target"] = probe["url"]
            status, _body = http_get(probe["url"], probe["timeout_s"])
            live = status in set(probe.get("expect") or DEFAULT_EXPECT)
            result["status"] = "live" if live else "down"
            result["message"] = f"GET {probe['url']} → {status}" + ("" if live else f" (expected {', '.join(map(str, probe.get('expect') or DEFAULT_EXPECT))})")
        else:
            result["target"] = " ".join(shlex.quote(part) for part in probe["argv"])
            completed = runner(probe["argv"], probe["timeout_s"])
            result["status"] = "live" if completed.returncode == 0 else "down"
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()
            result["message"] = f"exit {completed.returncode}" + (f": {tail[-1][:200]}" if tail else "")
    except FileNotFoundError as exc:
        result["message"] = f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        result["status"] = "down"
        result["message"] = f"probe timed out after {probe['timeout_s']:g}s"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["status"] = "down"
        result["message"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - defensive; a probe must never break the graph
        result["message"] = f"{type(exc).__name__}: {exc}"
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _json_path(document: Any, path: str) -> Any:
    current = document
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
            current = current[int(key)]
        else:
            return None
    return current


def probe_revision(probe: Dict[str, Any], runner: Callable[[List[str], float], subprocess.CompletedProcess] = run_argv,
                   http_get: Callable[[str, float], tuple] = _http_get) -> Dict[str, Any]:
    """``{revision, checked_at, message}`` — ``revision`` is None when the probe
    could not answer, with the reason in ``message``. Never raises."""
    result: Dict[str, Any] = {"revision": None, "checked_at": _now(), "message": ""}
    try:
        if probe["kind"] == "https":
            status, body = http_get(probe["url"], probe["timeout_s"])
            if status != 200:
                result["message"] = f"GET {probe['url']} → {status}"
                return result
            text = body.strip()
            if probe.get("json_path"):
                try:
                    value = _json_path(json.loads(text), probe["json_path"])
                except ValueError:
                    result["message"] = "body is not JSON"
                    return result
                text = "" if value is None else str(value)
            result["revision"] = text.splitlines()[0].strip()[:128] if text else None
            if result["revision"] is None:
                result["message"] = "empty revision"
        else:
            completed = runner(probe["argv"], probe["timeout_s"])
            if completed.returncode != 0:
                result["message"] = f"exit {completed.returncode}: {(completed.stderr or '').strip()[:200]}"
                return result
            lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
            result["revision"] = lines[0][:128] if lines else None
            if result["revision"] is None:
                result["message"] = "command printed no revision"
    except FileNotFoundError as exc:
        result["message"] = f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        result["message"] = f"probe timed out after {probe['timeout_s']:g}s"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["message"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        result["message"] = f"{type(exc).__name__}: {exc}"
    return result


# ── Cache ────────────────────────────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


def last_probe(graph_id: str) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        entry = _cache.get(graph_id)
    return dict(entry) if entry else None


def _remember(graph_id: str, payload: Dict[str, Any]) -> None:
    with _cache_lock:
        _cache[graph_id] = {**payload, "_at": time.monotonic()}


def _fresh(graph_id: str, ttl: float) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        entry = _cache.get(graph_id)
    if entry and time.monotonic() - entry.get("_at", 0.0) <= ttl:
        return {k: v for k, v in entry.items() if k != "_at"}
    return None


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def probe_deployment(manifest: Dict[str, Any], deployment: Dict[str, Any], *, force: bool = False, ttl: float = CACHE_TTL_S,
                     runner: Callable[[List[str], float], subprocess.CompletedProcess] = run_argv,
                     http_get: Callable[[str, float], tuple] = _http_get) -> Dict[str, Any]:
    """Health and revision for one deployment: the cached result when fresh
    (unless ``force``), else probed now and remembered."""
    graph_id = deployment["graph_id"]
    if not force:
        cached = _fresh(graph_id, ttl)
        if cached is not None:
            return cached
    health = probe_health(deployment["health"], runner, http_get)
    health["target"] = health.get("target") or deployment["target"]
    revision = probe_revision(deployment["revision"], runner, http_get) if deployment.get("revision") else None
    payload = {
        "service": graph_id,
        "manifest": manifest["id"],
        "deployment": deployment["id"],
        "environment": deployment["environment"],
        "provider": deployment["provider"],
        "target": deployment["target"],
        "url": deployment.get("url"),
        "health": health,
        "revision": revision,
    }
    _remember(graph_id, payload)
    return {k: v for k, v in payload.items()}


def probe_manifest(manifest: Dict[str, Any], *, deployment_id: Optional[str] = None, force: bool = False,
                   ttl: float = CACHE_TTL_S) -> List[Dict[str, Any]]:
    """Probe every deployment of a manifest (or one), concurrently, in id order."""
    targets = [d for d in manifest.get("deployments") or [] if deployment_id is None or d["id"] == deployment_id]
    if deployment_id is not None and not targets:
        raise DeploymentError(4050, f"{manifest['id']} has no deployment {deployment_id!r}")
    if not targets:
        return []
    results: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        futures = {pool.submit(probe_deployment, manifest, d, force=force, ttl=ttl): d["id"] for d in targets}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [results[d["id"]] for d in targets]


# ── Declarations for the dataflow graph ──────────────────────────────────────

def describe_deployment(manifest: Dict[str, Any], deployment: Dict[str, Any], probe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The deployment as ``architecture.describe`` and ``service.deployments``
    report it: the declaration plus the last known probe (or none)."""
    from tui_gateway import service_logs

    return {
        "id": deployment["id"],
        "graph_id": deployment["graph_id"],
        "environment": deployment["environment"],
        "provider": deployment["provider"],
        "target": deployment["target"],
        "url": deployment.get("url"),
        "health_probe": deployment["health"],
        "revision_probe": deployment.get("revision"),
        "logs": [service_logs.resolve_sink(sink) for sink in deployment.get("logs") or []],
        "health": (probe or {}).get("health"),
        "revision": ((probe or {}).get("revision") or {}).get("revision") if probe else None,
        "probed_at": ((probe or {}).get("health") or {}).get("checked_at") if probe else None,
    }


def _markdown(manifest: Dict[str, Any], deployment: Dict[str, Any], probe: Dict[str, Any]) -> str:
    health = probe.get("health") or {}
    revision = (probe.get("revision") or {}).get("revision")
    lines = [
        f"**{manifest['name']}** deployed to **{deployment['environment']}** on `{deployment['provider']}`.",
        "",
        f"- target: `{deployment['target']}`",
    ]
    if deployment.get("url"):
        lines.append(f"- url: {deployment['url']}")
    lines.append(f"- health: **{health.get('status', 'unknown')}** — {health.get('message') or 'not probed yet'}")
    if deployment.get("revision"):
        lines.append(f"- live revision: `{revision}`" if revision else "- live revision: unknown")
    if deployment.get("logs"):
        lines.append(f"- logs: {', '.join(sink['label'] for sink in deployment['logs'])} (`service.logs` on `{deployment['graph_id']}`)")
    return "\n".join(lines)


def collect_deployment_services(home: Optional[str] = None, *, probe: bool = True) -> List[Dict[str, Any]]:
    """One graph service declaration per declared deployment, with its health.
    ``probe=False`` uses only what is already known (tests, cheap listings)."""
    from cron.jobs import normalize_service_declaration
    from tui_gateway import architecture_store as store

    services: List[Dict[str, Any]] = []
    for manifest in store.list_manifests(home):
        deployments = manifest.get("deployments") or []
        if not deployments:
            continue
        probes: Dict[str, Dict[str, Any]] = {}
        if probe:
            try:
                for result in probe_manifest(manifest):
                    probes[result["deployment"]] = result
            except Exception:
                logger.exception("deployment probes failed for %s", manifest["id"])
        annotation = store.status_for(manifest, home)
        for deployment in deployments:
            result = probes.get(deployment["id"]) or last_probe(deployment["graph_id"]) or {}
            health = dict(result.get("health") or {"status": "unknown", "probe": deployment["health"]["kind"],
                                                    "target": deployment["target"], "checked_at": "", "latency_ms": 0,
                                                    "message": "not probed yet"})
            try:
                declaration = normalize_service_declaration(
                    f"{manifest['name']} · {deployment['environment']}",
                    _markdown(manifest, deployment, result),
                    inputs=[],
                    outputs=[deployment["url"]] if deployment.get("url") else [],
                    side_effects=[],
                    relationships=[{"predicate": "deploys", "object": store.graph_id(manifest)}],
                    source_files=[],
                )
            except ValueError as exc:
                logger.warning("deployment %s rejected: %s", deployment["graph_id"], exc)
                continue
            services.append({
                "id": deployment["graph_id"],
                "label": declaration["name"],
                "description": declaration["description"],
                "inputs": declaration["inputs"],
                "outputs": declaration["outputs"],
                "side_effects": declaration["side_effects"],
                "relationships": declaration.get("relationships") or [],
                "source_files": [],
                "health": health,
                "deployment": {
                    "manifest": manifest["id"], "id": deployment["id"], "environment": deployment["environment"],
                    "provider": deployment["provider"], "target": deployment["target"], "url": deployment.get("url"),
                    "revision": ((result.get("revision") or {}).get("revision")) if result else None,
                },
                "architecture": {**annotation, "deployment": deployment["graph_id"]},
            })
    return services
