"""FCM (Firebase Cloud Messaging) push-notification service.

Single transactional helper `send_fcm(token, title, body, data)`
that the BL-09 / BL-12 / BL-14 wiring calls when a user with an
FCM token needs to be notified.

Configuration is via the standard Google ADC (Application Default
Credentials) pattern: set the `GOOGLE_APPLICATION_CREDENTIALS`
env var to the path of a service-account JSON file. Project info
is in `~/.claude/.../memory/project_rootstalk_firebase.md`
(non-secret pointers only — the JSON itself stays out of the repo).

Graceful no-creds fallback: if Firebase isn't initialisable
(missing env var, malformed JSON, network issue at boot),
`send_fcm` logs a warning and returns False rather than raising.
This lets the rest of the codebase always call `send_fcm` without
having to check whether Firebase is configured first; SMS and
DB-write paths run unaffected. Same defensive pattern as
`send_sms` (BL-09 audit batch 2).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Firebase Admin SDK is global-singleton. Initialise lazily once and
# cache the App handle. A lock guards concurrent first-call init.
_app = None
_app_init_lock = threading.Lock()
_app_init_attempted = False


def _get_app():
    """Lazy-initialise the Firebase App. Returns None on any error
    (missing creds, malformed JSON, etc). Caches the result so we
    don't retry init on every call."""
    global _app, _app_init_attempted
    if _app is not None:
        return _app
    if _app_init_attempted:
        return None
    with _app_init_lock:
        if _app is not None:
            return _app
        if _app_init_attempted:
            return None
        _app_init_attempted = True
        try:
            import firebase_admin
            if firebase_admin._apps:
                _app = firebase_admin.get_app()
            else:
                # initialize_app() with no args uses GOOGLE_APPLICATION_CREDENTIALS.
                _app = firebase_admin.initialize_app()
            logger.info("FCM initialised (Firebase Admin SDK ready).")
            return _app
        except Exception as exc:
            logger.warning(
                "FCM not initialised — push notifications disabled. "
                "Set GOOGLE_APPLICATION_CREDENTIALS to a service-account "
                "JSON path to enable. (cause: %s)", exc,
            )
            return None


async def send_fcm(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """Send a single FCM push notification. Returns True on success,
    False on any failure (no creds, no token, send error).

    `data` is an optional payload dict — values must be strings (FCM
    constraint); we coerce automatically. The receiving PWA can
    inspect `data` to deep-link into the relevant screen (e.g.
    `data={"type": "ALERT", "subscription_id": "..."}`).
    """
    app = _get_app()
    if app is None:
        return False
    if not token:
        logger.debug("FCM skipped — no token. title=%r", title)
        return False
    try:
        from firebase_admin import messaging
        # FCM data values must be strings. Coerce defensively so callers
        # can pass int / UUID / datetime without thinking about it.
        coerced_data = {k: str(v) for k, v in (data or {}).items()}
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=coerced_data,
            token=token,
        )
        # firebase_admin.messaging.send is sync; offload to executor
        # so we don't block the FastAPI/Celery event loop on network IO.
        loop = asyncio.get_event_loop()
        message_id = await loop.run_in_executor(
            None, lambda: messaging.send(message),
        )
        logger.info("FCM sent: %s (title=%r)", message_id, title)
        return True
    except Exception as exc:
        # firebase_admin raises a typed exception hierarchy
        # (UnregisteredError when the device uninstalled the app, etc).
        # Treat all as "send failed, log and continue" — the caller
        # already accepts a bool and won't propagate the failure.
        logger.error("FCM send failed: %s (title=%r)", exc, title)
        return False


def reset_for_tests() -> None:
    """Test-only: clear cached app so a subsequent `_get_app` call
    re-tries initialisation. Useful for monkeypatch-driven tests
    that swap out firebase_admin between cases."""
    global _app, _app_init_attempted
    _app = None
    _app_init_attempted = False
