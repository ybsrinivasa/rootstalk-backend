"""FCM service — unit tests with mocked firebase_admin.

Covers the no-creds graceful fallback path (the most exercised one
in dev / test envs), the happy-path success, and the per-call
failure mode (e.g. unregistered device token). All hermetic — no
network, no real Firebase project hit.
"""
from __future__ import annotations

import pytest

from app.services import fcm_service


@pytest.fixture(autouse=True)
def reset_fcm_state():
    """Clear the module-level cached app between tests so each
    monkeypatch starts from scratch."""
    fcm_service.reset_for_tests()
    yield
    fcm_service.reset_for_tests()


# ── No-creds graceful fallback ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_fcm_returns_false_when_firebase_not_initialised(monkeypatch):
    """The default state in dev / test envs: GOOGLE_APPLICATION_CREDENTIALS
    isn't set, so Firebase init silently caches None. send_fcm returns
    False without raising, letting BL-09 / BL-12 / BL-14 callers
    proceed with their SMS / DB-write paths."""
    # _get_app catches its own init errors and returns None — emulate
    # that final state directly.
    monkeypatch.setattr(fcm_service, "_get_app", lambda: None)

    result = await fcm_service.send_fcm(
        token="fake-token", title="Hi", body="Test",
    )
    assert result is False


@pytest.mark.asyncio
async def test_send_fcm_returns_false_for_empty_token(monkeypatch):
    """Even with Firebase initialised, an empty token short-circuits
    rather than calling messaging.send (which would raise). Lets
    callers always invoke send_fcm without first checking whether
    the user has a token."""
    monkeypatch.setattr(fcm_service, "_get_app", lambda: object())

    assert await fcm_service.send_fcm(token="", title="t", body="b") is False
    assert await fcm_service.send_fcm(token=None, title="t", body="b") is False  # type: ignore[arg-type]


# ── Happy-path send (mocked messaging.send) ───────────────────────────────────

@pytest.mark.asyncio
async def test_send_fcm_returns_true_on_successful_send(monkeypatch):
    """messaging.send returns a message_id string on success → True."""
    monkeypatch.setattr(fcm_service, "_get_app", lambda: object())

    fake_messaging = type("M", (), {})()
    fake_messaging.Notification = lambda title, body: ("Notif", title, body)
    fake_messaging.Message = lambda notification, data, token: (
        "Msg", notification, data, token,
    )
    fake_messaging.send = lambda msg: "fake-message-id-123"

    import sys
    # Inject fake firebase_admin.messaging into module cache so
    # `from firebase_admin import messaging` inside send_fcm picks it up.
    fake_firebase = type(sys)("firebase_admin")
    fake_firebase.messaging = fake_messaging
    monkeypatch.setitem(sys.modules, "firebase_admin", fake_firebase)
    monkeypatch.setitem(sys.modules, "firebase_admin.messaging", fake_messaging)

    result = await fcm_service.send_fcm(
        token="real-token", title="Hello", body="World",
    )
    assert result is True


# ── Send-failure path (e.g. unregistered device) ──────────────────────────────

@pytest.mark.asyncio
async def test_send_fcm_returns_false_when_messaging_send_raises(monkeypatch):
    """A device that uninstalled the app surfaces as
    UnregisteredError from firebase_admin.messaging.send. send_fcm
    catches all exceptions and returns False — caller continues."""
    monkeypatch.setattr(fcm_service, "_get_app", lambda: object())

    fake_messaging = type("M", (), {})()
    fake_messaging.Notification = lambda title, body: ("Notif",)
    fake_messaging.Message = lambda notification, data, token: ("Msg",)

    def _raise(_):
        raise RuntimeError("UnregisteredError(404)")
    fake_messaging.send = _raise

    import sys
    fake_firebase = type(sys)("firebase_admin")
    fake_firebase.messaging = fake_messaging
    monkeypatch.setitem(sys.modules, "firebase_admin", fake_firebase)
    monkeypatch.setitem(sys.modules, "firebase_admin.messaging", fake_messaging)

    assert await fcm_service.send_fcm(
        token="stale-token", title="X", body="Y",
    ) is False


# ── Data-payload coercion ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_fcm_coerces_data_values_to_strings(monkeypatch):
    """FCM requires data dict values to be strings. Callers may pass
    int / UUID / datetime; the service coerces via str() before
    handing to messaging.Message."""
    monkeypatch.setattr(fcm_service, "_get_app", lambda: object())

    captured: dict = {}

    fake_messaging = type("M", (), {})()
    fake_messaging.Notification = lambda title, body: object()

    def _make_msg(notification, data, token):
        captured["data"] = data
        return object()

    fake_messaging.Message = _make_msg
    fake_messaging.send = lambda msg: "ok-id"

    import sys
    fake_firebase = type(sys)("firebase_admin")
    fake_firebase.messaging = fake_messaging
    monkeypatch.setitem(sys.modules, "firebase_admin", fake_firebase)
    monkeypatch.setitem(sys.modules, "firebase_admin.messaging", fake_messaging)

    await fcm_service.send_fcm(
        token="t", title="T", body="B",
        data={"int_field": 42, "str_field": "hi"},
    )
    assert captured["data"] == {"int_field": "42", "str_field": "hi"}
    # All values are strings post-coercion (FCM requirement).
    assert all(isinstance(v, str) for v in captured["data"].values())


# ── Caching: init failure isn't retried per-call ──────────────────────────────

@pytest.mark.asyncio
async def test_init_failure_is_cached_to_avoid_retry_storm(monkeypatch):
    """If GOOGLE_APPLICATION_CREDENTIALS is malformed at startup, every
    incoming alert / query / approval would otherwise retry init and
    log noisily. The lazy-init caches the failure once; subsequent
    `_get_app()` calls return None without re-trying."""
    call_count = {"n": 0}

    def _fail_once_count(*a, **k):
        call_count["n"] += 1
        raise RuntimeError("bad creds")

    # Replace firebase_admin.initialize_app via patching the module
    # before _get_app is called.
    import sys
    fake_firebase = type(sys)("firebase_admin")
    fake_firebase._apps = {}
    fake_firebase.initialize_app = _fail_once_count
    fake_firebase.get_app = lambda: None
    monkeypatch.setitem(sys.modules, "firebase_admin", fake_firebase)

    # First call: init attempted, fails, returns None.
    assert fcm_service._get_app() is None
    assert call_count["n"] == 1
    # Second call: cached failure, init NOT re-attempted.
    assert fcm_service._get_app() is None
    assert call_count["n"] == 1
