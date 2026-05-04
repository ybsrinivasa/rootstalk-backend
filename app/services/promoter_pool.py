"""Per-promoter subscription-pool service.

Mediates the four operations on a `promoter_allocations` row:

- `allocate_to_promoter`  — CA gives N units from the company's
  unallocated balance to a specific promoter's row.
- `reclaim_from_promoter` — CA takes N un-consumed units back from a
  promoter's row to the company's unallocated balance.
- `consume_for_assignment` — promoter draws down their own row by 1
  when they successfully assign a subscription to a farmer.
- `get_promoter_balance` / `get_company_unallocated_balance` —
  read-side accessors used by the new endpoints and by the
  pre-existing `_get_pool_balance` helper.

Invariants:
- `units_balance == allocated_total - reclaimed_total - consumed_total`
- A promoter's row is created lazily (via `allocate_to_promoter` or
  the legacy backfill); reclaim/consume against a missing row raise
  ValueError.
- All four operations validate non-negativity of the post-state and
  raise ValueError if it would go negative.

Concurrency: SELECT ... FOR UPDATE locks the row for the duration of
each mutation so two simultaneous CA actions on the same promoter
don't drift the running totals.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.subscriptions.models import SubscriptionPool
from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation


# ── Read accessors ──────────────────────────────────────────────────────────

async def get_promoter_balance(
    db: AsyncSession, client_id: str, promoter_user_id: str,
) -> int:
    """Return the promoter's currently-available units (0 if no row exists)."""
    row = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client_id,
            PromoterAllocation.promoter_user_id == promoter_user_id,
        )
    )).scalar_one_or_none()
    return int(row.units_balance) if row is not None else 0


async def get_company_unallocated_balance(
    db: AsyncSession, client_id: str,
) -> int:
    """Return the units the CA can still spend on allocations.

    Formula:
        unallocated = total_purchased
                    − sum(promoter_allocations.units_balance)
                    − sum(promoter_allocations.consumed_total)

    Notes on this formula:
      • Self-subscribe is intentionally excluded (per Phase C clarification
        2026-05-04: company subscriptions are *only* for promoter
        allocation; self-subs do not touch the company pool).
      • SubscriptionPool.units_consumed (legacy) is intentionally NOT
        used in the formula — going forward, every consumption flows
        through promoter_allocations.consumed_total. Pre-Phase-C
        legacy consumption rows on SubscriptionPool stay as historical
        record only.
    """
    total_purchased = (await db.execute(
        select(func.coalesce(func.sum(SubscriptionPool.units_purchased), 0))
        .where(SubscriptionPool.client_id == client_id)
    )).scalar() or 0

    promoter_balance_total = (await db.execute(
        select(func.coalesce(func.sum(PromoterAllocation.units_balance), 0))
        .where(PromoterAllocation.client_id == client_id)
    )).scalar() or 0

    promoter_consumed_total = (await db.execute(
        select(func.coalesce(func.sum(PromoterAllocation.consumed_total), 0))
        .where(PromoterAllocation.client_id == client_id)
    )).scalar() or 0

    return int(total_purchased) - int(promoter_balance_total) - int(promoter_consumed_total)


# ── Mutations ───────────────────────────────────────────────────────────────

async def allocate_to_promoter(
    db: AsyncSession, *, client_id: str, promoter_user_id: str, units: int,
) -> PromoterAllocation:
    """CA action — move `units` from company unallocated to promoter row.

    Lazy-creates the promoter's row on first allocation. Raises
    ValueError if `units` is non-positive or exceeds the company's
    unallocated balance.
    """
    if units <= 0:
        raise ValueError("units must be positive")

    unallocated = await get_company_unallocated_balance(db, client_id)
    if units > unallocated:
        raise ValueError(
            f"insufficient company unallocated balance "
            f"({unallocated} available, {units} requested)"
        )

    row = (await db.execute(
        select(PromoterAllocation)
        .where(
            PromoterAllocation.client_id == client_id,
            PromoterAllocation.promoter_user_id == promoter_user_id,
        )
        .with_for_update()
    )).scalar_one_or_none()

    if row is None:
        row = PromoterAllocation(
            client_id=client_id,
            promoter_user_id=promoter_user_id,
            units_balance=units,
            allocated_total=units,
            reclaimed_total=0,
            consumed_total=0,
        )
        db.add(row)
    else:
        row.units_balance += units
        row.allocated_total += units

    await db.flush()
    return row


async def reclaim_from_promoter(
    db: AsyncSession, *, client_id: str, promoter_user_id: str, units: int,
) -> PromoterAllocation:
    """CA action — pull `units` back from a promoter to the company pool.

    Cannot exceed the promoter's current balance (already-consumed
    units are not reclaimable). Raises ValueError on bad input or
    missing row.
    """
    if units <= 0:
        raise ValueError("units must be positive")

    row = (await db.execute(
        select(PromoterAllocation)
        .where(
            PromoterAllocation.client_id == client_id,
            PromoterAllocation.promoter_user_id == promoter_user_id,
        )
        .with_for_update()
    )).scalar_one_or_none()

    if row is None:
        raise ValueError("no allocation exists for this promoter")
    if units > row.units_balance:
        raise ValueError(
            f"cannot reclaim more than the promoter's current balance "
            f"({row.units_balance} available, {units} requested)"
        )

    row.units_balance -= units
    row.reclaimed_total += units
    await db.flush()
    return row


async def consume_for_assignment(
    db: AsyncSession, *, client_id: str, promoter_user_id: str,
) -> PromoterAllocation:
    """Promoter action — draw down 1 unit when an assignment is created.

    Raises ValueError if the promoter has no balance (the route should
    have already short-circuited via `get_promoter_balance` before
    reaching this point, but the guard here makes the invariant
    explicit and prevents silent over-consumption).
    """
    row = (await db.execute(
        select(PromoterAllocation)
        .where(
            PromoterAllocation.client_id == client_id,
            PromoterAllocation.promoter_user_id == promoter_user_id,
        )
        .with_for_update()
    )).scalar_one_or_none()

    if row is None or row.units_balance <= 0:
        raise ValueError(
            "promoter has no allocated units — assignment cannot proceed"
        )

    row.units_balance -= 1
    row.consumed_total += 1
    await db.flush()
    return row
