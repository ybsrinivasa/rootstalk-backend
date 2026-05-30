"""Per-promoter subscription-pool service.

Mediates the operations on a `promoter_allocations` row:

- `allocate_to_promoter`  — CA gives N units from the company's
  unallocated balance to a specific promoter's row.
- `reclaim_from_promoter` — CA takes N un-consumed units back from a
  promoter's row to the company's unallocated balance.
- `consume_for_assignment` — promoter draws down their own row by 1
  when they successfully assign a subscription to a farmer.
- `refund_to_promoter` — credit 1 unit back to the promoter's kitty
  when an assignment is terminated by farmer-reject / auto-expire /
  promoter self-cancel (F-P B2, 2026-05-29).
- `get_promoter_balance` / `get_company_unallocated_balance` —
  read-side accessors used by the new endpoints and by the
  pre-existing `_get_pool_balance` helper.

Invariants:
- `units_balance == allocated_total
                  - reclaimed_total
                  - consumed_total
                  + refunded_total`
- A promoter's row is created lazily (via `allocate_to_promoter` or
  the legacy backfill); reclaim/consume/refund against a missing row
  raise ValueError.
- All operations validate non-negativity of the post-state and raise
  ValueError if it would go negative.

Concurrency: SELECT ... FOR UPDATE locks the row for the duration of
each mutation so two simultaneous CA actions on the same promoter
don't drift the running totals.
"""
from __future__ import annotations

from datetime import date as _date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.subscriptions.models import EnterpriseLicense, SubscriptionPool
from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation


# EL module (2026-05-30) — sentinel returned in place of a numeric
# balance whenever an active Enterprise Licence covers the client.
# Big enough to render comfortably as "Unlimited" in chips and not
# get truncated, small enough to stay an Int (≤ Postgres INT4 max).
ENTERPRISE_UNLIMITED_BALANCE = 999_999


async def is_enterprise_licensed(
    db: AsyncSession, client_id: str, today: _date | None = None,
) -> bool:
    """True iff `client_id` has an ACTIVE Enterprise Licence whose
    [from_date, to_date] window covers today.

    Single source of truth used by every kitty / consume / allocate
    decision so the bypass is consistent across endpoints. Cheap —
    one indexed point read on (client_id, status).
    """
    today = today or _date.today()
    lic = (await db.execute(
        select(EnterpriseLicense).where(
            EnterpriseLicense.client_id == client_id,
            EnterpriseLicense.status == "ACTIVE",
            EnterpriseLicense.from_date <= today,
            EnterpriseLicense.to_date >= today,
        ).limit(1)
    )).scalar_one_or_none()
    return lic is not None


# ── Read accessors ──────────────────────────────────────────────────────────

async def get_promoter_balance(
    db: AsyncSession, client_id: str, promoter_user_id: str,
) -> int:
    """Return the promoter's currently-available units (0 if no row exists).

    EL bypass (2026-05-30): when the Client has an active Enterprise
    Licence, returns `ENTERPRISE_UNLIMITED_BALANCE`. The PWA renders
    this as "Unlimited" via the same chip; the assign flow's >0 gate
    naturally lets every initiate through.
    """
    if await is_enterprise_licensed(db, client_id):
        return ENTERPRISE_UNLIMITED_BALANCE
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

    EL bypass (2026-05-30): when the Client has an active Enterprise
    Licence, returns `ENTERPRISE_UNLIMITED_BALANCE` — the per-promoter
    allocation step is operationally suppressed for licensed clients,
    but the helper still answers cleanly so any UI that surfaces "what
    can I spend" reads as "Unlimited".

    Formula (regular path):
        unallocated = total_purchased
                    − sum(promoter_allocations.units_balance)
                    − sum(promoter_allocations.consumed_total)
                    + sum(promoter_allocations.refunded_total)

    Notes on this formula:
      • Self-subscribe is intentionally excluded (per Phase C clarification
        2026-05-04: company subscriptions are *only* for promoter
        allocation; self-subs do not touch the company pool).
      • SubscriptionPool.units_consumed (legacy) is intentionally NOT
        used in the formula — going forward, every consumption flows
        through promoter_allocations.consumed_total. Pre-Phase-C
        legacy consumption rows on SubscriptionPool stay as historical
        record only.
      • F-P B2 (2026-05-29): refunded_total cancels the part of
        consumed_total that was returned to the promoter's
        units_balance. Without this term, a refunded unit would be
        counted twice — once in units_balance (where it actually is)
        and once in consumed_total (where it stays as a historical
        record).
    """
    if await is_enterprise_licensed(db, client_id):
        return ENTERPRISE_UNLIMITED_BALANCE
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

    promoter_refunded_total = (await db.execute(
        select(func.coalesce(func.sum(PromoterAllocation.refunded_total), 0))
        .where(PromoterAllocation.client_id == client_id)
    )).scalar() or 0

    return (
        int(total_purchased)
        - int(promoter_balance_total)
        - int(promoter_consumed_total)
        + int(promoter_refunded_total)
    )


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
):
    """Promoter action — draw down 1 unit when an assignment is created.

    EL bypass (2026-05-30): when the Client has an active Enterprise
    Licence, this is a no-op. No PromoterAllocation row is touched;
    no balance check; returns None. The Subscription / Assignment
    rows still get created upstream — only the kitty side-effect is
    suppressed (per user spec "avoid assigning subscriptions to
    promoters for EL").

    Otherwise raises ValueError if the promoter has no balance (the
    route should have already short-circuited via
    `get_promoter_balance` before reaching this point, but the guard
    here makes the invariant explicit and prevents silent
    over-consumption).
    """
    if await is_enterprise_licensed(db, client_id):
        return None

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


async def refund_to_promoter(
    db: AsyncSession, *, client_id: str, promoter_user_id: str,
):
    """F-P B2 (2026-05-29) — credit 1 unit back to the promoter's kitty.

    Called when a PromoterAssignment terminates without producing an
    ACTIVE subscription: farmer rejects, 72h auto-expire, or F-P
    self-cancels. Increments `units_balance` and `refunded_total`;
    `consumed_total` is intentionally NOT decremented — it stays the
    historical "ever-consumed" running count. The balance invariant
    becomes:
        units_balance == allocated_total
                       - reclaimed_total
                       - consumed_total
                       + refunded_total

    EL bypass (2026-05-30): when the Client has an active Enterprise
    Licence, this is a no-op. No PromoterAllocation row is touched
    (none was decremented at consume time). Pre-existing rows from
    pre-EL allocations stay untouched. Same idempotency story as
    consume — the upstream transition guard prevents double-calls.

    Idempotency is the caller's responsibility: refund should only be
    invoked exactly once per terminating transition. The BL-11
    transition guard on the farmer-respond path already prevents
    re-flipping an already-terminal Assignment, but new callers
    (auto-expire sweep, self-cancel endpoint) must replicate that
    check before calling.
    """
    if await is_enterprise_licensed(db, client_id):
        return None

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

    row.units_balance += 1
    row.refunded_total += 1
    await db.flush()
    return row
