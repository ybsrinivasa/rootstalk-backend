"""Integration tests for GET /client/{id}/subscription-pool/quote.

Covers the route's translation of pricing-service output to JSON,
validation responses (422), and SA-agnostic access (any authenticated
user can ask for a quote).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.subscriptions.router import get_pool_quote
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


@requires_docker
@pytest.mark.asyncio
async def test_quote_returns_paise_and_rupee_strings(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    out = await get_pool_quote(
        client_id=client.id, units=100,
        db=db, current_user=user,
    )
    assert out["units"] == 100
    assert out["client_id"] == client.id
    assert out["gross_paise"] == 19900_00
    assert out["discount_paise"] == 47478
    assert out["total_paise"] == 1942522
    assert out["gross_rupees"] == "19900.00"
    assert out["discount_rupees"] == "474.78"
    assert out["total_rupees"] == "19425.22"
    assert out["per_unit_gross_paise"] == 19900
    assert out["min_units"] == 1
    assert out["max_units"] == 50_000


@requires_docker
@pytest.mark.asyncio
async def test_quote_rejects_zero_units_with_422(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await get_pool_quote(
            client_id=client.id, units=0,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_quote_rejects_above_max_with_422(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await get_pool_quote(
            client_id=client.id, units=999_999,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_quote_minimum_units(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    out = await get_pool_quote(
        client_id=client.id, units=1,
        db=db, current_user=user,
    )
    # ₹199 gross − ₹0.50 discount → ₹198.50.
    assert out["gross_paise"] == 19900
    assert out["discount_paise"] == 50
    assert out["total_paise"] == 19850
    assert out["total_rupees"] == "198.50"
