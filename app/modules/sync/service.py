"""
Cosh → RootsTalk sync service.

Processes the payload received from Cosh and upserts into cosh_reference_cache.
All Cosh data (both Core entities and Connect relationships) flows through here.

Field Mapping document (pending):
  Written once Cosh 2.0 is live and entity names are confirmed.
  This service is built to the RootsTalk API Contract spec.
  Any mismatch between Cosh's actual output and this contract will be
  resolved by adjusting Cosh's sync payload — not this endpoint.
"""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.modules.sync.models import CoshReferenceCache, CoshSyncLog


async def upsert_entity(
    db: AsyncSession,
    cosh_id: str,
    entity_type: str,
    status: str,
    translations: dict,
    parent_cosh_id: Optional[str],
    secondary_parent_cosh_id: Optional[str],
    metadata: Optional[dict],
) -> str:
    """
    Upsert one entity into the cache.
    Returns 'inserted' or 'updated'.
    """
    now = datetime.now(timezone.utc)

    # Validate: English translation is mandatory
    if not translations.get("en"):
        raise ValueError("Missing required translation: en")

    stmt = pg_insert(CoshReferenceCache).values(
        cosh_id=cosh_id,
        entity_type=entity_type,
        parent_cosh_id=parent_cosh_id,
        secondary_parent_cosh_id=secondary_parent_cosh_id,
        status=status,
        translations=translations,
        metadata_=metadata,
        synced_at=now,
    ).on_conflict_do_update(
        constraint="uq_cosh_ref_id_type",
        set_={
            "parent_cosh_id": parent_cosh_id,
            "secondary_parent_cosh_id": secondary_parent_cosh_id,
            "status": status,
            "translations": translations,
            "metadata_": metadata,
            "synced_at": now,
        }
    )
    result = await db.execute(stmt)
    return "inserted" if result.rowcount == 1 else "updated"


async def inactivate_absent_entities(db: AsyncSession, entity_type: str, seen_ids: set[str]):
    """
    Full sync: mark any entity of this type NOT in seen_ids as inactive.
    Only called during sync_mode=full.
    """
    await db.execute(
        update(CoshReferenceCache)
        .where(
            CoshReferenceCache.entity_type == entity_type,
            CoshReferenceCache.cosh_id.not_in(seen_ids) if seen_ids else True,
            CoshReferenceCache.status == "active",
        )
        .values(status="inactive")
    )


async def process_payload(db: AsyncSession, payload: dict, sync_log: CoshSyncLog) -> dict:
    """
    Process the full sync payload.
    Returns entity_results summary.
    """
    sync_mode = payload.get("sync_mode", "incremental")
    entity_batches = payload.get("entity_batches", [])

    entity_results = []
    total_inserted = 0
    total_updated = 0
    total_failed = 0

    for batch in entity_batches:
        entity_type = batch.get("entity_type")
        items = batch.get("items", [])

        batch_inserted = 0
        batch_updated = 0
        batch_failed = 0
        errors = []
        seen_ids = set()

        for item in items:
            cosh_id = item.get("cosh_id")
            try:
                if not cosh_id:
                    raise ValueError("Missing cosh_id")

                action = await upsert_entity(
                    db=db,
                    cosh_id=cosh_id,
                    entity_type=entity_type,
                    status=item.get("status", "active"),
                    translations=item.get("translations", {}),
                    parent_cosh_id=item.get("parent_cosh_id"),
                    secondary_parent_cosh_id=item.get("secondary_parent_cosh_id"),
                    metadata=item.get("metadata"),
                )
                seen_ids.add(cosh_id)
                if action == "inserted":
                    batch_inserted += 1
                else:
                    batch_updated += 1

            except Exception as e:
                batch_failed += 1
                errors.append({"cosh_id": cosh_id or "unknown", "reason": str(e)})

        # Full sync: inactivate any entities of this type not in the payload
        if sync_mode == "full" and seen_ids is not None:
            await inactivate_absent_entities(db, entity_type, seen_ids)

        total_inserted += batch_inserted
        total_updated += batch_updated
        total_failed += batch_failed

        entity_results.append({
            "entity_type": entity_type,
            "received": len(items),
            "inserted": batch_inserted,
            "updated": batch_updated,
            "failed": batch_failed,
            "errors": errors,
        })

    sync_log.items_synced = total_inserted + total_updated
    sync_log.items_failed = total_failed
    sync_log.status = "partial" if total_failed > 0 and (total_inserted + total_updated) > 0 \
        else "failed" if total_failed > 0 and (total_inserted + total_updated) == 0 \
        else "completed"
    sync_log.completed_at = datetime.now(timezone.utc)

    return {
        "sync_id": payload.get("sync_id"),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "status": sync_log.status,
        "summary": {
            "total_items": total_inserted + total_updated + total_failed,
            "inserted": total_inserted,
            "updated": total_updated,
            "inactivated": 0,
            "failed": total_failed,
        },
        "entity_results": entity_results,
    }


def get_cosh_entity(db_sync, cosh_id: str, entity_type: str):
    """Synchronous lookup for use in business logic layers."""
    pass  # implemented as async in router


async def get_cosh_translation(
    db: AsyncSession,
    cosh_id: str,
    entity_type: str,
    language_code: str = "en",
) -> Optional[str]:
    """
    Returns the display name for a Cosh entity in the requested language.
    Falls back to English if the language is not available.
    """
    result = await db.execute(
        select(CoshReferenceCache.translations).where(
            CoshReferenceCache.cosh_id == cosh_id,
            CoshReferenceCache.entity_type == entity_type,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return row.get(language_code) or row.get("en")
