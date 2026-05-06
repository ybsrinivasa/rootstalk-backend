"""BL-15 audit — DB-backed integration tests for the V1 (Option B)
reference number generator.

Pure-function coverage of the format helpers lives in
`tests/test_bl15.py` (14 tests). This file drives
`_generate_reference_for_sub` directly against the testcontainer DB
to verify the live max+1 query produces sequential, format-compliant
references, scoped per (client_code, year).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.modules.subscriptions.router import _generate_reference_for_sub
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


# ── Format ────────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_first_reference_for_a_client_uses_v1_format(db):
    """Brand-new client + brand-new year → first reference is
    `XX-YY-000001`. Pre-fix the live route returned a 4-digit random
    suffix like `PADMASHALI26-3847`."""
    client = await make_client(db, full_name="Padmashali Seeds")
    client.short_name = "padmashali"
    await db.commit()

    ref = await _generate_reference_for_sub(db, client.id)
    assert ref.startswith("PA-")
    assert ref.endswith("-000001")
    # Full V1 format: XX-YY-NNNNNN — three hyphens, 13 chars exactly
    # (year + sequence widths are fixed; client code may vary
    # length only via the fallback "RT" path).
    parts = ref.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 2
    assert len(parts[1]) == 2
    assert len(parts[2]) == 6


@requires_docker
@pytest.mark.asyncio
async def test_short_name_too_short_falls_back_to_rt_code(db):
    """Spec stop-gap: a client whose short_name is too short (or
    empty) gets the `RT` fallback rather than a malformed 1-char
    code that would break the format."""
    client = await make_client(db)
    client.short_name = "a"
    await db.commit()

    ref = await _generate_reference_for_sub(db, client.id)
    assert ref.startswith("RT-")


# ── Sequential numbering ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_second_reference_for_same_client_year_increments(db):
    """Headline collision-fix test: two sequential generations for
    the same (client, year) bucket return v=1 then v=2, never the
    same number twice. Pre-fix's 4-digit random had a real birthday-
    collision rate."""
    farmer1 = await make_user(db, name="F1")
    farmer2 = await make_user(db, name="F2")
    client = await make_client(db)
    client.short_name = "padmashali"
    pkg = await make_package(db, client)
    sub1 = await make_subscription(db, farmer=farmer1, client=client, package=pkg)
    sub2 = await make_subscription(db, farmer=farmer2, client=client, package=pkg)
    await db.commit()

    sub1.reference_number = await _generate_reference_for_sub(db, client.id)
    sub1.status = SubscriptionStatus.ACTIVE
    await db.commit()

    sub2.reference_number = await _generate_reference_for_sub(db, client.id)
    sub2.status = SubscriptionStatus.ACTIVE
    await db.commit()

    assert sub1.reference_number.endswith("-000001")
    assert sub2.reference_number.endswith("-000002")
    assert sub1.reference_number != sub2.reference_number


@requires_docker
@pytest.mark.asyncio
async def test_per_client_buckets_are_independent(db):
    """Two different clients in the same year each start their own
    sequence at v=1. The LIKE prefix scopes the max+1 query to the
    client's own bucket."""
    client_a = await make_client(db, full_name="Acme Seeds")
    client_a.short_name = "acme"
    client_b = await make_client(db, full_name="Padmashali Seeds")
    client_b.short_name = "padmashali"
    await db.commit()

    ref_a1 = await _generate_reference_for_sub(db, client_a.id)
    # Persist via a real Subscription so the LIKE query sees it on
    # the next call.
    farmer = await make_user(db)
    pkg_a = await make_package(db, client_a)
    sub_a = await make_subscription(db, farmer=farmer, client=client_a, package=pkg_a)
    sub_a.reference_number = ref_a1
    await db.commit()

    ref_b1 = await _generate_reference_for_sub(db, client_b.id)

    assert ref_a1.startswith("AC-") and ref_a1.endswith("-000001")
    assert ref_b1.startswith("PA-") and ref_b1.endswith("-000001")


# ── Legacy V0 references are invisible to the V1 max+1 query ─────────────────

@requires_docker
@pytest.mark.asyncio
async def test_legacy_v0_reference_does_not_poison_v1_sequence(db):
    """A legacy reference like `PADMASHALI26-3847` matches neither
    the LIKE pattern `PA-26-%` (no separator after the 2-char code)
    nor the sequence parser. The next V1 generation correctly starts
    at v=1 in the new format. Spec rule: legacy references stay
    untouched; new ones use the V1 format."""
    farmer = await make_user(db)
    client = await make_client(db, full_name="Padmashali Seeds")
    client.short_name = "padmashali"
    pkg = await make_package(db, client)
    sub_legacy = await make_subscription(
        db, farmer=farmer, client=client, package=pkg,
    )
    sub_legacy.reference_number = "PADMASHALI26-3847"
    await db.commit()

    ref = await _generate_reference_for_sub(db, client.id)
    assert ref == f"PA-26-000001" or ref.endswith("-000001")
    # Legacy reference is still on the row (Spec: "Never updated").
    refreshed = (await db.execute(
        select(Subscription).where(Subscription.id == sub_legacy.id)
    )).scalar_one()
    assert refreshed.reference_number == "PADMASHALI26-3847"
