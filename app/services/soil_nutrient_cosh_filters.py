"""Cosh walks specific to the Soil Nutrient module.

Decision U (2026-10-17) — Client Portal product picker is filtered
server-side to common_names manufactured by the Client's linked
Manufacturers. This module implements the Cosh walk:

    client ─(client_cosh_manufacturers M:N)─→ manufacturer cosh_ids
                                                 │
                                                 ▼
    tradename_manufacturer Connect ─────→ trade_name cosh_ids
                                                 │
                                                 ▼
    tradename_commonname Connect ────────→ common_name cosh_ids

The resulting common_name set intersects with the output of
`load_fertiliser_candidates()` to produce the picker feed.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import ClientCoshManufacturer
from app.modules.sync.models import CoshConnectRow
from app.services.cosh_constants import (
    COSH_COMMON_NAMES_CORE,
    COSH_INPUT_MANUFACTURERS_CORE,
    COSH_TRADE_NAMES_CORE,
    COSH_TRADENAME_COMMONNAME_CONNECT,
    COSH_TRADENAME_MANUFACTURER_CONNECT,
)


async def common_names_for_client_manufacturers(
    db: AsyncSession, client_id: str,
) -> set[str] | None:
    """Return the set of common_name cosh_ids for products made by
    any of the Client's linked Manufacturers.

    Returns `None` when the Client has no linked Manufacturers —
    callers interpret that as "no filter available; show empty
    picker" (differentiating from the empty-set case where
    manufacturers exist but Cosh has no mapped common_names).
    """
    mfr_rows = (await db.execute(
        select(ClientCoshManufacturer.cosh_manufacturer_id).where(
            ClientCoshManufacturer.client_id == client_id,
        )
    )).all()
    mfr_ids = {r[0] for r in mfr_rows}
    if not mfr_ids:
        return None

    # Step 1 — manufacturer → trade_names via tradename_manufacturer
    tnm_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_MANUFACTURER_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    tn_ids: set[str] = set()
    for row in tnm_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (row.endpoints or [])}
        if ep.get(COSH_INPUT_MANUFACTURERS_CORE) in mfr_ids:
            tn = ep.get(COSH_TRADE_NAMES_CORE)
            if tn:
                tn_ids.add(tn)
    if not tn_ids:
        return set()

    # Step 2 — trade_names → common_names via tradename_commonname
    tncn_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_COMMONNAME_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    cn_ids: set[str] = set()
    for row in tncn_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (row.endpoints or [])}
        if ep.get(COSH_TRADE_NAMES_CORE) in tn_ids:
            cn = ep.get(COSH_COMMON_NAMES_CORE)
            if cn:
                cn_ids.add(cn)
    return cn_ids
