"""One-shot reactivator for Packages that were INACTIVATED by the
publish over-demote bug fixed in 2e82c58 (2026-05-20).

The bug: publishing any Package "Tomato-Flood" under (client, crop)
demoted every other ACTIVE Package for the same (client, crop) —
including sibling PoPs with different names like "Tomato-Drip",
"Tomato-Greenhouse", etc. Subscriptions were migrated onto the
freshly-published row.

A safe reactivation:

  • The Package is currently INACTIVE.
  • It has NO cascade stamps (cascade_inactivated_at is NULL) —
    rules out the crop/location-cascade INACTIVE flavour, which
    must NOT be auto-revived (the SE must consciously act on
    those, see Batch FF/II).
  • It was actually published once (published_at IS NOT NULL).
    Pure DRAFTs that never went live aren't reactivation
    candidates.
  • No other ACTIVE Package exists with the same (client, crop,
    name) — if one does, this row is a legitimate older version
    in the same lineage; leaving it INACTIVE is correct.
  • Reactivating won't trip §4.2 PV-uniqueness against any ACTIVE
    sibling under the same (client, crop) that now shares a
    district + identical fingerprint.

Run modes:

  python scripts/reactivate_overdemoted_packages.py
      # Dry run across every client. Prints what would change.

  python scripts/reactivate_overdemoted_packages.py --apply
      # Actually flips status back to ACTIVE.

  python scripts/reactivate_overdemoted_packages.py --client kingcorp
      # Restrict to one client by short_name.

Subscriptions are NOT migrated back. If a farmer was on a PoP
that got over-demoted, the over-demote bug already migrated
them onto whichever PoP was published last. Reverting that
migration would be guessing at the farmer's intent; better to
leave subscriptions where they are. If the wrong-target migration
is itself a problem in your data, that's a separate audit and
the script will print enough detail for you to spot it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Allow `python scripts/...` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()


async def main(apply: bool, client_short: str | None) -> None:
    # Touch every model module so the SQLAlchemy registry is fully
    # populated — without this, lazy cross-module relationships
    # (e.g. Timeline ↔ StandardResponse) fail to resolve.
    import app.main  # noqa: F401  (side-effect: imports all routers + models)
    from app.modules.advisory.models import Package, PackageStatus
    from app.modules.clients.models import Client
    from app.services.pv_uniqueness import (
        assert_pv_unique_for_package, PVConflictError,
    )

    eng = create_async_engine(os.environ["DATABASE_URL"])
    Session = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        # Narrow by client if asked.
        client_filter = []
        if client_short:
            c = (await db.execute(
                select(Client).where(Client.short_name == client_short.lower())
            )).scalar_one_or_none()
            if c is None:
                print(f"No client with short_name={client_short!r}; aborting.")
                return
            client_filter.append(Package.client_id == c.id)
            print(f"Restricted to client {c.display_name} ({c.short_name})")
        else:
            print("Scanning all clients.")

        candidates = (await db.execute(
            select(Package).where(
                Package.client_id.is_not(None),
                Package.status == PackageStatus.INACTIVE,
                Package.cascade_inactivated_at.is_(None),
                Package.published_at.is_not(None),
                *client_filter,
            ).order_by(Package.client_id, Package.crop_cosh_id, Package.name)
        )).scalars().all()

        print(f"\n{len(candidates)} INACTIVE candidate(s) for reactivation review.\n")

        reactivated = 0
        skipped_lineage = 0
        skipped_pv = 0
        skipped_other = 0

        # Cache client display names for the report.
        client_names: dict[str, str] = {}

        for pkg in candidates:
            if pkg.client_id not in client_names:
                c = await db.get(Client, pkg.client_id)
                client_names[pkg.client_id] = (c.display_name if c else pkg.client_id)
            cname = client_names[pkg.client_id]

            # 1. Lineage check — is there already an ACTIVE row with
            #    the same (client, crop, name)?
            active_sibling = (await db.execute(
                select(Package.id).where(
                    Package.client_id == pkg.client_id,
                    Package.crop_cosh_id == pkg.crop_cosh_id,
                    Package.name == pkg.name,
                    Package.status == PackageStatus.ACTIVE,
                    Package.id != pkg.id,
                ).limit(1)
            )).scalar_one_or_none()
            if active_sibling is not None:
                skipped_lineage += 1
                print(f"  SKIP (lineage) [{cname}] {pkg.crop_cosh_id} · {pkg.name!r} "
                      f"— newer version already ACTIVE")
                continue

            # 2. §4.2 PV-uniqueness check. Temporarily flip status
            #    to ACTIVE in-memory, run the assertion, then snap
            #    back if it fires. (We commit only at the end.)
            pkg.status = PackageStatus.ACTIVE
            try:
                await assert_pv_unique_for_package(db, package=pkg)
            except PVConflictError as e:
                pkg.status = PackageStatus.INACTIVE
                skipped_pv += 1
                shared = e.conflicts[0]["shared_districts"] if e.conflicts else []
                print(f"  SKIP (§4.2)    [{cname}] {pkg.crop_cosh_id} · {pkg.name!r} "
                      f"— shares {len(shared)} district(s) + fingerprint with an ACTIVE sibling")
                continue
            except Exception as e:
                pkg.status = PackageStatus.INACTIVE
                skipped_other += 1
                print(f"  SKIP (error)   [{cname}] {pkg.crop_cosh_id} · {pkg.name!r} "
                      f"— {type(e).__name__}: {str(e)[:100]}")
                continue

            reactivated += 1
            print(f"  OK   reactivate  [{cname}] {pkg.crop_cosh_id} · {pkg.name!r}")
            # status is already ACTIVE in-memory; keep it.

        print(f"\nSummary: {reactivated} would be reactivated, "
              f"{skipped_lineage} skipped (lineage), {skipped_pv} skipped (§4.2), "
              f"{skipped_other} skipped (other).")

        if apply and reactivated > 0:
            await db.commit()
            print("\nApplied — changes committed.")
        elif apply:
            print("\nNothing to commit.")
        else:
            print("\nDry run — no changes committed. Re-run with --apply to write.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Commit the reactivations. Default is dry-run.")
    p.add_argument("--client", default=None,
                   help="Restrict to one client by short_name.")
    args = p.parse_args()
    asyncio.run(main(apply=args.apply, client_short=args.client))
