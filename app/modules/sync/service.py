"""
Cosh → RootsTalk sync service.

Processes incoming sync payloads from Cosh and persists them into:
  • cosh_core_items            — flat Cosh entities (Cores).
  • cosh_connect_rows          — N-ary Connects with typed endpoints.

Built to the contract in docs/COSH_2_SYNC_CONTRACT.md. That document is
the alignment reference between Cosh 2.0's emitter and this ingest;
any divergence is resolved there before either side patches code.

Classification rule (§4 of the contract)
----------------------------------------
Each incoming item is routed by **payload shape**, not by a hardcoded
entity_type list:

  • item has `positions` dict  → Connect → cosh_connect_rows
  • item has `translations`    → Core    → cosh_core_items

This means new Cosh entities (new image Connects per crop, new Cores
for ITKs, etc.) onboard with zero backend code change. Cosh stays the
schema authority.

Connect endpoints adapter
-------------------------
Cosh emits a `positions` dict keyed by stringified position number:

    {"1": {"cosh_id": "...", "entity_type": "crop"},
     "2": {"cosh_id": "...", "entity_type": "crop_stage"},
     ...}

The adapter reshapes this into our internal endpoints array:

    [{"role": "crop",       "cosh_id": "...", "position": 1},
     {"role": "crop_stage", "cosh_id": "...", "position": 2},
     ...]

`role` mirrors Cosh's `entity_type` for the target — same vocabulary
end-to-end so consumers (BL-08, image lookup) read by Cosh's role
names without translation.

BlankBox sentinel (§5 of the contract)
--------------------------------------
A position whose value is the BlankBox sentinel is dropped from the
endpoints array — downstream code sees the position as absent.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem, CoshSyncLog


# ── BlankBox sentinel ───────────────────────────────────────────────────────
# Set per Cosh's chosen spelling. The contract doc tracks this; both
# sides reference the same constant. Pin both common spellings until
# Cosh confirms.

BLANK_BOX_VALUES: frozenset[str] = frozenset({
    "BlankBox",
    "Blank Box",
})


def _is_blank_box(value: Optional[str]) -> bool:
    return value is not None and value in BLANK_BOX_VALUES


# ── Connect-vs-Core classification (shape-driven) ───────────────────────────

def _is_connect(item: dict) -> bool:
    """An item is a Connect when it carries a non-empty `positions`
    dict. Otherwise it's a Core (must carry `translations`)."""
    positions = item.get("positions")
    return isinstance(positions, dict) and bool(positions)


# ── Connect-positions adapter ───────────────────────────────────────────────

def _extract_endpoints(item: dict) -> list[dict]:
    """Reshape Cosh's `positions` dict into our endpoints array.

    Drops:
      • positions whose `cosh_id` is missing or BlankBox-sentinel-valued
      • positions whose `entity_type` is missing
    Sorts by integer position number for stable ordering.
    """
    positions = item.get("positions") or {}
    out: list[dict] = []
    for pos_num_str, pos in sorted(positions.items(), key=lambda kv: int(kv[0])):
        cosh_id = pos.get("cosh_id")
        role = pos.get("entity_type")
        if not cosh_id or not role:
            continue
        if _is_blank_box(cosh_id):
            continue
        out.append({
            "role": role,
            "cosh_id": cosh_id,
            "position": int(pos_num_str),
        })
    return out


def _connect_row_metadata(item: dict) -> Optional[dict]:
    """Connect rows carry scalar attributes outside `positions` (e.g.
    `priority_rank` on pest_diagnosis_chain). Pull every top-level
    field that isn't part of the protocol envelope into metadata."""
    reserved = {"cosh_id", "entity_type", "status", "positions"}
    extras = {k: v for k, v in item.items() if k not in reserved}
    return extras or None


# ── Upserts ─────────────────────────────────────────────────────────────────

