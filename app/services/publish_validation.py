"""CCA Step 2 / Batch 2C — Publish-time mandatory-fields gate.

Spec §4.1 + §4.2 together require, before a Package can publish:

- All mandatory fields populated: name, package_type, duration_days,
  start_date_label_cosh_id. (These are enforced at create time as
  of Batch 2A but defensive re-check costs nothing here.)
- At least one (state, district) Location.
- At least one Subject Expert listed as Author.
- §4.2 second-PoP rule: if any other DRAFT/ACTIVE PoP for the same
  (client, crop) shares at least one district with this PoP, BOTH
  must have non-empty P/V before either can publish.

The validator returns the **complete** list of missing items so the
CA portal can render a single consolidated checklist instead of
forcing the expert through fix-one-at-a-time roundtrips. The route
layer maps `PublishBlockedError` to a 422 with stable code
`publish_blocked_missing_fields` and `missing: [...]` body.

Defensive separation from Batches 2D / 2E: those guards primarily
fire at SAVE time on `set_package_variables` /
`set_package_locations`. They also run at publish as belt-and-
suspenders against direct-SQL bypass or concurrent-edit races, but
their failure modes (data corruption signals) are distinct from
2C's "fields not yet filled in" failure mode and stay as separate
422 responses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import (
    Package, PackageAuthor, PackageLocation, PackageStatus, PackageVariable,
)


@dataclass(frozen=True)
class MissingPublishField:
    """One missing requirement. `code` is the stable identifier the
    portal dispatches on; `message` is the human-readable text;
    `extra` carries optional structured data (sibling id/name,
    shared districts) for the §4.2 codes."""
    code: str
    message: str
    extra: Optional[dict] = None


class PublishBlockedError(Exception):
    """Raised when one or more publish requirements are unmet."""

    code = "publish_blocked_missing_fields"

    def __init__(self, missing: list[MissingPublishField]):
        self.missing = missing
        codes = ", ".join(m.code for m in missing)
        super().__init__(
            f"Cannot publish: {len(missing)} requirement(s) not met "
            f"({codes}). Fix all listed items and try again."
        )


def validate_publish_readiness(
    *,
    package: dict,
    location_count: int,
    author_count: int,
    has_pv: bool,
    siblings_with_shared_districts: list[dict],
) -> list[MissingPublishField]:
    """Pure: collect every reason the package can't publish today.
    Empty list = ready.

    `package` is a flat dict with the fields the validator inspects:
        name, package_type, duration_days, start_date_label_cosh_id

    `siblings_with_shared_districts` is a list of dicts with shape:
        {id, name, has_pv: bool, shared_districts: [{state_cosh_id, district_cosh_id}, ...]}
    Only DRAFT/ACTIVE siblings sharing at least one district should
    be in this list — INACTIVE siblings are filtered out by the
    async wrapper.
    """
    missing: list[MissingPublishField] = []

    if not package.get("name"):
        missing.append(MissingPublishField(
            "missing_name", "Package name is required.",
        ))
    if not package.get("package_type"):
        missing.append(MissingPublishField(
            "missing_package_type", "Package type is required.",
        ))
    if not package.get("duration_days"):
        missing.append(MissingPublishField(
            "missing_duration", "Package duration is required.",
        ))
    if not package.get("start_date_label_cosh_id"):
        missing.append(MissingPublishField(
            "missing_start_date_label", "Start Date Label is required.",
        ))

    if location_count == 0:
        missing.append(MissingPublishField(
            "no_locations",
            "At least one (state, district) location must be assigned.",
        ))
    if author_count == 0:
        missing.append(MissingPublishField(
            "no_authors",
            "At least one Subject Expert must be credited as an author.",
        ))

    # §4.2 second-PoP rule.
    if siblings_with_shared_districts:
        if not has_pv:
            missing.append(MissingPublishField(
                "no_pv_with_shared_district_sibling",
                "At least one parameter-variable assignment is required "
                "because another PoP shares a district with this one "
                "(spec §4.2).",
            ))
        for sib in siblings_with_shared_districts:
            if not sib.get("has_pv"):
                missing.append(MissingPublishField(
                    "sibling_has_no_pv",
                    f"PoP '{sib['name']}' shares a district with this one "
                    "but has no parameter-variable assignments. Neither "
                    "PoP can publish until both do (spec §4.2).",
                    extra={
                        "sibling_package_id": sib["id"],
                        "sibling_package_name": sib["name"],
                        "shared_districts": sib["shared_districts"],
                    },
                ))

    return missing


async def assert_package_publish_ready(
    db: AsyncSession, *, package: Package,
) -> None:
    """Async wrapper. Loads counts + sibling data, delegates to the
    pure validator, raises `PublishBlockedError` if any requirement
    is unmet."""
    location_count = (await db.execute(
        select(func.count()).select_from(PackageLocation).where(
            PackageLocation.package_id == package.id,
        )
    )).scalar() or 0

    author_count = (await db.execute(
        select(func.count()).select_from(PackageAuthor).where(
            PackageAuthor.package_id == package.id,
        )
    )).scalar() or 0

    pv_count = (await db.execute(
        select(func.count()).select_from(PackageVariable).where(
            PackageVariable.package_id == package.id,
        )
    )).scalar() or 0
    has_pv = pv_count > 0

    this_districts = set((await db.execute(
        select(
            PackageLocation.state_cosh_id, PackageLocation.district_cosh_id,
        ).where(PackageLocation.package_id == package.id)
    )).all())

    siblings_with_shared: list[dict] = []
    if this_districts:
        sibling_pkgs = (await db.execute(
            select(Package).where(
                Package.client_id == package.client_id,
                Package.crop_cosh_id == package.crop_cosh_id,
                Package.id != package.id,
                Package.status.in_([PackageStatus.DRAFT, PackageStatus.ACTIVE]),
            )
        )).scalars().all()
        for sib in sibling_pkgs:
            sib_districts = set((await db.execute(
                select(
                    PackageLocation.state_cosh_id,
                    PackageLocation.district_cosh_id,
                ).where(PackageLocation.package_id == sib.id)
            )).all())
            shared = this_districts & sib_districts
            if not shared:
                continue
            sib_pv_count = (await db.execute(
                select(func.count()).select_from(PackageVariable).where(
                    PackageVariable.package_id == sib.id,
                )
            )).scalar() or 0
            siblings_with_shared.append({
                "id": sib.id,
                "name": sib.name,
                "has_pv": sib_pv_count > 0,
                "shared_districts": [
                    {"state_cosh_id": s, "district_cosh_id": d}
                    for s, d in sorted(shared)
                ],
            })

    pkg_type_value = (
        package.package_type.value if package.package_type is not None
        and hasattr(package.package_type, "value")
        else package.package_type
    )
    missing = validate_publish_readiness(
        package={
            "name": package.name,
            "package_type": pkg_type_value,
            "duration_days": package.duration_days,
            "start_date_label_cosh_id": package.start_date_label_cosh_id,
        },
        location_count=location_count,
        author_count=author_count,
        has_pv=has_pv,
        siblings_with_shared_districts=siblings_with_shared,
    )
    if missing:
        raise PublishBlockedError(missing)
