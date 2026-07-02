"""APNs sender — pushes notifications to registered HermesNative devices.

Uses token-based (JWT/ES256) auth over APNs' HTTP/2 API via httpx. No
certificate provisioning needed — just the .p8 signing key from the Apple
Developer portal.

Configuration (env vars, typically in ~/.hermes/.env):
    APNS_KEY_PATH   path to the .p8 AuthKey (e.g. ~/.hermes/AuthKey_ABC123.p8)
    APNS_KEY_ID     10-char key id from the developer portal
    APNS_TEAM_ID    10-char Apple team id
    APNS_BUNDLE_ID  default topic (e.g. com.researchoors.HermesNative.macOS)
    APNS_ENV        "production" (default) or "sandbox"

APNs is enabled iff the first four are set. When disabled every send is a
silent no-op, so hooks can call ``notify_all`` unconditionally.

Delivery is fire-and-forget on a daemon thread: gateway event emission must
never block on Apple's servers.
"""

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_APNS_HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}

# JWT tokens are valid 20-60 min; refresh at 40.
_TOKEN_TTL_SECONDS = 40 * 60

_jwt_lock = threading.Lock()
_jwt_cache: dict = {"token": None, "issued_at": 0.0}


def _config() -> Optional[dict]:
    """Read APNs config from the environment; None when not configured."""
    key_path = os.environ.get("APNS_KEY_PATH", "").strip()
    key_id = os.environ.get("APNS_KEY_ID", "").strip()
    team_id = os.environ.get("APNS_TEAM_ID", "").strip()
    bundle_id = os.environ.get("APNS_BUNDLE_ID", "").strip()
    if not (key_path and key_id and team_id and bundle_id):
        return None
    env = os.environ.get("APNS_ENV", "production").strip().lower()
    host = _APNS_HOSTS.get(env, _APNS_HOSTS["production"])
    return {
        "key_path": os.path.expanduser(key_path),
        "key_id": key_id,
        "team_id": team_id,
        "bundle_id": bundle_id,
        "host": host,
    }


def is_configured() -> bool:
    return _config() is not None


def _auth_token(cfg: dict) -> Optional[str]:
    """Mint (or reuse) the ES256 provider JWT."""
    with _jwt_lock:
        now = time.time()
        if _jwt_cache["token"] and now - _jwt_cache["issued_at"] < _TOKEN_TTL_SECONDS:
            return _jwt_cache["token"]
        try:
            import jwt  # PyJWT[crypto] — already a core dependency

            with open(cfg["key_path"], encoding="utf-8") as f:
                signing_key = f.read()
            token = jwt.encode(
                {"iss": cfg["team_id"], "iat": int(now)},
                signing_key,
                algorithm="ES256",
                headers={"kid": cfg["key_id"]},
            )
            _jwt_cache["token"] = token
            _jwt_cache["issued_at"] = now
            return token
        except Exception as exc:
            logger.warning("APNs JWT mint failed: %s", exc)
            return None


def _send_one(client, cfg: dict, entry: dict, payload: dict, auth: str) -> None:
    """POST one notification; prune tokens Apple reports as dead."""
    token = entry.get("token", "")
    topic = entry.get("bundle_id") or cfg["bundle_id"]
    url = f"{cfg['host']}/3/device/{token}"
    headers = {
        "authorization": f"bearer {auth}",
        "apns-topic": topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    try:
        resp = client.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            return
        body = resp.text[:200]
        logger.warning("APNs %s for %s…: %s", resp.status_code, token[:8], body)
        if resp.status_code == 410 or "BadDeviceToken" in body or "Unregistered" in body:
            from tui_gateway.push_store import prune_token

            prune_token(token)
            logger.info("pruned dead APNs token %s…", token[:8])
    except Exception as exc:
        logger.warning("APNs send failed for %s…: %s", token[:8], exc)


def _deliver(payload: dict) -> None:
    """Send *payload* to every registered device (runs on a worker thread)."""
    cfg = _config()
    if cfg is None:
        return
    from tui_gateway.push_store import list_tokens

    tokens = list_tokens()
    if not tokens:
        return
    auth = _auth_token(cfg)
    if auth is None:
        return
    try:
        import httpx

        with httpx.Client(http2=True) as client:
            for entry in tokens:
                _send_one(client, cfg, entry, payload, auth)
    except ImportError:
        logger.warning("APNs disabled: httpx with http2 support unavailable")
    except Exception as exc:
        logger.warning("APNs delivery error: %s", exc)


def notify_all(
    title: str,
    body: str,
    *,
    subtitle: str = "",
    category: str = "",
    session_id: str = "",
    thread_id: str = "",
    extra: Optional[dict] = None,
) -> None:
    """Fire-and-forget push to all registered devices. No-op if unconfigured.

    ``session_id`` rides in the custom payload so the client's existing
    notification-tap routing (userInfo["session_id"]) opens the right session.
    """
    if not is_configured():
        return
    alert: dict = {"title": title[:120], "body": body[:220]}
    if subtitle:
        alert["subtitle"] = subtitle[:120]
    aps: dict = {"alert": alert, "sound": "default"}
    if category:
        aps["category"] = category
    if thread_id:
        aps["thread-id"] = thread_id
    payload: dict = {"aps": aps}
    if session_id:
        payload["session_id"] = session_id
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)

    threading.Thread(target=_deliver, args=(payload,), daemon=True).start()