async def upsert_core_item(
    db: AsyncSession,
    *,
    cosh_id: str,
    core_type: str,
    parent_cosh_id: Optional[str],
    status: str,
    translations: dict,
    metadata: Optional[dict],
) -> str:
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
    result = await db.execute(stmt)
    return "inserted" if result.rowcount == 1 else "updated"


async def upsert_connect_row(
    db: AsyncSession,
    *,
    connect_id: str,
    connect_type: str,
    endpoints: list[dict],
    status: str,
    metadata: Optional[dict],
) -> str:
    """Upsert into cosh_connect_rows. `endpoints` must be non-empty."""
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
    result = await db.execute(stmt)
    return "inserted" if result.rowcount == 1 else "updated"


# ── Full-sync inactivation ──────────────────────────────────────────────────

async def inactivate_absent_entities(
    db: AsyncSession,
    *,
    entity_type: str,
    is_connect_batch: bool,
    seen_ids: set[str],
) -> None:
    """Full sync: mark active rows of this entity_type whose id is not
    in `seen_ids` as inactive. Routes to cosh_core_items or
    cosh_connect_rows by the batch's classification."""
    if is_connect_batch:
        q = update(CoshConnectRow).where(
            CoshConnectRow.connect_type == entity_type,
            CoshConnectRow.status == "active",
        )
        if seen_ids:
            q = q.where(CoshConnectRow.connect_id.not_in(seen_ids))
        await db.execute(q.values(status="inactive"))
    else:
        q = update(CoshCoreItem).where(
            CoshCoreItem.core_type == entity_type,
            CoshCoreItem.status == "active",
        )
        if seen_ids:
            q = q.where(CoshCoreItem.cosh_id.not_in(seen_ids))
        await db.execute(q.values(status="inactive"))


# ── Main payload processor ──────────────────────────────────────────────────

async def process_payload(
    db: AsyncSession, payload: dict, sync_log: CoshSyncLog,
) -> dict:
    """Process a sync payload per the Cosh 2.0 contract.

    Each batch's items are classified by shape (positions ⇒ Connect,
    translations ⇒ Core) and upserted into the appropriate typed
    table. Mixed-shape batches are technically supported but unusual —
    Cosh's emitter sends one shape per batch."""
    sync_mode = payload.get("sync_mode", "incremental")
    entity_batches = payload.get("entity_batches", [])

    entity_results: list[dict] = []
    total_inserted = 0
    total_updated = 0
    total_failed = 0

    for batch in entity_batches:
        entity_type = batch.get("entity_type")
        items = batch.get("items", [])

        # Classify the batch by inspecting its first valid item.
        # Empty batches are skipped silently.
        batch_is_connect = any(_is_connect(i) for i in items)

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
                item_status = item.get("status", "active")

                if _is_connect(item):
                    endpoints = _extract_endpoints(item)
                    action = await upsert_connect_row(
                        db=db,
                        connect_id=cosh_id,
                        connect_type=entity_type,
                        endpoints=endpoints,
                        status=item_status,
                        metadata=_connect_row_metadata(item),
                    )
                else:
                    translations = item.get("translations", {})
                    action = await upsert_core_item(
                        db=db,
                        cosh_id=cosh_id,
                        core_type=entity_type,
                        parent_cosh_id=item.get("parent_cosh_id"),
                        status=item_status,
                        translations=translations,
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

        # Full sync: inactivate any (entity_type) rows not seen this run
        if sync_mode == "full":
            await inactivate_absent_entities(
                db,
                entity_type=entity_type,
                is_connect_batch=batch_is_connect,
                seen_ids=seen_ids,
            )

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
    Reads from cosh_core_items (Connect rows have no translations)."""
    result = await db.execute(
        select(CoshCoreItem.translations).where(
            CoshCoreItem.cosh_id == cosh_id,
            CoshCoreItem.core_type == entity_type,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return row.get(language_code) or row.get("en")
