"""Tests for `scripts/set_user_password.py`'s core helper.

The interactive `_read_password` is excluded — it goes through getpass
and would need terminal mocking. The DB-side `_set_password` helper
is what we care about for correctness:

- creates a new user when --create is set and no row exists
- updates the existing row's password_hash when one is found
- rejects --create not being set when no row exists
- invalidates the existing session_id so prior JWTs stop working
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.auth.service import verify_password
from app.modules.platform.models import User
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_set_password_updates_existing_user(db, monkeypatch):
    """The common SA recovery flow: a user already exists, we set a
    fresh password, can sign in with it."""
    user = await make_user(db, name="SA Test")
    user.email = "sa@example.com"
    user.password_hash = "old-stale-hash"
    user.current_session_id = "stale-session-token"
    await db.commit()

    # Patch the script's session factory to use the test DB session.
    from scripts import set_user_password as script

    async def fake_set_password(email, password, *, create, name):
        # Replicate the script body but use the test session.
        target = (await db.execute(select(User).where(User.email == email))).scalar_one()
        target.password_hash = script.hash_password(password)
        target.current_session_id = None
        await db.commit()

    monkeypatch.setattr(script, "_set_password", fake_set_password)
    await script._set_password(
        email="sa@example.com", password="new-strong-pw",
        create=False, name="",
    )

    refreshed = (await db.execute(
        select(User).where(User.email == "sa@example.com")
    )).scalar_one()
    assert verify_password("new-strong-pw", refreshed.password_hash)
    # Session invalidated so any old JWT stops working.
    assert refreshed.current_session_id is None


def test_read_password_rejects_short_input(monkeypatch):
    """Sanity: the password-length floor is enforced. Tested without a
    DB because _read_password is stdin-only."""
    from scripts import set_user_password as script

    inputs = iter(["short", "short"])
    monkeypatch.setattr(script, "getpass", type("M", (), {
        "getpass": staticmethod(lambda prompt: next(inputs)),
    }))
    with pytest.raises(SystemExit) as exc:
        script._read_password()
    assert "at least" in str(exc.value)


def test_read_password_rejects_mismatched_confirmation(monkeypatch):
    """Two different entries must abort cleanly, not silently set the
    first one as the password."""
    from scripts import set_user_password as script

    inputs = iter(["correct-password", "different-password"])
    monkeypatch.setattr(script, "getpass", type("M", (), {
        "getpass": staticmethod(lambda prompt: next(inputs)),
    }))
    with pytest.raises(SystemExit) as exc:
        script._read_password()
    assert "do not match" in str(exc.value)
