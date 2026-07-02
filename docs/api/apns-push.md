# APNs Push Notifications for HermesNative

Remote push delivery to HermesNative devices (macOS + iOS) via Apple Push
Notification service. Unlike WebSocket events — which only reach a live,
connected app — APNs pushes arrive with the app dead, the Mac asleep, or on
another device entirely.

## What gets pushed

| Event | Push | Category |
|-------|------|----------|
| `approval.request` | "Approval Required" + redacted command | `approval` |
| `clarify.request` | "Question" + the question | `clarify` |
| `message.complete` (status=complete) | "Response Complete" + text preview | `responseComplete` |
| cron run completion (`mark_job_run`) | "Cron: {name}" + ✓ ok / ✗ error | `cronComplete` |

Streaming deltas and tool chatter are deliberately **not** pushed.

Every session-scoped push carries `session_id` in the custom payload, matching
the client's existing notification-tap routing (`userInfo["session_id"]`).

## Gateway setup

1. In the [Apple Developer portal](https://developer.apple.com/account/resources/authkeys/list),
   create an **APNs Auth Key** (.p8). Note the **Key ID** and your **Team ID**.
2. Copy the key somewhere the gateway can read, e.g. `~/.hermes/AuthKey_ABC123.p8`.
3. Configure the gateway environment (e.g. `~/.hermes/.env`):

```bash
APNS_KEY_PATH=~/.hermes/AuthKey_ABC123.p8
APNS_KEY_ID=ABC123DEFG          # 10-char key id
APNS_TEAM_ID=TEAM456789         # 10-char team id
APNS_BUNDLE_ID=com.researchoors.HermesNative.macOS   # default topic
# APNS_ENV=sandbox              # for Xcode-run debug builds; default production
```

4. Install the HTTP/2 dependency: `pip install 'hermes-agent[apns]'` (or
   `uv sync --extra apns`). JWT signing uses PyJWT[crypto], already a core dep.

APNs is enabled iff the four `APNS_*` vars are set. Unconfigured, every push
call is a silent no-op — no behavior change.

## Client registration RPCs

```jsonc
// Register (idempotent on token; refreshes metadata + last_seen)
{"method": "push.register", "params": {
  "token": "<hex device token>",
  "platform": "macos",                       // or "ios"
  "device_name": "Ethen's MacBook Pro",      // optional
  "bundle_id": "com.researchoors.HermesNative.macOS"  // optional per-device topic
}}
// → {"registered": true, "apns_configured": true, "entry": {...}}

// Unregister (e.g. sign-out)
{"method": "push.unregister", "params": {"token": "<hex device token>"}}
// → {"removed": true}
```

`apns_configured: false` in the register response tells the client the gateway
has no APNs credentials — surface that in settings rather than failing.

Tokens live in `~/.hermes/push_tokens.json` (bounded at 50, LRU-evicted).
Tokens Apple reports dead (410 Unregistered / BadDeviceToken) are pruned
automatically on send.

## Notes

- **Auth model:** token-based (JWT ES256, `kid` header) over APNs HTTP/2 —
  no push certificates to renew. Provider JWTs are cached ~40 min.
- **Delivery is fire-and-forget** on a daemon thread; event emission and the
  cron scheduler never block on Apple.
- **macOS + iOS topics differ** — the app registers with its own `bundle_id`
  per device, so one gateway pushes to both.
- **Sandbox vs production:** Xcode-run debug builds get sandbox tokens; set
  `APNS_ENV=sandbox` when testing, unset (or `production`) for TestFlight /
  notarized builds.
