"""Dealer dealerships V2 — Cosh-driven manufacturer catalog + selection.

Pins:
  - GET /dealer/manufacturers-catalog rejects unknown categories
  - POST /dealer/dealerships persists manufacturer_cosh_id + category
  - POST is idempotent on (dealer, cosh_id, category) — re-adding the
    same manufacturer in the same category returns the existing row
  - GET /dealer/dealerships filters by category when supplied
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.orders.models import DealerManufacturerCatalog, DealerRelationship
from app.modules.orders.router import (
    add_dealership, dealer_manufacturers_catalog, list_dealerships,
)
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_catalog_rejects_unknown_category(db):
    user = await make_user(db, name="Dealer Cat")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await dealer_manufacturers_catalog(
            category="WIDGETS", db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert "PESTICIDE" in exc.value.detail
    assert "FERTILIZER" in exc.value.detail


@requires_docker
@pytest.mark.asyncio
async def test_add_persists_cosh_id_and_category(db):
    user = await make_user(db, name="Dealer Add")
    await db.commit()

    out = await add_dealership(
        data={
            "manufacturer_name": "Bayer",
            "manufacturer_cosh_id": "input_manufacturers:bayer",
            "category": "pesticide",  # lowercased — should normalise
        },
        db=db, current_user=user,
    )
    assert out["manufacturer_name"] == "Bayer"
    rel = (await db.execute(
        select(DealerRelationship).where(DealerRelationship.id == out["id"])
    )).scalar_one()
    assert rel.manufacturer_cosh_id == "input_manufacturers:bayer"
    assert rel.category == "PESTICIDE"


@requires_docker
@pytest.mark.asyncio
async def test_add_is_idempotent_per_cosh_id_and_category(db):
    user = await make_user(db, name="Dealer Idem")
    await db.commit()

    payload = {
        "manufacturer_name": "UPL",
        "manufacturer_cosh_id": "input_manufacturers:upl",
        "category": "PESTICIDE",
    }
    a = await add_dealership(data=payload, db=db, current_user=user)
    b = await add_dealership(data=payload, db=db, current_user=user)
    assert a["id"] == b["id"]
    rows = (await db.execute(
        select(DealerRelationship).where(
            DealerRelationship.dealer_user_id == user.id,
            DealerRelationship.manufacturer_cosh_id == "input_manufacturers:upl",
        )
    )).scalars().all()
    assert len(rows) == 1


@requires_docker
@pytest.mark.asyncio
async def test_same_manufacturer_in_both_categories_is_two_rows(db):
    """Bayer pesticides and Bayer fertilizers are separate dealership
    contracts — selecting one doesn't auto-select the other."""
    user = await make_user(db, name="Dealer Both")
    await db.commit()

    await add_dealership(
        data={
            "manufacturer_name": "Bayer",
            "manufacturer_cosh_id": "input_manufacturers:bayer",
            "category": "PESTICIDE",
        }, db=db, current_user=user,
    )
    await add_dealership(
        data={
            "manufacturer_name": "Bayer",
            "manufacturer_cosh_id": "input_manufacturers:bayer",
            "category": "FERTILIZER",
        }, db=db, current_user=user,
    )

    pest = await list_dealerships(category="PESTICIDE", db=db, current_user=user)
    fert = await list_dealerships(category="FERTILIZER", db=db, current_user=user)
    assert len(pest) == 1 and pest[0]["category"] == "PESTICIDE"
    assert len(fert) == 1 and fert[0]["category"] == "FERTILIZER"


@requires_docker
@pytest.mark.asyncio
async def test_catalog_reads_from_materialised_table(db):
    """Catalog endpoint reads from dealer_manufacturer_catalog
    rows directly — confirms we're not re-walking Cosh per call.
    Seeded rows surface in the response sorted by name."""
    user = await make_user(db, name="Dealer Mat")
    db.add(DealerManufacturerCatalog(
        category="PESTICIDE",
        manufacturer_cosh_id="input_manufacturers:zeta",
        manufacturer_name="Zeta Crop Care",
    ))
    db.add(DealerManufacturerCatalog(
        category="PESTICIDE",
        manufacturer_cosh_id="input_manufacturers:alpha",
        manufacturer_name="Alpha AgriSciences",
    ))
    db.add(DealerManufacturerCatalog(
        category="FERTILIZER",
        manufacturer_cosh_id="input_manufacturers:beta",
        manufacturer_name="Beta Fertilizers",
    ))
    await db.commit()

    pest = await dealer_manufacturers_catalog(
        category="PESTICIDE", db=db, current_user=user,
    )
    assert [r["name"] for r in pest] == [
        "Alpha AgriSciences", "Zeta Crop Care",
    ]
    # FERTILIZER seed is isolated to its own category — no leak.
    fert = await dealer_manufacturers_catalog(
        category="FERTILIZER", db=db, current_user=user,
    )
    assert [r["name"] for r in fert] == ["Beta Fertilizers"]


@requires_docker
@pytest.mark.asyncio
async def test_list_with_no_filter_returns_all_categories(db):
    user = await make_user(db, name="Dealer All")
    await db.commit()
    await add_dealership(
        data={
            "manufacturer_name": "Coromandel",
            "manufacturer_cosh_id": "input_manufacturers:coro",
            "category": "FERTILIZER",
        }, db=db, current_user=user,
    )
    await add_dealership(
        data={
            "manufacturer_name": "Syngenta",
            "manufacturer_cosh_id": "input_manufacturers:syng",
            "category": "PESTICIDE",
        }, db=db, current_user=user,
    )
    all_rows = await list_dealerships(category=None, db=db, current_user=user)
    assert len(all_rows) == 2
    assert {r["manufacturer_name"] for r in all_rows} == {"Coromandel", "Syngenta"}
