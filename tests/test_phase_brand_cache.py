"""Fix 2026-06-01 — brand_lookup_cache rebuild logic.

Verifies that `rebuild_brand_cache` walks the
tradename_commonname × tradename_manufacturer × tradename_formulation
Connects correctly and produces one row per (common_name, trade_name)
pair with manufacturer + formulation pre-resolved.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.orders.models import BrandLookupCache
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.brand_cache import (
    get_brands_for_common_name, rebuild_brand_cache,
)
from tests.conftest import requires_docker


async def _seed_two_brands_for_captan(db):
    db.add(CoshCoreItem(
        cosh_id="cosh:cn-captan", core_type="common_names_of_inputs",
        parent_cosh_id=None, translations={"en": "Captan"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh:tn-captan-a", core_type="trade_names",
        parent_cosh_id=None, translations={"en": "Captan-Pro"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh:tn-captan-b", core_type="trade_names",
        parent_cosh_id=None, translations={"en": "Captaf"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh:mfr-acme", core_type="input_manufacturers",
        parent_cosh_id=None, translations={"en": "AcmeCo"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh:mfr-rallis", core_type="input_manufacturers",
        parent_cosh_id=None, translations={"en": "Rallis"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh:fmt-wp", core_type="formulations",
        parent_cosh_id=None,
        translations={"en": "Wettable Powder"}, status="active",
    ))
    # Connects
    db.add(CoshConnectRow(
        connect_id="cn:tncn:a", connect_type="tradename_commonname",
        endpoints=[
            {"role": "trade_names", "cosh_id": "cosh:tn-captan-a", "position": 1},
            {"role": "common_names_of_inputs", "cosh_id": "cosh:cn-captan", "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id="cn:tncn:b", connect_type="tradename_commonname",
        endpoints=[
            {"role": "trade_names", "cosh_id": "cosh:tn-captan-b", "position": 1},
            {"role": "common_names_of_inputs", "cosh_id": "cosh:cn-captan", "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id="cn:tnm:a", connect_type="tradename_manufacturer",
        endpoints=[
            {"role": "trade_names", "cosh_id": "cosh:tn-captan-a", "position": 1},
            {"role": "input_manufacturers", "cosh_id": "cosh:mfr-acme", "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id="cn:tnm:b", connect_type="tradename_manufacturer",
        endpoints=[
            {"role": "trade_names", "cosh_id": "cosh:tn-captan-b", "position": 1},
            {"role": "input_manufacturers", "cosh_id": "cosh:mfr-rallis", "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id="cn:tnf:a", connect_type="tradename_formulation",
        endpoints=[
            {"role": "trade_names", "cosh_id": "cosh:tn-captan-a", "position": 1},
            {"role": "formulations", "cosh_id": "cosh:fmt-wp", "position": 2},
        ],
        status="active",
    ))
    await db.commit()


@requires_docker
@pytest.mark.asyncio
async def test_rebuild_writes_one_row_per_tradename_commonname_pair(db):
    await _seed_two_brands_for_captan(db)
    written = await rebuild_brand_cache(db)
    assert written == 2

    rows = (await db.execute(
        select(BrandLookupCache).where(
            BrandLookupCache.common_name_cosh_id == "cosh:cn-captan",
        ).order_by(BrandLookupCache.trade_name)
    )).scalars().all()
    assert [r.trade_name for r in rows] == ["Captaf", "Captan-Pro"]
    captaf = next(r for r in rows if r.trade_name == "Captaf")
    captan_pro = next(r for r in rows if r.trade_name == "Captan-Pro")
    assert captaf.manufacturer_name == "Rallis"
    assert captan_pro.manufacturer_name == "AcmeCo"
    assert captan_pro.formulation_name == "Wettable Powder"
    # Captaf has no tradename_formulation row → formulation is NULL.
    assert captaf.formulation_name is None


@requires_docker
@pytest.mark.asyncio
async def test_rebuild_is_idempotent(db):
    """Running rebuild twice must not duplicate rows or accumulate stale data."""
    await _seed_two_brands_for_captan(db)
    await rebuild_brand_cache(db)
    second = await rebuild_brand_cache(db)
    assert second == 2
    total = (await db.execute(
        select(BrandLookupCache)
    )).scalars().all()
    assert len(total) == 2


@requires_docker
@pytest.mark.asyncio
async def test_get_brands_lazy_bootstraps_empty_cache(db):
    """Cache empty + Cosh has data → first read populates the cache."""
    await _seed_two_brands_for_captan(db)
    # Don't pre-populate; just read.
    rows = await get_brands_for_common_name(db, "cosh:cn-captan")
    assert len(rows) == 2
    assert {r.trade_name for r in rows} == {"Captaf", "Captan-Pro"}


@requires_docker
@pytest.mark.asyncio
async def test_get_brands_returns_empty_when_common_name_uncovered(db):
    """Cache populated but the queried common name has no rows → empty list,
    no re-bootstrap (cache isn't stale, the CN is just uncovered)."""
    await _seed_two_brands_for_captan(db)
    await rebuild_brand_cache(db)
    rows = await get_brands_for_common_name(db, "cosh:cn-mancozeb")
    assert rows == []


@requires_docker
@pytest.mark.asyncio
async def test_rebuild_skips_rows_with_inactive_cores(db):
    """A trade_names row marked inactive shouldn't appear in the cache."""
    await _seed_two_brands_for_captan(db)
    # Flip Captaf's TN core to inactive.
    captaf = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "cosh:tn-captan-b")
    )).scalar_one()
    captaf.status = "inactive"
    await db.commit()

    written = await rebuild_brand_cache(db)
    assert written == 1
    rows = await get_brands_for_common_name(db, "cosh:cn-captan")
    assert [r.trade_name for r in rows] == ["Captan-Pro"]
