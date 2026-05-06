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

These are pure functions over already-loaded ORM instances. The router
fetches the rows, calls the service, commits.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from app.modules.advisory.models import Package, PackageStatus


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
