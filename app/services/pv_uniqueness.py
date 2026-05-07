"""CCA Step 2 / Batch 2D — Same-district P/V uniqueness.

Spec §4.2: "No district can have two PoPs with the same Parameter-
Variable combination — enforced by the system." Restated more
precisely: in any district that two PoPs both cover, their P/V
fingerprints must differ in at least one assignment.

The fingerprint of a Package = `{parameter_id: variable_id}` for
all its `PackageVariable` rows. Two PoPs "conflict" iff (a) their
location sets intersect on at least one (state, district) pair AND
(b) their fingerprints are equal as Python dicts. The both-empty
case (`{} == {}`) is a real conflict — it surfaces the spec §4.2
"first PoP can have empty P/V, but only until a second PoP exists
with shared coverage" rule the moment the second PoP is given a
location overlap.

INACTIVE siblings are excluded from the check by the async wrapper
— they don't actively serve farmers, and a legitimate "replace an
old superseded PoP with same fingerprint" workflow would otherwise
be blocked by its own predecessor.

Pure-function `find_pv_conflicts` operates on already-loaded data
so it can be unit-tested without a DB.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import (
    Package, PackageLocation, PackageStatus, PackageVariable,
)


@dataclass(frozen=True)
class PVConflict:
    """Describes a single sibling-with-conflicting-fingerprint pair."""
    sibling_package_id: str
    sibling_package_name: str
    shared_districts: tuple[tuple[str, str], ...]  # ((state, district), ...)


class PVConflictError(Exception):
    """Raised when one or more siblings share a district with this
    PoP AND have the identical P/V fingerprint. The route layer
    maps this to a 422 with `code = pv_conflict_with_sibling` and
    a body carrying the offending siblings + districts so the CA
    portal can surface a precise corrective message."""

    code = "pv_conflict_with_sibling"

    def __init__(self, conflicts: list[PVConflict]):
        self.conflicts = conflicts
        names = ", ".join(f"{c.sibling_package_name!r}" for c in conflicts)
        super().__init__(
            f"This Package has the same Parameters & Variables as "
            f"{names} in at least one shared district. Adjust at "
            "least one variable so guided elimination remains "
            "deterministic for the farmer."
        )


def find_pv_conflicts(
    *,
    this_fingerprint: dict[str, str],
    this_districts: set[tuple[str, str]],
    siblings: list[tuple[str, str, dict[str, str], set[tuple[str, str]]]],
) -> list[PVConflict]:
    """Pure: return a list of conflicts (one per sibling that
    matches the rule).

    `siblings` is a list of tuples
    `(sibling_id, sibling_name, sibling_fingerprint, sibling_districts)`.

    A sibling produces a `PVConflict` iff:
    - `this_districts & sibling_districts` is non-empty, AND
    - `this_fingerprint == sibling_fingerprint`.
    """
    conflicts: list[PVConflict] = []
    for sib_id, sib_name, sib_fp, sib_districts in siblings:
        shared = this_districts & sib_districts
        if not shared:
            continue
        if this_fingerprint == sib_fp:
            conflicts.append(PVConflict(
                sibling_package_id=sib_id,
                sibling_package_name=sib_name,
                shared_districts=tuple(sorted(shared)),
            ))
    return conflicts


async def assert_pv_unique_for_package(
    db: AsyncSession, *, package: Package,
) -> None:
    """Load this Package's fingerprint + districts plus those of all
    DRAFT/ACTIVE siblings under the same `(client, crop)`, then
    delegate to `find_pv_conflicts`. Raise `PVConflictError` if any
    conflict is found.

    No-op when the package has no districts assigned (no shared-
    district scenario possible) or no DRAFT/ACTIVE siblings exist.

    Caller must ensure the latest writes are flushed to the session
    so `select(...)` sees the about-to-be-committed state.
    """
    this_pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == package.id)
    )).scalars().all()
    this_fp = {pv.parameter_id: pv.variable_id for pv in this_pvs}

    this_locs = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == package.id)
    )).scalars().all()
    this_districts = {(l.state_cosh_id, l.district_cosh_id) for l in this_locs}
    if not this_districts:
        return

    sibling_pkgs = (await db.execute(
        select(Package).where(
            Package.client_id == package.client_id,
            Package.crop_cosh_id == package.crop_cosh_id,
            Package.id != package.id,
            Package.status.in_([PackageStatus.DRAFT, PackageStatus.ACTIVE]),
        )
    )).scalars().all()
    if not sibling_pkgs:
        return

    sibling_ids = [p.id for p in sibling_pkgs]

    pvs_by_pkg: dict[str, dict[str, str]] = defaultdict(dict)
    for pv in (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id.in_(sibling_ids))
    )).scalars().all():
        pvs_by_pkg[pv.package_id][pv.parameter_id] = pv.variable_id

    locs_by_pkg: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for l in (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id.in_(sibling_ids))
    )).scalars().all():
        locs_by_pkg[l.package_id].add((l.state_cosh_id, l.district_cosh_id))

    siblings = [
        (p.id, p.name, dict(pvs_by_pkg[p.id]), set(locs_by_pkg[p.id]))
        for p in sibling_pkgs
    ]

    conflicts = find_pv_conflicts(
        this_fingerprint=this_fp,
        this_districts=this_districts,
        siblings=siblings,
    )
    if conflicts:
        raise PVConflictError(conflicts)
