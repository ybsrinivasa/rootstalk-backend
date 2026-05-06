"""Tests for `_base_url()` resolution order.

Pre-fix: hardcoded `localhost:3004` in dev / `https://rootstalk.in`
elsewhere. Post-fix: env-driven via `FRONTEND_BASE_URL`, with the
hardcoded values as fallbacks for backwards compatibility.

Critical for the testing-server rollout — the testing server lives
at https://rstalk.eywa.farm and onboarding emails must point there,
not at the production rootstalk.in domain.
"""
from __future__ import annotations

from app.modules.clients import router as clients_router
from app.config import settings


def test_uses_frontend_base_url_when_set(monkeypatch):
    """Explicit env var wins regardless of environment value — this
    is the testing-server case."""
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk.eywa.farm")
    monkeypatch.setattr(settings, "environment", "staging")
    assert clients_router._base_url() == "https://rstalk.eywa.farm"


def test_strips_trailing_slash(monkeypatch):
    """Env vars frequently come in with a trailing slash; the call
    sites do `f"{_base_url()}/onboarding/{token}"` so a double slash
    would yield `//onboarding/...`. Strip the trailing slash here so
    callers don't have to think about it."""
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk.eywa.farm/")
    monkeypatch.setattr(settings, "environment", "staging")
    assert clients_router._base_url() == "https://rstalk.eywa.farm"


def test_dev_fallback_when_unset(monkeypatch):
    """Backwards compat for local dev — no env var, environment=dev,
    return the hardcoded localhost URL."""
    monkeypatch.setattr(settings, "frontend_base_url", "")
    monkeypatch.setattr(settings, "environment", "development")
    assert clients_router._base_url() == "http://localhost:3004"


def test_prod_fallback_when_unset_in_non_dev(monkeypatch):
    """Backwards compat for the pre-config-driven prod deploy. A
    startup warning fires (in main.py) — see that path tested via
    log capture in tests/test_startup_warnings.py if needed."""
    monkeypatch.setattr(settings, "frontend_base_url", "")
    monkeypatch.setattr(settings, "environment", "production")
    assert clients_router._base_url() == "https://rootstalk.in"
