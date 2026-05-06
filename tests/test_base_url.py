"""Tests for `_base_url()` resolution order.

Pre-fix: hardcoded `localhost:3004` in dev / `https://rootstalk.in`
elsewhere. Post-fix: env-driven via `FRONTEND_BASE_URL`. The
production fallback was dropped on 2026-05-06 once `rootstalk.in`
got earmarked for the PWA — the old fallback was actively wrong
for the SA/CA portal. Non-dev now raises if the env var is unset
(both at startup in app/main.py and inline here as belt-and-
suspenders).
"""
from __future__ import annotations

import pytest

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


def test_raises_when_unset_in_non_dev(monkeypatch):
    """Non-dev requires the env var. If something bypasses the
    startup gate in main.py and we reach `_base_url()` with an
    unset value, fail loudly rather than silently emitting the
    wrong host (the previous behaviour silently used the PWA
    domain `rootstalk.in` for the SA/CA portal — actively wrong)."""
    monkeypatch.setattr(settings, "frontend_base_url", "")
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(RuntimeError, match="FRONTEND_BASE_URL"):
        clients_router._base_url()
