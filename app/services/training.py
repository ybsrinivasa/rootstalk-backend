"""Training Sandbox helpers — read-path indirection + invariants.

Training children (Client.is_training=True) don't own Packages,
PromoterAllocations, or Subscriptions of their own — those all
live on the real parent. Discovery / pre-subscribe reads that
would ordinarily filter `Package.client_id == <cid>` need to
resolve <cid> through the training→parent link first.

Post-subscription reads don't need this: once a farmer holds a
training Subscription, subsequent lookups go via
`Package.id == sub.package_id` which fetches the specific
package row directly (the row lives under the parent, but the
FK resolves cleanly regardless).

Helpers here are the single source of truth for these
indirections so a future flag change or lifecycle tweak lands in
one place instead of scattered `is_training` checks.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import Client


async def resolve_package_client_id(
    db: AsyncSession, client_id: str,
) -> str:
    """Return the client_id that owns Packages for the given caller-
    supplied client_id.

    For a real client, this is a passthrough (returns the input).
    For a training child, this returns `parent_client_id` — the
    real parent owns the Packages the training session practises
    against, and copying them into the child would double the
    author's work and defeat the "practice on real content" point.

    Cheap: one indexed point-read on `clients.id`. Callers should
    invoke this once per request and cache the result if they'll
    reference it multiple times.

    Returns the input unchanged if the client_id doesn't resolve
    to anything — defensive; upstream code will fail its own
    "client not found" gate before it needs a Package.
    """
    row = (await db.execute(
        select(Client.is_training, Client.parent_client_id)
        .where(Client.id == client_id)
    )).first()
    if row is None:
        return client_id
    is_training, parent_client_id = row
    if is_training and parent_client_id:
        return parent_client_id
    return client_id


async def is_training_client(
    db: AsyncSession, client_id: str,
) -> bool:
    """True iff the given client_id is a training child.

    Used by the Razorpay bypass and PromoterAllocation bypass
    (Commit D) — both of those check "should this transaction
    skip real money / real allocation?" before running.
    """
    row = (await db.execute(
        select(Client.is_training).where(Client.id == client_id)
    )).scalar_one_or_none()
    return bool(row)


async def training_parent_of(
    db: AsyncSession, client_id: str,
) -> Optional[str]:
    """Return `parent_client_id` if client_id is a training child,
    else None. Complement of `is_training_client` for callers that
    need both facts."""
    row = (await db.execute(
        select(Client.is_training, Client.parent_client_id)
        .where(Client.id == client_id)
    )).first()
    if row is None:
        return None
    is_training, parent_client_id = row
    return parent_client_id if is_training else None
