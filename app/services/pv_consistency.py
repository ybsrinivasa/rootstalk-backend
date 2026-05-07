"""CCA Step 2 / Batch 2E — Parameter-set consistency within a district.

Spec §4.2: "Parameters must be consistent within a district but can
vary across districts." Restated: every PoP that covers a district
must answer the SAME set of `parameter_id`s in its P/V fingerprint.
Otherwise the farmer's question sequence in that district is
ambiguous — at some node BL-01 would either ask a question that
doesn't apply to all remaining PoPs or fail to discriminate.

`display_order` lives on the `Parameter` row (per crop+client), not
on `PackageVariable`. Two PoPs sharing a parameter therefore share
its order automatically — so consistency reduces to set-equality on
`parameter_id`s.

This complements Batch 2D (`pv_uniqueness`) — together they cover
spec §4.2 in shared districts. The two are mutually exclusive per
pair of siblings:

| Sibling state                          | 2D fires? | 2E fires? |
| both fingerprints `{}`                 |    yes    |    no     |
| same set + same values                 |    yes    |    no     |
| same set + different values            |    no     |    no     |  (legitimate)
| different parameter sets (any values)  |    no     |    yes    |
| one empty, other non-empty             |    no     |    yes    |
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
class PVConsistencyViolation:
    sibling_package_id: str
    sibling_package_name: str
    shared_districts: tuple[tuple[str, str], ...]
    this_parameter_ids: tuple[str, ...]
    sibling_parameter_ids: tuple[str, ...]


class PVConsistencyError(Exception):
    """Raised when this PoP's parameter set differs from any
    DRAFT/ACTIVE sibling's set in any district they both cover."""

    code = "pv_parameter_set_mismatch"

    def __init__(self, violations: list[PVConsistencyViolation]):
        self.violations = violations
        names = ", ".join(f"{v.sibling_package_name!r}" for v in violations)
        super().__init__(
            f"This Package's parameter set differs from {names} in at "
            "least one shared district. Spec §4.2 requires every PoP "
            "covering a district to use the same parameters so the "
            "farmer's question sequence stays deterministic."
        )


def find_consistency_violations(
    *,
    this_parameter_ids: frozenset[str],
    this_districts: set[tuple[str, str]],
    siblings: list[tuple[str, str, frozenset[str], set[tuple[str, str]]]],
) -> list[PVConsistencyViolation]:
    """Pure: return a violation per sibling whose parameter set
    differs AND that shares at least one district with this PoP.

    `siblings` is a list of tuples
    `(sibling_id, sibling_name, sibling_parameter_ids, sibling_districts)`.
    """
    violations: list[PVConsistencyViolation] = []
    for sib_id, sib_name, sib_param_ids, sib_districts in siblings:
        shared = this_districts & sib_districts
        if not shared:
            continue
        if this_parameter_ids != sib_param_ids:
            violations.append(PVConsistencyViolation(
                sibling_package_id=sib_id,
                sibling_package_name=sib_name,
                shared_districts=tuple(sorted(shared)),
                this_parameter_ids=tuple(sorted(this_parameter_ids)),
                sibling_parameter_ids=tuple(sorted(sib_param_ids)),
            ))
    return violations


async def assert_pv_consistency_for_package(
    db: AsyncSession, *, package: Package,
) -> None:
    """Load this Package's parameter set + districts plus those of
    all DRAFT/ACTIVE siblings under the same `(client, crop)`, then
    delegate to `find_consistency_violations`. Raise
    `PVConsistencyError` if any violation is found.

    No-op when the package has no districts (no shared-district
    scenario possible) or no DRAFT/ACTIVE siblings exist.

    Caller must have flushed the latest writes so `select(...)`
    sees the about-to-be-committed state.
    """
    this_pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == package.id)
    )).scalars().all()
    this_param_ids = frozenset(pv.parameter_id for pv in this_pvs)

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

    pvs_by_pkg: dict[str, set[str]] = defaultdict(set)
    for pv in (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id.in_(sibling_ids))
    )).scalars().all():
        pvs_by_pkg[pv.package_id].add(pv.parameter_id)

    locs_by_pkg: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for l in (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id.in_(sibling_ids))
    )).scalars().all():
        locs_by_pkg[l.package_id].add((l.state_cosh_id, l.district_cosh_id))

    siblings = [
        (p.id, p.name, frozenset(pvs_by_pkg[p.id]), set(locs_by_pkg[p.id]))
        for p in sibling_pkgs
    ]

    violations = find_consistency_violations(
        this_parameter_ids=this_param_ids,
        this_districts=this_districts,
        siblings=siblings,
    )
    if violations:
        raise PVConsistencyError(violations)
