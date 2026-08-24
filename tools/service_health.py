"""Service health specifications and bounded readiness probes."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit


def _bounded_number(value: Any, *, field: str, default: float, minimum: float, maximum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"service_health.{field} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(
            f"service_health.{field} must be between {minimum:g} and {maximum:g} seconds"
        )
    return number


def normalize_service_health(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate the health gate attached to one service launch.

    HTTP is the first concrete probe contract.  The object is deliberately
    extensible so TCP/exec probes can be added without changing service lease
    persistence or graph rendering.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("service_health must be an object")
    probe_type = str(value.get("type") or "").strip().lower()
    if probe_type != "http":
        raise ValueError("service_health.type must be 'http'")
    url = str(value.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("service_health.url must be an http or https URL")
    expected = value.get("expected_status", 200)
    if isinstance(expected, bool) or not isinstance(expected, int) or not 100 <= expected <= 599:
        raise ValueError("service_health.expected_status must be an integer from 100 to 599")
    return {
        "type": "http",
        "url": url,
        "expected_status": expected,
        "timeout_seconds": _bounded_number(
            value.get("timeout_seconds"), field="timeout_seconds", default=2.0,
            minimum=0.1, maximum=30.0,
        ),
        "startup_timeout_seconds": _bounded_number(
            value.get("startup_timeout_seconds"), field="startup_timeout_seconds",
            default=30.0, minimum=0.1, maximum=300.0,
        ),
    }


def probe_service_health(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Run one bounded probe and return stable graph-facing evidence."""
    started = time.monotonic()
    status_code: Optional[int] = None
    error = ""
    try:
        request = Request(spec["url"], method="GET")
        with urlopen(request, timeout=spec["timeout_seconds"]) as response:  # noqa: S310
            status_code = int(response.status)
    except HTTPError as exc:
        status_code = int(exc.code)
    except Exception as exc:  # noqa: BLE001 - probe failures are health evidence
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = round((time.monotonic() - started) * 1000, 1)
    healthy = status_code == spec["expected_status"]
    if status_code is not None:
        message = f"HTTP {status_code}"
    else:
        message = error or "probe failed"
    return {
        "status": "healthy" if healthy else "unhealthy",
        "probe": spec["type"],
        "target": spec["url"],
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "latency_ms": latency_ms,
        "message": message,
    }


def wait_for_service_health(
    spec: Dict[str, Any],
    *,
    retry_interval_seconds: float = 0.25,
) -> Dict[str, Any]:
    """Probe until healthy or the startup deadline, returning final evidence."""
    deadline = time.monotonic() + spec["startup_timeout_seconds"]
    while True:
        evidence = probe_service_health(spec)
        if evidence["status"] == "healthy" or time.monotonic() >= deadline:
            return evidence
        time.sleep(min(retry_interval_seconds, max(0.0, deadline - time.monotonic())))
