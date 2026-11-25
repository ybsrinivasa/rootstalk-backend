"""One-shot backfill — snapshot the current AOM lock state for SMS Grow farmers.

AOM content-versioning policy (shipped 2026-11-25) depends on snapshots
being present for farmers in the two committed states (in-window OR
purchased-ack) BEFORE the first CM/CA edit lands. Without a pre-existing
snapshot the regular edit path would silently apply to all SMS Grow farmers
— defeating the point of the whole policy.

Scoped to SMS Grow only per user direction (2026-11-25):
    "the others don't matter."

Usage:
    # Dry-run (prints counts, no writes):
    docker compose exec api python scripts/backfill_sms_grow_snapshots.py

    # Commit (writes snapshots):
    docker compose exec api python scripts/backfill_sms_grow_snapshots.py --commit

Idempotent: take_snapshot + direct SELECTs are no-ops where a snapshot
already exists for the (sub, lineage, source) key. Safe to re-run.

What gets written:
  - For each ACTIVE Pure-AOM Subscription under SMS Grow:
      - VIEWED (BACKFILL label) snapshot for each timeline where
        today ∈ [from_date, to_date) — the farmer's committed-by-time
        state. Overridable via force-push.
      - PURCHASE_ACK snapshot for each practice with purchased_at
        NOT NULL (via PracticeAcknowledgement) — the farmer's
        committed-by-money state. Sacrosanct against force-push.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
import app.main  # noqa: F401  — register ORM models

from app.modules.advisory.models import Package, PracticeAcknowledgement, Timeline
from app.modules.clients.models import Client
from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.services.snapshot import take_snapshot
from app.services.snapshot_render import cca_calendar_dates


SMS_GROW_DISPLAY_LIKE = "%SMS%GROW%"


async def run(commit: bool) -> None:
    async with AsyncSessionLocal() as db:
        # 1) Find SMS Grow client(s).
        clients = (await db.execute(
            select(Client).where(Client.display_name.ilike(SMS_GROW_DISPLAY_LIKE))
        )).scalars().all()
        if not clients:
            print(f"No clients matched {SMS_GROW_DISPLAY_LIKE!r}. Nothing to do.")
            return
        print(f"Clients matched: {len(clients)}")
        for c in clients:
            print(f"  - {c.display_name} (id={c.id}, short_name={c.short_name})")

        today_d = date.today()
        total_subs = 0
        total_viewed_written = 0
        total_ack_written = 0

        for client in clients:
            # 2) ACTIVE Pure-AOM subscriptions for this client.
            subs = (await db.execute(
                select(Subscription).where(
                    Subscription.client_id == client.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.advisory_only_mode == True,  # noqa: E712
                    Subscription.in_app_orders_enabled == False,  # noqa: E712
                )
            )).scalars().all()
            print(f"\n[{client.display_name}] ACTIVE Pure-AOM subs: {len(subs)}")
            total_subs += len(subs)

            for sub in subs:
                if sub.crop_start_date is None:
                    print(f"  sub={sub.reference_number} — skip (no crop_start_date)")
                    continue
                crop_start = (
                    sub.crop_start_date.date()
                    if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
                )

                # 3) Timelines for this sub's package — walk all and
                # snapshot where today ∈ [from, to).
                timelines = (await db.execute(
                    select(Timeline).where(
                        Timeline.package_id == sub.package_id,
                    )
                )).scalars().all()
                viewed_hits = 0
                for tl in timelines:
                    meta_dict = {"timeline": {
                        "from_type": (
                            tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
                        ),
                        "from_value": int(tl.from_value),
                        "to_value": int(tl.to_value),
                    }}
                    from app.services.snapshot_render import metadata_from_content
                    meta = metadata_from_content(meta_dict)
                    try:
                        from_d, to_d = cca_calendar_dates(meta, crop_start, today_d)
                    except Exception:
                        continue
                    if from_d <= today_d < to_d:
                        if commit:
                            try:
                                await take_snapshot(
                                    db, sub.id, tl.id, "BACKFILL", source="CCA",
                                )
                            except Exception as exc:
                                print(f"    VIEWED write failed sub={sub.reference_number} tl={tl.name}: {exc}")
                                continue
                        viewed_hits += 1

                # 4) PracticeAcknowledgement rows with purchased_at NOT NULL.
                acks = (await db.execute(
                    select(PracticeAcknowledgement).where(
                        PracticeAcknowledgement.subscription_id == sub.id,
                        PracticeAcknowledgement.purchased_at.is_not(None),
                    )
                )).scalars().all()
                ack_hits = 0
                for ack in acks:
                    # Resolve current ACTIVE timeline for this lineage
                    # under the sub's package so take_snapshot has a
                    # physical timeline_id to serialise.
                    tl_row = (await db.execute(
                        select(Timeline).where(
                            Timeline.package_id == sub.package_id,
                            Timeline.lineage_id == ack.timeline_lineage_id,
                        ).limit(1)
                    )).scalars().first()
                    if tl_row is None:
                        continue
                    if commit:
                        try:
                            await take_snapshot(
                                db, sub.id, tl_row.id, "PURCHASE_ACK", source="CCA",
                            )
                        except Exception as exc:
                            print(f"    PURCHASE_ACK write failed sub={sub.reference_number} lineage={ack.timeline_lineage_id}: {exc}")
                            continue
                    ack_hits += 1

                total_viewed_written += viewed_hits
                total_ack_written += ack_hits
                if viewed_hits or ack_hits:
                    print(f"  sub={sub.reference_number} — viewed={viewed_hits} ack={ack_hits}")

        print("\n" + "=" * 60)
        print(f"SUMMARY  (commit={commit})")
        print(f"  Pure-AOM subs scanned     : {total_subs}")
        print(f"  VIEWED/BACKFILL snapshots : {total_viewed_written}")
        print(f"  PURCHASE_ACK snapshots    : {total_ack_written}")
        if not commit:
            print("\n(Dry-run — nothing written. Re-run with --commit to apply.)")


if __name__ == "__main__":
    commit = "--commit" in sys.argv
    asyncio.run(run(commit))
