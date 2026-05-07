"""
Cosh → RootsTalk sync service.

Processes incoming sync payloads from Cosh and persists them into:
  • cosh_reference_cache       — legacy single-table cache (kept alive
                                  during the schema-migration transition;
                                  to be dropped once every readsite is
                                  refactored).
  • cosh_core_items            — flat Cosh entities (Cores).
  • cosh_connect_rows          — N-ary Connects with typed endpoints.

Both new tables receive the same data as the legacy table — dual-write —
so existing readsites keep working until they're individually migrated
to read the new tables.

Classification & adapter
------------------------
Today Cosh emits a flat payload (`entity_batches[].items[]` with
`parent_cosh_id`, optional `secondary_parent_cosh_id`, `metadata`).
The adapter classifies each item by `entity_type`:

  • Connect entity_types (today: only `problem_to_symptom`) — the
    payload's metadata holds the endpoint cosh_ids under per-role keys
    (problem_cosh_id, plant_part_cosh_id, …). The adapter pulls them
    out via `CONNECT_ENDPOINT_ROLES` and writes a typed `endpoints`
    array. The remainder of `metadata` (priority_rank, crop_stage_cosh_id,
    etc.) lands on `cosh_connect_rows.metadata_`.

  • Cores (everything else) — the payload's `parent_cosh_id` carries
    onto `cosh_core_items.parent_cosh_id`; metadata stays as-is.
    `secondary_parent_cosh_id` is unused going forward and is dropped.

When Cosh later emits native typed Connect payloads (with `endpoints`
directly in the item), the adapter detects the array and bypasses the
metadata extraction. So this code is forward-compatible without a
second migration.

Field Mapping document (pending)
--------------------------------
A field-mapping doc will be locked once Cosh 2.0's first production
sync is verified end-to-end against this endpoint. Any divergence
between Cosh's actual emit and this contract is resolved by adjusting
Cosh's payload — not by patching this service.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import (
    CoshConnectRow, CoshCoreItem, CoshReferenceCache, CoshSyncLog,
)


# ── Classification & endpoint-role adapter ──────────────────────────────────

CONNECT_ENTITY_TYPES: frozenset[str] = frozenset({
    "problem_to_symptom",
})

# For each Connect entity_type, mapping from endpoint role → metadata key
# in the legacy flat payload. The adapter uses this to extract endpoints
# when the item doesn't already have a typed `endpoints` array.
CONNECT_ENDPOINT_ROLES: dict[str, dict[str, str]] = {
    "problem_to_symptom": {
        "problem":     "problem_cosh_id",
        "plant_part":  "plant_part_cosh_id",
        "symptom":     "symptom_cosh_id",
        "sub_part":    "sub_part_cosh_id",
        "sub_symptom": "sub_symptom_cosh_id",
    },
}


def _is_connect(entity_type: str) -> bool:
    return entity_type in CONNECT_ENTITY_TYPES


def _extract_endpoints(entity_type: str, item: dict) -> list[dict]:
    """Build the endpoints array for a Connect row.

    Native typed payload has `endpoints` directly. Legacy flat payload
    has endpoint cosh_ids in `metadata` under per-role keys; the
    adapter pulls them out via CONNECT_ENDPOINT_ROLES."""
    native = item.get("endpoints")
    if isinstance(native, list) and native:
        return native

    role_map = CONNECT_ENDPOINT_ROLES.get(entity_type, {})
    metadata = item.get("metadata") or {}
    out: list[dict] = []
    for role, meta_key in role_map.items():
        cosh_id = metadata.get(meta_key)
        if cosh_id:
            out.append({"role": role, "cosh_id": cosh_id})
    return out


def _connect_metadata_clean(entity_type: str, item: dict) -> Optional[dict]:
    """Strip the role-keyed cosh_ids out of metadata when writing a
    Connect row — those values now live in `endpoints`, so keeping
    them in metadata too would duplicate the truth-source."""
    metadata = item.get("metadata")
    if not metadata:
        return None
    role_map = CONNECT_ENDPOINT_ROLES.get(entity_type, {})
    if not role_map:
        return metadata
    cleaned = {k: v for k, v in metadata.items() if k not in role_map.values()}
    return cleaned or None


# ── Upserts ─────────────────────────────────────────────────────────────────

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
    """Legacy upsert into cosh_reference_cache. Returns 'inserted' or
    'updated'. Translations must include `en`."""
    if not translations.get("en"):
        raise ValueError("Missing required translation: en")
    now = datetime.now(timezone.utc)
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
            "metadata": metadata,
            "synced_at": now,
        },
    )
    result = await db.execute(stmt)
    return "inserted" if result.rowcount == 1 else "updated"


async def upsert_core_item(
    db: AsyncSession,
    *,
    cosh_id: str,
    core_type: str,
    parent_cosh_id: Optional[str],
    status: str,
    translations: dict,
    metadata: Optional[dict],
) -> None:
    """Upsert into cosh_core_items. Translations must include `en`."""
    if not translations.get("en"):
        raise ValueError("Missing required translation: en")
    now = datetime.now(timezone.utc)
    stmt = pg_insert(CoshCoreItem).values(
        cosh_id=cosh_id,
        core_type=core_type,
        parent_cosh_id=parent_cosh_id,
        status=status,
        translations=translations,
        metadata_=metadata,
        synced_at=now,
    ).on_conflict_do_update(
        constraint="uq_cosh_core_id_type",
        set_={
            "parent_cosh_id": parent_cosh_id,
            "status": status,
            "translations": translations,
            "metadata": metadata,
            "synced_at": now,
        },
    )
    await db.execute(stmt)


async def upsert_connect_row(
    db: AsyncSession,
    *,
    connect_id: str,
    connect_type: str,
    endpoints: list[dict],
    status: str,
    metadata: Optional[dict],
) -> None:
    """Upsert into cosh_connect_rows. `endpoints` must be a non-empty
    list of {role, cosh_id} dicts."""
    if not endpoints:
        raise ValueError(
            f"Connect {connect_type} for {connect_id!r} has no endpoints"
        )
    now = datetime.now(timezone.utc)
    stmt = pg_insert(CoshConnectRow).values(
        connect_id=connect_id,
        connect_type=connect_type,
        endpoints=endpoints,
        status=status,
        metadata_=metadata,
        synced_at=now,
    ).on_conflict_do_update(
        constraint="uq_cosh_connect_id_type",
        set_={
            "endpoints": endpoints,
            "status": status,
            "metadata": metadata,
            "synced_at": now,
        },
    )
    await db.execute(stmt)


# ── Full-sync inactivation ──────────────────────────────────────────────────

async def inactivate_absent_entities(
    db: AsyncSession, entity_type: str, seen_ids: set[str],
) -> None:
    """Full sync: mark any active rows of this entity_type whose id
    is not in `seen_ids` as inactive. Mirrors the inactivation across
    legacy `cosh_reference_cache` and the new typed table for the same
    entity_type."""

    # Legacy cache
    legacy_q = update(CoshReferenceCache).where(
        CoshReferenceCache.entity_type == entity_type,
        CoshReferenceCache.status == "active",
    )
    if seen_ids:
        legacy_q = legacy_q.where(CoshReferenceCache.cosh_id.not_in(seen_ids))
    await db.execute(legacy_q.values(status="inactive"))

    # New typed table — Core or Connect
    if _is_connect(entity_type):
        new_q = update(CoshConnectRow).where(
            CoshConnectRow.connect_type == entity_type,
            CoshConnectRow.status == "active",
        )
        if seen_ids:
            new_q = new_q.where(CoshConnectRow.connect_id.not_in(seen_ids))
        await db.execute(new_q.values(status="inactive"))
    else:
        new_q = update(CoshCoreItem).where(
            CoshCoreItem.core_type == entity_type,
            CoshCoreItem.status == "active",
        )
        if seen_ids:
            new_q = new_q.where(CoshCoreItem.cosh_id.not_in(seen_ids))
        await db.execute(new_q.values(status="inactive"))


# ── Main payload processor ──────────────────────────────────────────────────

async def process_payload(
    db: AsyncSession, payload: dict, sync_log: CoshSyncLog,
) -> dict:
    """Process the full sync payload: dual-writes every item to the
    legacy table and the appropriate typed table. Returns the summary
    body the sync endpoint hands back to Cosh."""
    sync_mode = payload.get("sync_mode", "incremental")
    entity_batches = payload.get("entity_batches", [])

    entity_results: list[dict] = []
    total_inserted = 0
    total_updated = 0
    total_failed = 0

    for batch in entity_batches:
        entity_type = batch.get("entity_type")
        items = batch.get("items", [])

        batch_inserted = 0
        batch_updated = 0
        batch_failed = 0
        errors: list[dict] = []
        seen_ids: set[str] = set()

        for item in items:
            cosh_id = item.get("cosh_id")
            try:
                if not cosh_id:
                    raise ValueError("Missing cosh_id")
                translations = item.get("translations", {})
                item_status = item.get("status", "active")
                metadata = item.get("metadata")

                # 1. Legacy cache (kept alive during transition)
                action = await upsert_entity(
                    db=db,
                    cosh_id=cosh_id,
                    entity_type=entity_type,
                    status=item_status,
                    translations=translations,
                    parent_cosh_id=item.get("parent_cosh_id"),
                    secondary_parent_cosh_id=item.get("secondary_parent_cosh_id"),
                    metadata=metadata,
                )

                # 2. New typed tables — Core or Connect
                if _is_connect(entity_type):
                    endpoints = _extract_endpoints(entity_type, item)
                    await upsert_connect_row(
                        db=db,
                        connect_id=cosh_id,
                        connect_type=entity_type,
                        endpoints=endpoints,
                        status=item_status,
                        metadata=_connect_metadata_clean(entity_type, item),
                    )
                else:
                    await upsert_core_item(
                        db=db,
                        cosh_id=cosh_id,
                        core_type=entity_type,
                        parent_cosh_id=item.get("parent_cosh_id"),
                        status=item_status,
                        translations=translations,
                        metadata=metadata,
                    )

                seen_ids.add(cosh_id)
                if action == "inserted":
                    batch_inserted += 1
                else:
                    batch_updated += 1

            except Exception as e:
                batch_failed += 1
                errors.append({"cosh_id": cosh_id or "unknown", "reason": str(e)})

        # Full sync: inactivate any (entity_type) rows not seen this run
        if sync_mode == "full":
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
    if total_failed > 0 and (total_inserted + total_updated) > 0:
        sync_log.status = "partial"
    elif total_failed > 0:
        sync_log.status = "failed"
    else:
        sync_log.status = "completed"
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
    """Returns the display name for a Cosh entity in the requested
    language. Falls back to English if the language is not available.

    Reads from the legacy cache today; will switch to cosh_core_items
    in Batch #100 when readsites are migrated."""
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
