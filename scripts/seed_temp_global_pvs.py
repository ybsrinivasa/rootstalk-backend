"""One-shot temporary seeder for Global Parameters/Variables.

User seeded these example PVs on 2026-05-11 so the SA portal has
something to demo before Cosh ships the real parameter_for_crop
Connect. Once Cosh data lands, run `--purge` first to drop these
synthetic rows, then re-run the live sync.

Fixture data (per crop):

  SOIL TYPE       Red soil, Black soil, Alluvial soil, Lateritic soil
  IRRIGATION TYPE Flood, Channel, Drip, Sprinkler
  CROP DURATION   Short, Medium, Long
  GROWING SEASON  Kharif, Rabi, Summer

Idempotent: re-running creates nothing new. Parameters are marked
`source=COSH` (matches the eventual sync's expectation) and
`client_id IS NULL` so they live at Global scope.

Usage:
    python scripts/seed_temp_global_pvs.py            # seed
    python scripts/seed_temp_global_pvs.py --dry-run  # report only
    python scripts/seed_temp_global_pvs.py --purge    # delete the fixture rows
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select

from app.database import AsyncSessionLocal
from app.modules.advisory.models import (
    PackageVariable, Parameter, ParameterSource, Variable,
)
from app.services.cosh_crop_view import list_crops


FIXTURE: dict[str, list[str]] = {
    "SOIL TYPE":       ["Red soil", "Black soil", "Alluvial soil", "Lateritic soil"],
    "IRRIGATION TYPE": ["Flood", "Channel", "Drip", "Sprinkler"],
    "CROP DURATION":   ["Short", "Medium", "Long"],
    "GROWING SEASON":  ["Kharif", "Rabi", "Summer"],
}


async def main(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as db:
        crops = await list_crops(db)
        if not crops:
            print("No crops in the system yet — sync Cosh biological_names first.")
            return
        print(f"Found {len(crops)} crops.")

        if args.purge:
            # FK constraints don't cascade, so we delete children
            # (PackageVariable → Variable) before the Parameter.
            param_ids: list[str] = []
            for name in FIXTURE.keys():
                ids = [p.id for p in (await db.execute(
                    select(Parameter).where(
                        Parameter.name == name,
                        Parameter.client_id == None,  # noqa: E711
                        Parameter.source == ParameterSource.COSH,
                    )
                )).scalars().all()]
                print(f"  '{name}': {len(ids)} fixture parameter row(s)")
                param_ids.extend(ids)
            if not param_ids:
                print("Nothing to purge.")
                return
            if args.dry_run:
                print(f"[dry] would delete {len(param_ids)} Parameters + their Variables + any PackageVariable refs")
                return
            await db.execute(delete(PackageVariable).where(
                PackageVariable.parameter_id.in_(param_ids),
            ))
            await db.execute(delete(Variable).where(
                Variable.parameter_id.in_(param_ids),
            ))
            res = await db.execute(delete(Parameter).where(
                Parameter.id.in_(param_ids),
            ))
            await db.commit()
            print(f"Purge complete — removed {res.rowcount} Parameters.")
            return

        created_params = 0
        created_vars = 0
        for crop in crops:
            crop_cosh_id = crop.get("cosh_id")
            crop_name = crop.get("name_en") or crop_cosh_id
            for display_order, (pname, vnames) in enumerate(FIXTURE.items()):
                param = (await db.execute(
                    select(Parameter).where(
                        Parameter.crop_cosh_id == crop_cosh_id,
                        Parameter.client_id == None,  # noqa: E711
                        Parameter.name == pname,
                    )
                )).scalar_one_or_none()
                if param is None:
                    if args.dry_run:
                        print(f"  [dry] {crop_name}: + {pname} + {len(vnames)} variables")
                        continue
                    param = Parameter(
                        crop_cosh_id=crop_cosh_id,
                        client_id=None,
                        name=pname,
                        source=ParameterSource.COSH,
                        display_order=display_order,
                    )
                    db.add(param)
                    await db.flush()
                    created_params += 1
                # Variables
                existing_vars = {
                    v.name for v in (await db.execute(
                        select(Variable).where(Variable.parameter_id == param.id)
                    )).scalars().all()
                }
                for vname in vnames:
                    if vname in existing_vars:
                        continue
                    if args.dry_run:
                        continue
                    db.add(Variable(parameter_id=param.id, name=vname))
                    created_vars += 1
            if args.dry_run is False:
                await db.commit()

        verb = "would create" if args.dry_run else "created"
        print(f"\n{verb} {created_params} parameters, {created_vars} variables.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
