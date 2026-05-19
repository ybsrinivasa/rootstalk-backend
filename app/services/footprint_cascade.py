"""Batch FF (2026-05-19) — footprint→package cascade.

When the CA narrows the company location footprint, the impact on
existing packages must be explicit, not silent. This module:

  1. Diffs the proposed new footprint against the current ACTIVE
     ClientLocations and computes the removed (state, district) pairs.
  2. Finds every package that references at least one removed pair.
  3. Buckets affected packages into "will shrink" (other locations
     remain) vs "will inactivate" (this was the last location).
  4. Without `force=True`, raises `FootprintCascadeConfirmationRequired`
     so the caller can show the CA an impact summary and a Confirm
     button. With `force=True`, executes the cascade: hard-delete
     matching `package_locations` rows, flip empty packages to
     INACTIVE with stamped audit fields.

Add-to-footprint is intentionally NOT cascaded — adding districts to
the footprint cannot affect existing packages (it only widens future
picker options).

Removed locations do NOT auto-restore on footprint re-add (user
decision 2026-05-19, asymmetric with crop revival): if the CA
re-adds a district later, existing packages still don't reference
it. The SE must consciously add it back via Edit Locations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import (
    Package, PackageLocation, PackageStatus,
)
from app.modules.clients.models import ClientLocation


CASCADE_REASON_LOCATIONS = "locations_cleared_by_cascade"


@dataclass
class _AffectedPackage:
    id: str
    name: str
    status_before: str
    removed_locations: list[tuple[str, str]] = field(default_factory=list)
    remaining_after: int = 0

    @property
    def will_inactivate(self) -> bool:
        return self.remaining_after == 0


@dataclass
class FootprintCascadeImpact:
    """Structured summary of what the cascade WILL do (or DID do, if
    force=True). The router serialises this into the 422 confirmation
    payload."""
    removed_pairs: list[tuple[str, str]]
    shrunk: list[_AffectedPackage]
    inactivated: list[_AffectedPackage]

    @property
    def any_impact(self) -> bool:
        return bool(self.shrunk or self.inactivated)

    def to_dict(self) -> dict:
        def _pkg(a: _AffectedPackage) -> dict:
            return {
                "package_id": a.id,
                "package_name": a.name,
                "status_before": a.status_before,
                "removed_locations": [
                    {"state_cosh_id": s, "district_cosh_id": d}
                    for s, d in a.removed_locations
                ],
                "remaining_locations": a.remaining_after,
            }
        return {
            "removed_pairs": [
                {"state_cosh_id": s, "district_cosh_id": d}
                for s, d in self.removed_pairs
            ],
            "will_shrink": [_pkg(a) for a in self.shrunk],
            "will_inactivate": [_pkg(a) for a in self.inactivated],
        }


class FootprintCascadeConfirmationRequired(Exception):
    """Raised by `diff_footprint_and_cascade` when force=False and
    the diff would affect at least one package. Router maps this to
    422 `footprint_cascade_confirmation_required` with the impact
    payload."""

    def __init__(self, impact: FootprintCascadeImpact):
        self.impact = impact
        super().__init__("Footprint cascade requires explicit confirmation.")


async def diff_footprint_and_cascade(
    db: AsyncSession, *, client_id: str,
    new_pairs: set[tuple[str, str]],
    force: bool,
) -> FootprintCascadeImpact:
    """Core cascade entry point.

    `new_pairs` is the desired post-edit footprint. The current
    footprint is read from `client_locations` filtered to ACTIVE.

    Returns an impact summary describing what will happen (or did,
    if force=True). Raises `FootprintCascadeConfirmationRequired`
    when force=False and any package would be affected.
    """
    current_pairs = {
        (s, d) for s, d in (await db.execute(
            select(ClientLocation.state_cosh_id, ClientLocation.district_cosh_id)
            .where(
                ClientLocation.client_id == client_id,
                ClientLocation.status == "ACTIVE",
            )
        )).all()
    }
    removed = current_pairs - new_pairs
    if not removed:
        return FootprintCascadeImpact(
            removed_pairs=[], shrunk=[], inactivated=[],
        )

    # Find every package_location row that matches a removed pair.
    candidate_pkg_ids: set[str] = set()
    pkg_to_removed: dict[str, list[tuple[str, str]]] = {}
    for state_id, dist_id in removed:
        pl_rows = (await db.execute(
            select(PackageLocation.package_id).join(
                Package, Package.id == PackageLocation.package_id,
            ).where(
                Package.client_id == client_id,
                PackageLocation.state_cosh_id == state_id,
                PackageLocation.district_cosh_id == dist_id,
            )
        )).scalars().all()
        for pid in pl_rows:
            candidate_pkg_ids.add(pid)
            pkg_to_removed.setdefault(pid, []).append((state_id, dist_id))

    if not candidate_pkg_ids:
        return FootprintCascadeImpact(
            removed_pairs=sorted(removed), shrunk=[], inactivated=[],
        )

    pkgs = (await db.execute(
        select(Package).where(Package.id.in_(candidate_pkg_ids))
    )).scalars().all()

    affected: list[_AffectedPackage] = []
    for pkg in pkgs:
        total_now = (await db.execute(
            select(PackageLocation).where(PackageLocation.package_id == pkg.id)
        )).scalars().all()
        removed_for_pkg = pkg_to_removed.get(pkg.id, [])
        remaining = len(total_now) - len(removed_for_pkg)
        affected.append(_AffectedPackage(
            id=pkg.id,
            name=pkg.name,
            status_before=(
                pkg.status.value if hasattr(pkg.status, "value") else str(pkg.status)
            ),
            removed_locations=sorted(removed_for_pkg),
            remaining_after=max(remaining, 0),
        ))

    shrunk = [a for a in affected if not a.will_inactivate]
    inactivated = [a for a in affected if a.will_inactivate]
    impact = FootprintCascadeImpact(
        removed_pairs=sorted(removed),
        shrunk=shrunk,
        inactivated=inactivated,
    )

    if not force:
        raise FootprintCascadeConfirmationRequired(impact)

    # Execute: hard-delete matching PackageLocation rows, flip empty
    # packages to INACTIVE, stamp audit fields on every affected
    # package (whether shrunk or inactivated).
    now = datetime.now(timezone.utc)
    pkg_by_id_typed = {p.id: p for p in pkgs}
    for state_id, dist_id in removed:
        await db.execute(
            PackageLocation.__table__.delete().where(
                PackageLocation.state_cosh_id == state_id,
                PackageLocation.district_cosh_id == dist_id,
                PackageLocation.package_id.in_(candidate_pkg_ids),
            )
        )
    await db.flush()
    for a in affected:
        pkg = pkg_by_id_typed[a.id]
        pkg.last_cascade_at = now
        if a.will_inactivate and pkg.status == PackageStatus.ACTIVE:
            pkg.status = PackageStatus.INACTIVE
            pkg.cascade_inactivated_at = now
            pkg.cascade_inactivated_reason = CASCADE_REASON_LOCATIONS
    await db.flush()
    return impact
