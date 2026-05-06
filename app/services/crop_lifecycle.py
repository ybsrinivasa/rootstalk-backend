"""CCA Step 1 — crop lifecycle cascade.

When the CA removes a crop from the company's target list, every
ACTIVE Package under that (client, crop) is flipped to INACTIVE and
stamped with `cascade_inactivated_at`. Existing farmer subscriptions
are untouched — they continue unabated. New subscriptions are blocked
because the Package is INACTIVE.

When the CA later re-adds the same crop, every Package that carries a
`cascade_inactivated_at` stamp is revived back to ACTIVE and the stamp
is cleared. Packages that were INACTIVE for *other* reasons (e.g.
superseded by a newer published version) keep their state.

DRAFT packages are intentionally left alone in both directions:
they're not subscribable, so there's nothing to protect on removal,
and they should not be silently flipped to ACTIVE on re-add.

The cascade and restore primitives are pure functions over
already-loaded ORM instances. `assert_crop_on_belt` (Batch 1C) is
the membership guard called at PoP create + publish time so an
expert can't build / publish for a crop the CA hasn't placed on
the belt.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Package, PackageStatus
from app.modules.clients.models import ClientCrop


class CropNotOnBeltError(Exception):
    """Raised when a PoP create/publish references a crop that is not
    currently on the company's CCA conveyor belt — either no
    ClientCrop row exists for (client, crop), or one exists but is
    soft-removed (`removed_at IS NOT NULL`). The router maps this to
    a 422 with a stable error code."""

    code = "crop_not_on_belt"

    def __init__(self, client_id: str, crop_cosh_id: str):
        self.client_id = client_id
        self.crop_cosh_id = crop_cosh_id
        super().__init__(
            f"Crop {crop_cosh_id!r} is not on the company's CCA list. "
            "The CA must add it from the Resources basket before an "
            "expert can build a Package of Practices for it."
        )


def derive_active_crop_set(packages: Iterable[Package]) -> set[str]:
    """Return the set of `crop_cosh_id`s that have at least one
    ACTIVE Package in the input collection.

    Spec rule (CCA Step 1, 2026-05-06): a crop's active/inactive
    state is *derived* from its Packages. A crop is ACTIVE iff at
    least one PoP under it is ACTIVE. DRAFT and INACTIVE PoPs do
    not contribute. A crop with zero PoPs surfaces as inactive —
    it's on the conveyor belt but no live advisory has been built
    for it yet.
    """
    return {p.crop_cosh_id for p in packages if p.status == PackageStatus.ACTIVE}


async def assert_crop_on_belt(
    db: AsyncSession, *, client_id: str, crop_cosh_id: str,
) -> None:
    """Raise `CropNotOnBeltError` if the (client, crop) is not on
    the conveyor belt. Used by `create_package` and `publish_package`
    to enforce CCA Step 1 membership.

    A row that exists with `removed_at IS NOT NULL` counts as
    off-the-belt — the CA has soft-removed the crop and re-add is
    the only legitimate path back."""
    row = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client_id,
            ClientCrop.crop_cosh_id == crop_cosh_id,
            ClientCrop.removed_at.is_(None),
        )
    )).scalar_one_or_none()
    if row is None:
        raise CropNotOnBeltError(client_id=client_id, crop_cosh_id=crop_cosh_id)


def cascade_inactivate_packages_for_crop(
    packages: Iterable[Package], now: datetime,
) -> list[Package]:
    """Flip ACTIVE packages to INACTIVE with `cascade_inactivated_at = now`.

    Returns the list of packages that were modified. Already-INACTIVE
    and DRAFT packages are skipped — we don't claim ownership of an
    inactivation we didn't cause.
    """
    changed: list[Package] = []
    for pkg in packages:
        if pkg.status == PackageStatus.ACTIVE:
            pkg.status = PackageStatus.INACTIVE
            pkg.cascade_inactivated_at = now
            changed.append(pkg)
    return changed


def restore_cascade_inactivated_packages(
    packages: Iterable[Package],
) -> list[Package]:
    """Revive packages stamped with `cascade_inactivated_at`.

    For each such package: status → ACTIVE, stamp cleared. Packages
    without the stamp are left alone — they were INACTIVE for an
    unrelated reason (e.g. superseded by a newer published version)
    and the CA re-adding the crop must not silently republish them.
    """
    changed: list[Package] = []
    for pkg in packages:
        if pkg.cascade_inactivated_at is not None:
            pkg.status = PackageStatus.ACTIVE
            pkg.cascade_inactivated_at = None
            changed.append(pkg)
    return changed
